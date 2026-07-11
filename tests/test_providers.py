"""Provider construction + the OpenRouter malformed-tool-call guard.

When a provider streams undecodable tool-call arguments (gemma's
token-level format is parsed provider-side; truncated parses happen),
agno records an id-only stub — ``{'id': ...}``, no ``function`` — and
replaying it 400s on every provider, killing the turn. The studio's
OpenRouter subclass strips stubs and their paired error results from
every outbound request.
"""

import pytest

pytest.importorskip("agno")

from agno.models.message import Message  # noqa: E402

from nontainer_studio.providers import build_model  # noqa: E402


def _shape(spec: str):
    return build_model(spec)


def test_openrouter_drops_malformed_stub_and_its_result():
    model = _shape("openrouter:google/gemma-4-26b-a4b-it")
    messages = [
        Message(role="user", content="make an app"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                {"id": "bad-1"},  # undecodable-arguments stub
                {
                    "id": "ok-1",
                    "type": "function",
                    "function": {"name": "file_write", "arguments": "{}"},
                },
            ],
        ),
        Message(role="tool", tool_call_id="bad-1", content="Error: no such tool"),
        Message(role="tool", tool_call_id="ok-1", content="wrote /a.py"),
    ]
    formatted = model._format_all_messages(messages)
    assert [m["role"] for m in formatted] == ["user", "assistant", "tool"]
    (assistant,) = [m for m in formatted if m["role"] == "assistant"]
    assert len(assistant["tool_calls"]) == 1
    assert assistant["tool_calls"][0]["function"]["name"] == "file_write"
    # the original Message objects are NOT mutated (they're the stored
    # conversation; sanitation is per-request only)
    assert len(messages[1].tool_calls) == 2


def test_openrouter_clean_messages_pass_through():
    model = _shape("openrouter:qwen/qwen3.6-35b-a3b")
    messages = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    formatted = model._format_all_messages(messages)
    assert [m["role"] for m in formatted] == ["user", "assistant"]


def test_gemma_routes_around_broken_tool_call_parsers():
    model = _shape("openrouter:google/gemma-4-26b-a4b-it")
    assert model.extra_body == {
        "provider": {
            "order": ["deepinfra", "cloudflare"],
            "ignore": ["novita", "google-vertex"],
        }
    }
    assert _shape("openrouter:qwen/qwen3.6-35b-a3b").extra_body is None


def test_gpt56_rides_the_responses_endpoint():
    model = _shape("openrouter:openai/gpt-5.6-luna")
    assert type(model).__name__ == "OpenRouterResponses"


def test_streamed_reasoning_fragments_merge_into_whole_blocks():
    """OpenRouter streams reasoning_details as index-keyed fragments;
    replaying them unmerged breaks signed thinking blocks (Anthropic:
    'Invalid `signature` in `thinking` block'). The formatted message
    must carry whole blocks."""
    model = _shape("openrouter:anthropic/claude-sonnet-5")
    msg = Message(
        role="assistant",
        content=None,
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }
        ],
        provider_data={
            "reasoning_details": [
                {"type": "reasoning.text", "index": 0, "text": "let me "},
                {"type": "reasoning.text", "index": 0, "text": "think"},
                {"type": "reasoning.text", "index": 0, "signature": "sig-abc"},
                {"type": "reasoning.text", "index": 1, "text": "second block"},
            ]
        },
    )
    formatted = model._format_message(msg)
    details = formatted["reasoning_details"]
    assert len(details) == 2
    assert details[0]["text"] == "let me think"
    assert details[0]["signature"] == "sig-abc"
    assert details[1]["text"] == "second block"
