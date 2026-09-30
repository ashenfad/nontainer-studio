"""The ``web`` host object: web search and page reading through OpenRouter.

Handed to an agent session's Python, never to a published app (see
``Registry._python_config``). Search runs on Perplexity's Sonar models.
Reading a page runs a small model with OpenRouter's ``web_fetch`` server
tool, whose extraction engine turns the page into clean text; the model
answers a question from it. The page's text itself never comes back:
OpenRouter hands fetched content only to a model, and a model asked to
relay a page verbatim silently abridges it, so ``fetch`` is shaped as a
question rather than a download.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

SEARCH_MODEL = "perplexity/sonar"
DEEP_SEARCH_MODEL = "perplexity/sonar-pro-search"
FETCH_MODEL = "google/gemini-3.5-flash-lite"
#: Parallel extracts a page's main content, dropping navigation and other
#: page furniture. Measured against the alternatives on one GitHub README
#: that changed days earlier: Parallel read the current page; Exa (and
#: "auto", which picked Exa) served an older, abridged copy; the raw
#: "openrouter" engine handed the model 124k tokens of HTML and still
#: missed the answer, at twenty times the cost.
FETCH_ENGINE = "parallel"
#: How much of a page the reading model is shown, in tokens. A long page
#: past this is cut, not summarized.
FETCH_MAX_CONTENT_TOKENS = 25_000

# Measured: a quick search takes 3-5s, a deep one 20-40s, a page read
# 4-10s. The agent's Python timeout (``AGENT_PYTHON_TIMEOUT`` in
# sessions.py) is set above the longest of these, since time spent in a
# host call counts against it.
SEARCH_TIMEOUT = 60.0
DEEP_SEARCH_TIMEOUT = 150.0
FETCH_TIMEOUT = 90.0

#: The most queries one list call runs at once.
MAX_CONCURRENT = 8

SEARCH_PROMPT = "Answer the question using web search. Be specific and cite sources."
FETCH_PROMPT = (
    "Read the page at the URL with the web_fetch tool, then answer the "
    "question from that page alone. Quote exact names, figures, code and "
    "wording where they matter. If the page could not be fetched, or does "
    "not answer the question, say so plainly instead of answering from "
    "memory."
)

_OFF = ("0", "false", "no", "off")


def web_enabled() -> bool:
    """Whether agent sessions get ``web``: an OpenRouter key is set and
    ``NONTAINER_STUDIO_WEB`` doesn't turn it off."""
    if not os.getenv("OPENROUTER_API_KEY"):
        return False
    return os.getenv("NONTAINER_STUDIO_WEB", "").strip().lower() not in _OFF


class Web:
    """Search the web and read pages. Every method returns plain text."""

    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/ashenfad/nontainer-studio",
                "X-Title": "nontainer-studio",
            },
        )

    def search(self, query: str | list[str], deep: bool = False) -> str | list[str]:
        """Answer ``query`` from a web search: the answer, then its
        numbered sources. ``deep=True`` runs a multi-step search, slower
        and pricier, for questions that need synthesis. A list of queries
        runs concurrently and returns a list of answers in the same
        order; one that fails holds its error in place of an answer."""
        if isinstance(query, (list, tuple)):
            queries = [str(q) for q in query]
            if not queries:
                return []
            with ThreadPoolExecutor(min(MAX_CONCURRENT, len(queries))) as pool:
                return list(pool.map(lambda q: self._search_or_error(q, deep), queries))
        return self._search(str(query), deep)

    def fetch(self, url: str, question: str) -> str:
        """Read the page at ``url`` and answer ``question`` from it. An
        extract, not the page's text: ask for the part you need, and ask
        for it quoted when the exact wording matters."""
        url, question = str(url).strip(), str(question).strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"web.fetch: not an http(s) URL: {url!r}")
        if not question:
            raise ValueError("web.fetch: say what to find on the page")
        data = self._post(
            "web.fetch",
            {
                "model": FETCH_MODEL,
                "messages": [
                    {"role": "system", "content": FETCH_PROMPT},
                    {"role": "user", "content": f"URL: {url}\nQuestion: {question}"},
                ],
                "tools": [
                    {
                        "type": "openrouter:web_fetch",
                        "parameters": {
                            "engine": FETCH_ENGINE,
                            "max_uses": 1,
                            "max_content_tokens": FETCH_MAX_CONTENT_TOKENS,
                        },
                    }
                ],
            },
            FETCH_TIMEOUT,
        )
        return f"{_content('web.fetch', data)}\n\nSource: {url}"

    def _search_or_error(self, query: str, deep: bool) -> str:
        try:
            return self._search(query, deep)
        except Exception as e:
            return f"web.search failed for {query!r}: {e}"

    def _search(self, query: str, deep: bool) -> str:
        if not query.strip():
            raise ValueError("web.search: the query is empty")
        data = self._post(
            "web.search",
            {
                "model": DEEP_SEARCH_MODEL if deep else SEARCH_MODEL,
                "messages": [
                    {"role": "system", "content": SEARCH_PROMPT},
                    {"role": "user", "content": query},
                ],
            },
            DEEP_SEARCH_TIMEOUT if deep else SEARCH_TIMEOUT,
        )
        answer = _content("web.search", data)
        sources = _sources(data)
        return f"{answer}\n\nSources:\n{sources}" if sources else answer

    def _post(self, label: str, body: dict, timeout: float) -> dict:
        try:
            r = self._client.post(OPENROUTER_CHAT_URL, json=body, timeout=timeout)
        except httpx.TimeoutException:
            raise RuntimeError(f"{label}: no answer within {timeout:.0f}s") from None
        except httpx.HTTPError as e:
            raise RuntimeError(f"{label}: network error: {e}") from None
        if r.status_code != 200:
            # An HTML error page is no use to the agent past its start.
            detail = r.text[:300]
            raise RuntimeError(f"{label}: HTTP {r.status_code}: {detail}")
        try:
            return r.json()
        except ValueError:
            raise RuntimeError(f"{label}: the response was not JSON") from None


def _message(data: dict) -> dict:
    choices = data.get("choices") or [{}]
    return choices[0].get("message") or {}


def _content(label: str, data: dict) -> str:
    content = _message(data).get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"{label}: the answer was empty")
    return content.strip()


def _sources(data: dict) -> str:
    """The numbered source list for an answer's ``[n]`` markers, which
    count through the response's url citations from 1."""
    lines = []
    for i, note in enumerate(_message(data).get("annotations") or [], 1):
        cite: Any = note.get("url_citation") if isinstance(note, dict) else None
        if not isinstance(cite, dict) or not cite.get("url"):
            continue
        title = (cite.get("title") or "").strip()
        lines.append(
            f"[{i}] {title} — {cite['url']}" if title else f"[{i}] {cite['url']}"
        )
    return "\n".join(lines)
