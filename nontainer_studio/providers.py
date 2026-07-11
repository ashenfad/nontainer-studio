"""Provider registry: which LLM backends this server can drive.

Availability is DETECTED, not configured: a provider is offered when
its env key is present (and its SDK importable). Keys live in the
server's environment only — the browser picks models, never touches
credentials.

Model specs are ``provider:model`` strings (``openrouter:deepseek/
deepseek-v4-flash``), with shorthands: a bare provider name means its
default model; a bare model id means the default provider (legacy
NONTAINER_STUDIO_MODEL values keep working). ``dummy`` is the scripted
test model (see dummy.py) — always buildable, only advertised when
it's the configured default.
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any

# name -> (env key, sdk module, default model, curated picks)
_PROVIDERS: dict[str, tuple[str, str, str, list[str]]] = {
    "anthropic": (
        "ANTHROPIC_API_KEY",
        "anthropic",
        "claude-sonnet-5",
        ["claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5"],
    ),
    "openai": (
        "OPENAI_API_KEY",
        "openai",
        "gpt-5.6-sol",
        [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.4-mini",
            "gpt-5.4",
        ],
    ),
    "openrouter": (
        "OPENROUTER_API_KEY",
        "openai",  # OpenRouter rides the openai SDK (OpenAILike)
        "anthropic/claude-sonnet-5",
        [
            "anthropic/claude-sonnet-5",
            "anthropic/claude-opus-4.8",
            "openai/gpt-5.6-luna",
            "openai/gpt-5.6-sol",
            "google/gemini-2.5-pro",
            "google/gemma-4-26b-a4b-it",
            "qwen/qwen3.6-35b-a3b",
            "z-ai/glm-5.2",
        ],
    ),
    "google": (
        "GOOGLE_API_KEY",
        "google.genai",
        "gemini-2.5-pro",
        ["gemini-2.5-pro", "gemini-2.5-flash"],
    ),
    "ollama": (
        "OLLAMA_HOST",  # opt-in: point at your daemon (usually :11434)
        "ollama",
        "llama3.3",
        [],
    ),
}

# detection order when no default is configured
_ORDER = ["anthropic", "openai", "openrouter", "google", "ollama"]


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def parse_spec(spec: str | None) -> tuple[str, str]:
    """Normalize any accepted spec form to (provider, model)."""
    if not spec:
        raise ValueError("empty model spec")
    if spec == "dummy":
        return "dummy", "dummy"
    if ":" in spec:
        provider, _, model = spec.partition(":")
        if provider not in _PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        return provider, model or _PROVIDERS[provider][2]
    if spec in _PROVIDERS:
        return spec, _PROVIDERS[spec][2]
    # legacy: a bare model id rides the default provider
    return _detect_provider(), spec


def canonical(spec: str | None) -> str:
    provider, model = parse_spec(spec or default_spec())
    return "dummy" if provider == "dummy" else f"{provider}:{model}"


def _detect_provider() -> str:
    for name in _ORDER:
        env, sdk, _, _ = _PROVIDERS[name]
        if os.getenv(env) and _installed(sdk):
            return name
    raise SystemExit(
        "No LLM provider available. Set one of: "
        + ", ".join(_PROVIDERS[n][0] for n in _ORDER)
        + " (or NONTAINER_STUDIO_MODEL=dummy for the scripted test model)."
    )


def default_spec() -> str:
    """The server's default model spec: NONTAINER_STUDIO_MODEL if set
    (any accepted form), else the first available provider's default."""
    configured = os.getenv("NONTAINER_STUDIO_MODEL")
    if configured:
        return canonical(configured)
    return canonical(_detect_provider())


def available() -> dict:
    """What the picker shows: providers whose key + SDK are present.
    The dummy provider is advertised only when it's the default (it's
    a test double, not a product surface)."""
    default = default_spec()
    providers = []
    for name in _ORDER:
        env, sdk, default_model, models = _PROVIDERS[name]
        if not (os.getenv(env) and _installed(sdk)):
            continue
        providers.append(
            {"name": name, "default": default_model, "models": models}
        )
    if default == "dummy":
        providers.append({"name": "dummy", "default": "dummy", "models": ["dummy"]})
    return {"providers": providers, "default": default}


def _sanitize_tool_calls(messages: list) -> list:
    """Drop the id-only tool-call stubs agno records when a provider
    streams undecodable arguments (gemma's token-level tool format is
    parsed provider-side, and truncated parses arrive as ``{'id': ...}``
    with no ``function``) — plus their paired error tool-results.
    Replaying a stub 400s on EVERY provider ("function/type field
    required"), killing the rest of the turn. Dropping it instead lets
    the model see its valid calls' results and retry the failed one."""
    import copy

    dropped: set[str] = set()
    out = []
    for m in messages:
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            good = [
                tc
                for tc in tool_calls
                if not isinstance(tc, dict) or "function" in tc
            ]
            if len(good) != len(tool_calls):
                for tc in tool_calls:
                    if isinstance(tc, dict) and "function" not in tc and tc.get("id"):
                        dropped.add(tc["id"])
                m = copy.copy(m)
                m.tool_calls = good or None
        if getattr(m, "tool_call_id", None) in dropped:
            continue
        out.append(m)
    return out


def _merge_reasoning_details(details: list) -> list:
    """OpenRouter streams ``reasoning_details`` as index-keyed FRAGMENTS
    (docs: preserving reasoning); agno accumulates them by naive list-
    extend, and replaying fragments breaks models with signed thinking
    blocks (Anthropic: "Invalid `signature` in `thinking` block").
    Merge fragments back into whole blocks: concatenate the text-ish
    fields per index, last non-empty wins for the rest."""
    merged: dict[Any, dict] = {}
    order: list[Any] = []
    for frag in details:
        if not isinstance(frag, dict):
            continue
        key = frag.get("index")
        if key is None or key not in merged:
            key = key if key is not None else f"pos-{len(order)}"
            merged[key] = dict(frag)
            order.append(key)
            continue
        block = merged[key]
        for field in ("text", "summary", "data"):
            if isinstance(frag.get(field), str):
                block[field] = (block.get(field) or "") + frag[field]
        for field in ("signature", "id", "format", "type"):
            if frag.get(field):
                block[field] = frag[field]
    return [merged[k] for k in order]


_safe_openrouter_cls: Any = None


def _safe_openrouter() -> Any:
    """OpenRouter subclass with malformed-tool-call sanitation (built
    lazily so importing this module never drags agno in)."""
    global _safe_openrouter_cls
    if _safe_openrouter_cls is None:
        from agno.models.openrouter import OpenRouter

        class SafeOpenRouter(OpenRouter):
            def _format_all_messages(self, messages, *args, **kwargs):  # type: ignore[override]
                return super()._format_all_messages(
                    _sanitize_tool_calls(messages), *args, **kwargs
                )

            def _format_message(self, message, *args, **kwargs):  # type: ignore[override]
                formatted = super()._format_message(message, *args, **kwargs)
                details = formatted.get("reasoning_details")
                if isinstance(details, list) and len(details) > 1:
                    formatted["reasoning_details"] = _merge_reasoning_details(
                        details
                    )
                return formatted

        _safe_openrouter_cls = SafeOpenRouter
    return _safe_openrouter_cls


def build_model(spec: str | None = None) -> Any:
    """spec -> a constructed agno Model (None = server default)."""
    provider, model = parse_spec(spec or default_spec())
    if provider == "dummy":
        from .dummy import DummyModel

        return DummyModel()
    if provider == "anthropic":
        from agno.models.anthropic import Claude

        # native extended thinking, streamed into the transcript's
        # thinking blocks (budget must stay under max_tokens)
        return Claude(
            id=model,
            thinking={"type": "enabled", "budget_tokens": 4096},
            max_tokens=16384,
        )
    if provider == "openai":
        from agno.models.openai import OpenAIChat

        return OpenAIChat(id=model)
    if provider == "openrouter":
        # gpt-5.6 rejects tools + reasoning on chat-completions (and
        # OpenRouter injects a default reasoning effort) — those models
        # ride OpenRouter's Responses endpoint instead.
        if model.startswith("openai/gpt-5.6"):
            from agno.models.openrouter import OpenRouterResponses

            # reasoning summaries are all OpenAI exposes of its CoT
            return OpenRouterResponses(
                id=model, max_output_tokens=16384, reasoning_summary="auto"
            )
        extra_body = None
        if model.startswith("anthropic/"):
            # Claude via OpenRouter doesn't reason unless asked. The
            # signed thinking blocks survive tool round-trips only
            # because SafeOpenRouter re-merges the streamed
            # reasoning_details fragments (see _merge_reasoning_details).
            extra_body = {"reasoning": {"max_tokens": 4096}}
        if model.startswith("google/gemma"):
            # gemma-4's native tool-call format (token-level, not JSON)
            # needs a provider-side parser, and quality varies wildly
            # (surveyed 2026-07 with 10-15KB file_write calls): Novita
            # doubled token pairs (`<div>` -> `<<divdiv>`, ~50%);
            # google-vertex silently truncates args at ~3.6KB while
            # reporting finish_reason=tool_calls; DeepInfra, Cloudflare,
            # and Venice were clean at 10-15KB.
            extra_body = {
                "provider": {
                    "order": ["deepinfra", "cloudflare"],
                    "ignore": ["novita", "google-vertex"],
                }
            }
        # the agno default (1024) truncates real coding turns
        return _safe_openrouter()(id=model, max_tokens=16384, extra_body=extra_body)
    if provider == "google":
        from agno.models.google import Gemini

        # thought summaries stream into the transcript's thinking blocks
        return Gemini(id=model, include_thoughts=True)
    if provider == "ollama":
        from agno.models.ollama import Ollama

        return Ollama(id=model)
    raise ValueError(f"unknown provider {provider!r}")
