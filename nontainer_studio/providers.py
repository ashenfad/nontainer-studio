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
        "gpt-5.4-mini",
        ["gpt-5.4-mini", "gpt-5.4", "gpt-5"],
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
            "deepseek/deepseek-v4-flash",
            "moonshotai/kimi-k2.6",
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


def build_model(spec: str | None = None) -> Any:
    """spec -> a constructed agno Model (None = server default)."""
    provider, model = parse_spec(spec or default_spec())
    if provider == "dummy":
        from .dummy import DummyModel

        return DummyModel()
    if provider == "anthropic":
        from agno.models.anthropic import Claude

        return Claude(id=model)
    if provider == "openai":
        from agno.models.openai import OpenAIChat

        return OpenAIChat(id=model)
    if provider == "openrouter":
        from agno.models.openrouter import OpenRouter

        # the agno default (1024) truncates real coding turns
        return OpenRouter(id=model, max_tokens=16384)
    if provider == "google":
        from agno.models.google import Gemini

        return Gemini(id=model)
    if provider == "ollama":
        from agno.models.ollama import Ollama

        return Ollama(id=model)
    raise ValueError(f"unknown provider {provider!r}")
