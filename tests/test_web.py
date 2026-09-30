"""The `web` host object, against a stubbed OpenRouter."""

import json

import httpx
import pytest

from nontainer_studio import web as web_mod
from nontainer_studio.web import Web, web_enabled


def _chat(content, annotations=None, fetched=1):
    message = {"role": "assistant", "content": content}
    if annotations is not None:
        message["annotations"] = annotations
    usage = {"server_tool_use_details": {"tool_calls_executed": fetched}}
    return {"choices": [{"message": message}], "usage": usage}


def _cite(url, title=""):
    return {"type": "url_citation", "url_citation": {"url": url, "title": title}}


def _web(handler):
    """A Web over a handler that sees each request body and returns
    (status, json)."""
    sent = []

    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        status, payload = handler(body)
        return httpx.Response(status, json=payload)

    return Web("key", transport=httpx.MockTransport(respond)), sent


def test_search_answers_with_numbered_sources():
    """Perplexity's [n] markers count through the response's url
    citations, so the answer comes back with those, numbered to match."""
    w, sent = _web(
        lambda body: (
            200,
            _chat(
                "It is MIT.[1][2]",
                [_cite("https://a.example", "A"), _cite("https://b.example")],
            ),
        )
    )
    assert w.search("license?") == (
        "It is MIT.[1][2]\n\nSources:\n[1] A — https://a.example\n[2] https://b.example"
    )
    assert sent[0]["model"] == web_mod.SEARCH_MODEL
    assert sent[0]["messages"][-1] == {"role": "user", "content": "license?"}

    w.search("compare them", deep=True)
    assert sent[1]["model"] == web_mod.DEEP_SEARCH_MODEL


def test_a_list_of_queries_runs_together_and_keeps_its_order():
    """One failing query holds its error in its own slot rather than
    costing the others, which were paid for."""

    def handler(body):
        q = body["messages"][-1]["content"]
        if q == "bad":
            return 500, {"error": "upstream"}
        return 200, _chat(f"answer to {q}")

    w, _ = _web(handler)
    out = w.search(["one", "bad", "two"])
    assert out[0] == "answer to one"
    assert out[1].startswith("web.search failed for 'bad': web.search: HTTP 500")
    assert out[2] == "answer to two"
    assert w.search([]) == []


def test_fetch_asks_a_small_model_to_read_the_page():
    w, sent = _web(lambda body: (200, _chat("  The default is 16.  ")))
    out = w.fetch("https://docs.example/page", "what is the default?")
    assert out == "The default is 16.\n\nSource: https://docs.example/page"
    body = sent[0]
    assert body["model"] == web_mod.FETCH_MODEL
    assert body["tools"] == [
        {
            "type": "openrouter:web_fetch",
            "parameters": {
                "engine": "parallel",
                "max_uses": 1,
                "max_content_tokens": web_mod.FETCH_MAX_CONTENT_TOKENS,
            },
        }
    ]
    assert body["tool_choice"] == "required"
    assert "https://docs.example/page" in body["messages"][-1]["content"]


def test_fetch_never_credits_a_page_it_did_not_read():
    """An answer from the model's memory must not come back as the
    page's: without an executed fetch there is no answer to give."""
    for payload in (
        _chat("From memory.", fetched=0),
        {"choices": _chat("x")["choices"]},
    ):
        w, _ = _web(lambda body, payload=payload: (200, payload))
        with pytest.raises(RuntimeError, match="the page was not read"):
            w.fetch("https://docs.example/page", "what is the default?")


def test_fetch_refuses_what_it_cannot_do():
    w, sent = _web(lambda body: (200, _chat("x")))
    with pytest.raises(ValueError, match="not an http"):
        w.fetch("file:///etc/passwd", "anything")
    with pytest.raises(ValueError, match="what to find"):
        w.fetch("https://a.example", "  ")
    assert sent == []


def test_failures_read_as_one_line_the_agent_can_act_on():
    w, _ = _web(lambda body: (401, {"error": {"message": "No auth credentials"}}))
    with pytest.raises(RuntimeError, match=r"web.search: HTTP 401: .*No auth"):
        w.search("q")
    w, _ = _web(lambda body: (200, _chat("")))
    with pytest.raises(RuntimeError, match="web.fetch: the answer was empty"):
        w.fetch("https://a.example", "q")

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    w = Web("key", transport=httpx.MockTransport(slow))
    with pytest.raises(RuntimeError, match=r"web.search: no answer within 60s"):
        w.search("q")


def test_web_needs_a_key_and_can_be_turned_off(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("NONTAINER_STUDIO_WEB", raising=False)
    assert not web_enabled()
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    assert web_enabled()
    for off in ("0", "off", "False"):
        monkeypatch.setenv("NONTAINER_STUDIO_WEB", off)
        assert not web_enabled()
    monkeypatch.setenv("NONTAINER_STUDIO_WEB", "1")
    assert web_enabled()
