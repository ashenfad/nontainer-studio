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


def test_openrouter_neutralizes_unparseable_arguments():
    """Provider-mangled args (Parasail split a JS `r =>` arrow into a
    bogus key/value) replay as a 400 on every subsequent request —
    wedging the session. The call survives with arguments={} so agno's
    paired decode-error result still reaches the model."""
    model = _shape("openrouter:qwen/qwen3.6-35b-a3b")
    broken = '{"actions": [{"eval": "fetch(x).then(r =": " r.json())"}]"}'
    messages = [
        Message(role="user", content="check the app"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "test_app", "arguments": broken},
                }
            ],
        ),
        Message(
            role="tool",
            tool_call_id="c1",
            content="Error while decoding function arguments...",
        ),
    ]
    formatted = model._format_all_messages(messages)
    assert [m["role"] for m in formatted] == ["user", "assistant", "tool"]
    (assistant,) = [m for m in formatted if m["role"] == "assistant"]
    assert assistant["tool_calls"][0]["function"]["arguments"] == "{}"
    # the error tool-result survives — that's the model's retry signal
    assert formatted[2]["role"] == "tool"
    # stored conversation untouched (sanitation is per-request only)
    assert messages[1].tool_calls[0]["function"]["arguments"] == broken


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


def test_openrouter_at_tag_pins_the_upstream_provider():
    """`model@slug[/quant]` pins OpenRouter's provider routing: order
    without fallbacks (an explicit pin means THAT provider), plus a
    quantization filter when given. The tag outranks curated routing
    and is stripped for identity (catalog lookups, model id)."""
    model = _shape("openrouter:qwen/qwen3.6-35b-a3b@wandb/fp8")
    assert model.id == "qwen/qwen3.6-35b-a3b"
    assert model.extra_body == {
        "provider": {
            "order": ["wandb"],
            "allow_fallbacks": False,
            "quantizations": ["fp8"],
        }
    }
    # slug-only: no quantization filter
    model = _shape("openrouter:qwen/qwen3.6-35b-a3b@deepinfra")
    assert model.extra_body == {
        "provider": {"order": ["deepinfra"], "allow_fallbacks": False}
    }
    # explicit tag replaces gemma's curated routing, keeps nothing stale
    model = _shape("openrouter:google/gemma-4-26b-a4b-it@venice")
    assert model.extra_body["provider"] == {
        "order": ["venice"],
        "allow_fallbacks": False,
    }
    # composes with anthropic's reasoning extra_body
    model = _shape("openrouter:anthropic/claude-sonnet-5@anthropic")
    assert model.extra_body["reasoning"] == {"max_tokens": 4096}
    assert model.extra_body["provider"]["order"] == ["anthropic"]


def test_openrouter_tag_is_ignored_for_catalog_metadata(monkeypatch):
    from nontainer_studio import providers

    monkeypatch.setattr(
        providers,
        "_openrouter_meta",
        {"anthropic/claude-sonnet-5": (True, 1_000_000)},
    )
    assert providers.supports_vision("openrouter:anthropic/claude-sonnet-5@anthropic")
    assert (
        providers.context_window("openrouter:anthropic/claude-sonnet-5@anthropic")
        == 1_000_000
    )


def test_gpt56_rides_the_responses_endpoint():
    """chat-completions rejects tools + reasoning for gpt-5.6 — both
    the openrouter AND direct-openai paths take the Responses API."""
    model = _shape("openrouter:openai/gpt-5.6-luna")
    assert type(model).__name__ == "OpenRouterResponses"
    model = _shape("openai:gpt-5.6-luna")
    assert type(model).__name__ == "OpenAIResponses"
    assert _shape("openai:gpt-5.4-mini").__class__.__name__ == "OpenAIChat"


def test_supports_vision_consults_openrouter_modalities(monkeypatch):
    """glm-5.2 has no image endpoints — attaching a test_app screenshot
    400s the next call. Vision gating rides OpenRouter's modality
    metadata; unknown models default to text-only (safe degradation)."""
    from nontainer_studio import providers

    monkeypatch.setattr(
        providers,
        "_openrouter_meta",
        {
            "anthropic/claude-sonnet-5": (True, 1_000_000),
            "z-ai/glm-5.2": (False, 202_752),
        },
    )
    assert providers.supports_vision("openrouter:anthropic/claude-sonnet-5")
    assert not providers.supports_vision("openrouter:z-ai/glm-5.2")
    assert not providers.supports_vision("openrouter:unknown/model")


def test_compress_token_limit_scales_with_context(monkeypatch):
    """Compaction watermark: 60% of the model's context, clamped to
    [32k, 250k] (the ceiling is a cost bound); unknown -> 100k; env
    override wins; 'off' disables."""
    from nontainer_studio import providers

    monkeypatch.delenv("NONTAINER_STUDIO_COMPRESS_TOKENS", raising=False)
    monkeypatch.setattr(
        providers,
        "_openrouter_meta",
        {
            "z-ai/glm-5.2": (False, 202_752),
            "big/ctx": (False, 1_000_000),
            "tiny/ctx": (False, 32_768),
        },
    )
    assert providers.compress_token_limit("openrouter:z-ai/glm-5.2") == int(
        202_752 * 0.6
    )
    assert providers.compress_token_limit("openrouter:big/ctx") == 250_000  # ceiling
    assert providers.compress_token_limit("openrouter:tiny/ctx") == 32_000  # floor
    assert providers.compress_token_limit("openrouter:unknown/model") == 100_000
    assert providers.compress_token_limit("anthropic:claude-sonnet-5") == 120_000

    monkeypatch.setenv("NONTAINER_STUDIO_COMPRESS_TOKENS", "50000")
    assert providers.compress_token_limit("openrouter:big/ctx") == 50_000
    monkeypatch.setenv("NONTAINER_STUDIO_COMPRESS_TOKENS", "off")
    assert providers.compress_token_limit("openrouter:big/ctx") is None


def test_openrouter_meta_failure_is_cached_with_ttl(monkeypatch):
    """A DNS blip at first lookup must not disable vision gating for
    the process lifetime — but an offline machine must not stall on
    every call either: failure is negatively cached for a TTL, then
    retried (PR #1 review)."""
    import urllib.request

    from nontainer_studio import providers

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise OSError("no dns")

    monkeypatch.setattr(providers, "_openrouter_meta", None)
    monkeypatch.setattr(providers, "_openrouter_meta_failed_at", None)
    monkeypatch.setattr(urllib.request, "urlopen", boom)

    assert providers._openrouter_model_meta("any/model") == (False, None)
    assert calls["n"] == 1
    # within the TTL: degraded default, NO second network attempt
    assert providers._openrouter_model_meta("any/model") == (False, None)
    assert calls["n"] == 1
    # after the TTL: the fetch is retried
    providers._openrouter_meta_failed_at -= providers._META_RETRY_SECONDS + 1
    assert providers._openrouter_model_meta("any/model") == (False, None)
    assert calls["n"] == 2


def test_supports_vision_provider_defaults():
    from nontainer_studio import providers

    assert providers.supports_vision("anthropic:claude-sonnet-5")
    assert providers.supports_vision("openai:gpt-5.6-sol")
    assert not providers.supports_vision("ollama:llama3.3")
    assert providers.supports_vision("dummy")  # scripted; keeps e2e real


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


# -- Anthropic's two thinking shapes ------------------------------------------


class _Support:
    def __init__(self, supported: bool):
        self.supported = supported


def _caps(*, enabled: bool, adaptive: bool, effort: bool):
    """A stand-in for models.retrieve(...).capabilities."""
    thinking = type(
        "T",
        (),
        {
            "types": type(
                "Ty", (), {"enabled": _Support(enabled), "adaptive": _Support(adaptive)}
            )()
        },
    )()
    return type("C", (), {"thinking": thinking, "effort": _Support(effort)})()


def _stub_anthropic(monkeypatch, capabilities, calls=None):
    import anthropic

    class _Models:
        def retrieve(self, model_id):
            if calls is not None:
                calls.append(model_id)
            if isinstance(capabilities, Exception):
                raise capabilities
            return type("M", (), {"capabilities": capabilities})()

    class _Client:
        models = _Models()

    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: _Client())


@pytest.fixture(autouse=True)
def _clear_thinking_cache():
    from nontainer_studio import providers

    for store in (providers._THINKING_CACHE, providers._thinking_failed_at):
        store.clear()
    yield
    for store in (providers._THINKING_CACHE, providers._thinking_failed_at):
        store.clear()


def test_adaptive_models_get_adaptive_and_effort(monkeypatch):
    """The bug: every Anthropic model was sent
    thinking={"type": "enabled"}, which claude-sonnet-5 rejects with a
    hard 400 on the first turn."""
    from nontainer_studio.providers import _anthropic_thinking

    _stub_anthropic(monkeypatch, _caps(enabled=False, adaptive=True, effort=True))
    assert _anthropic_thinking("claude-sonnet-5") == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }


def test_legacy_models_keep_the_budget_shape(monkeypatch):
    """claude-opus-4-5 and haiku-4-5 support only `enabled`, so this
    cannot simply be swapped for adaptive everywhere."""
    from nontainer_studio.providers import _anthropic_thinking

    _stub_anthropic(monkeypatch, _caps(enabled=True, adaptive=False, effort=False))
    assert _anthropic_thinking("claude-haiku-4-5") == {
        "thinking": {"type": "enabled", "budget_tokens": 4096}
    }


def test_adaptive_without_effort_omits_output_config(monkeypatch):
    from nontainer_studio.providers import _anthropic_thinking

    _stub_anthropic(monkeypatch, _caps(enabled=False, adaptive=True, effort=False))
    assert _anthropic_thinking("m") == {"thinking": {"type": "adaptive"}}


def test_a_model_without_thinking_gets_none(monkeypatch):
    from nontainer_studio.providers import _anthropic_thinking

    _stub_anthropic(monkeypatch, _caps(enabled=False, adaptive=False, effort=False))
    assert _anthropic_thinking("m") == {}


def test_a_failed_lookup_cannot_break_model_construction(monkeypatch):
    """The lookup is a network call on the construction path. If it
    could raise, an Anthropic outage or an offline box would stop the
    studio starting a session at all — a strictly worse failure than
    the one this fixes."""
    from nontainer_studio.providers import _anthropic_thinking

    _stub_anthropic(monkeypatch, RuntimeError("no network"))
    assert _anthropic_thinking("claude-sonnet-5") == {
        "thinking": {"type": "enabled", "budget_tokens": 4096}
    }


def test_effort_is_overridable(monkeypatch):
    from nontainer_studio.providers import _anthropic_thinking

    monkeypatch.setenv("NONTAINER_STUDIO_EFFORT", "high")
    _stub_anthropic(monkeypatch, _caps(enabled=False, adaptive=True, effort=True))
    assert _anthropic_thinking("m")["output_config"] == {"effort": "high"}


def test_the_capability_lookup_is_cached(monkeypatch):
    """One metadata call per model id, not one per turn."""
    from nontainer_studio.providers import _anthropic_thinking

    calls: list[str] = []
    _stub_anthropic(
        monkeypatch, _caps(enabled=False, adaptive=True, effort=True), calls
    )
    for _ in range(3):
        _anthropic_thinking("claude-sonnet-5")
    _anthropic_thinking("claude-opus-5")
    assert calls == ["claude-sonnet-5", "claude-opus-5"]


def test_a_failed_lookup_is_not_cached_as_an_answer(monkeypatch):
    """The P1 from PR #11 review. The fallback is a GUESS, and for an
    adaptive-only model like the default claude-sonnet-5 it is the WRONG
    guess — so caching it turns one DNS blip into a studio that 400s on
    every turn until the process restarts. Exactly the failure the
    fallback exists to prevent, arrived at the long way round."""
    from nontainer_studio import providers
    from nontainer_studio.providers import _anthropic_thinking

    calls: list[str] = []
    _stub_anthropic(monkeypatch, RuntimeError("dns blip"), calls)
    assert _anthropic_thinking("claude-sonnet-5") == {
        "thinking": {"type": "enabled", "budget_tokens": 4096}
    }
    assert "claude-sonnet-5" not in providers._THINKING_CACHE

    # ...and once the endpoint recovers (past the retry TTL) the real
    # shape is picked up without a restart.
    monkeypatch.setattr(providers, "_META_RETRY_SECONDS", 0.0)
    _stub_anthropic(
        monkeypatch, _caps(enabled=False, adaptive=True, effort=True), calls
    )
    assert _anthropic_thinking("claude-sonnet-5") == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }
    assert len(calls) == 2  # it really did ask again


def test_a_failed_lookup_is_not_retried_on_every_call(monkeypatch):
    """The other half of the TTL: a genuinely offline box must not pay
    the lookup on every model construction."""
    from nontainer_studio.providers import _anthropic_thinking

    calls: list[str] = []
    _stub_anthropic(monkeypatch, RuntimeError("offline"), calls)
    for _ in range(4):
        _anthropic_thinking("claude-sonnet-5")
    assert calls == ["claude-sonnet-5"]
