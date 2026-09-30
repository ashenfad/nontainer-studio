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
        "claude-sonnet-5-5",
        ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5"],
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
        "anthropic/claude-sonnet-5.5",
        [
            "anthropic/claude-sonnet-5.5",
            "anthropic/claude-opus-5.5",
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
        providers.append({"name": name, "default": default_model, "models": models})
    if default == "dummy":
        providers.append({"name": "dummy", "default": "dummy", "models": ["dummy"]})
    return {"providers": providers, "default": default}


def _sanitize_tool_calls(messages: list) -> list:
    """Two provider-corruption repairs, both replay poison:

    - Id-only stubs (``{'id': ...}``, no ``function``) — recorded when
      streamed arguments never decoded at all. Dropped, with their
      paired error tool-results: replaying a stub 400s on EVERY
      provider ("function/type field required").
    - Calls whose ``arguments`` string isn't valid JSON — some
      providers' tool-format parsers mangle args server-side (seen:
      Parasail splitting a JS ``r =>`` arrow into a bogus key/value),
      agno keeps the raw string plus an error result, and strict
      providers then 400 on every subsequent request ("Expecting ','
      delimiter"), wedging the session. Arguments are normalized to
      ``{}`` so the pair replays validly and the model still sees
      agno's decode-error feedback."""
    import copy
    import json as _json

    def _args_ok(tc: dict) -> bool:
        fn = tc.get("function")
        if not isinstance(fn, dict):
            return True
        args = fn.get("arguments")
        if not isinstance(args, str) or not args.strip():
            return True
        try:
            _json.loads(args)
            return True
        except ValueError:
            return False

    dropped: set[str] = set()
    out = []
    for m in messages:
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            good = []
            fixed = False
            for tc in tool_calls:
                if isinstance(tc, dict) and "function" not in tc:
                    if tc.get("id"):
                        dropped.add(tc["id"])
                    fixed = True
                    continue
                if isinstance(tc, dict) and not _args_ok(tc):
                    tc = {**tc, "function": {**tc["function"], "arguments": "{}"}}
                    fixed = True
                good.append(tc)
            if fixed:
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
                    formatted["reasoning_details"] = _merge_reasoning_details(details)
                return formatted

        _safe_openrouter_cls = SafeOpenRouter
    return _safe_openrouter_cls


# provider -> assume vision when we can't ask (first-party catalogs are
# all multimodal; ollama/dummy get the safe text-only default)
_VISION_BY_PROVIDER = {"anthropic": True, "openai": True, "google": True}

# provider -> context window when we can't ask (conservative floors
# for the first-party catalogs)
_CONTEXT_BY_PROVIDER = {
    "anthropic": 200_000,
    "openai": 400_000,
    "google": 1_000_000,
}

_openrouter_meta: dict[str, tuple[bool, int | None, int | None]] | None = None
_openrouter_meta_failed_at: float | None = None
_META_RETRY_SECONDS = 60.0


def _openrouter_model_meta(model: str) -> tuple[bool, int | None]:
    """(vision, context_length) for an OpenRouter model — asks the
    models API once (public metadata; cached for the process). Unknown
    or unreachable -> (False, None): a wrongly-withheld screenshot
    degrades to a path mention, a wrongly-attached one kills the next
    call; an unknown context just gets the flat default.

    Failure is cached with a TTL, not forever: a DNS blip at first
    session creation shouldn't disable vision gating for the process
    lifetime — but a genuinely offline machine mustn't stall 10s on
    every call either (PR #1 review)."""
    global _openrouter_meta, _openrouter_meta_failed_at
    model = model.partition("@")[0]  # tag pins routing, not identity
    if _openrouter_meta is None:
        import time

        now = time.monotonic()
        if (
            _openrouter_meta_failed_at is not None
            and now - _openrouter_meta_failed_at < _META_RETRY_SECONDS
        ):
            return (False, None)
        try:
            import json
            import urllib.request

            with urllib.request.urlopen(
                "https://openrouter.ai/api/v1/models", timeout=10
            ) as r:
                data = json.load(r)
            _openrouter_meta = {
                m["id"]: (
                    "image"
                    in ((m.get("architecture") or {}).get("input_modalities") or []),
                    m.get("context_length"),
                    (m.get("top_provider") or {}).get("max_completion_tokens"),
                )
                for m in data.get("data", [])
            }
        except Exception:
            _openrouter_meta_failed_at = now
            return (False, None)
    return _openrouter_meta.get(model, (False, None))


#: How much one Claude response may generate, thinking included. Claude
#: 5.x thinks adaptively, with no budget to set, so a hard turn can think
#: for 15k tokens before its tool call; at the old 16,384 that tool call
#: never came (finish_reason "length"), and a response with no tool call
#: ends the agent's run. 64k leaves room for long thinking AND a large
#: file write, while still bounding a runaway response. Billing is for
#: tokens generated, not for the cap.
_CLAUDE_MAX_OUTPUT = 64_000
#: What a Claude gets when its own limit could not be looked up: the old
#: value, which every Claude accepts.
_CLAUDE_FALLBACK_OUTPUT = 16_384
_ANTHROPIC_MAX_OUTPUT: dict[str, int] = {}  # model id -> its own limit


def _claude_max_tokens(limit: int | None) -> int:
    """The response cap for a Claude whose own output limit is ``limit``
    (None when unknown): never more than the model takes, since an
    over-limit max_tokens fails every call."""
    if not limit:
        return _CLAUDE_FALLBACK_OUTPUT
    return min(_CLAUDE_MAX_OUTPUT, limit)


def _openrouter_max_output(model: str) -> int | None:
    """An OpenRouter model's output limit, from the catalog, or None."""
    entry = _openrouter_model_meta(model)
    return entry[2] if len(entry) > 2 else None


def _effort() -> str:
    """NONTAINER_STUDIO_EFFORT, else the default: one setting for Claude
    on either path."""
    return os.getenv("NONTAINER_STUDIO_EFFORT") or _DEFAULT_EFFORT


def supports_vision(spec: str | None) -> bool:
    """Whether the spec'd model accepts image input — gates screenshot
    attachment (WorkspaceTools(vision=...)): text-only models 400 on
    the call AFTER an image-bearing tool result."""
    try:
        provider, model = parse_spec(spec or default_spec())
    except (ValueError, SystemExit):
        return False
    if provider == "dummy":
        return True  # the scripted model tolerates anything; keep e2e real
    if provider == "openrouter":
        return _openrouter_model_meta(model)[0]
    return _VISION_BY_PROVIDER.get(provider, False)


def context_window(spec: str | None) -> int | None:
    """The spec'd model's context length in tokens, when knowable."""
    try:
        provider, model = parse_spec(spec or default_spec())
    except (ValueError, SystemExit):
        return None
    if provider == "openrouter":
        return _openrouter_model_meta(model)[1]
    return _CONTEXT_BY_PROVIDER.get(provider)


def compress_token_limit(spec: str | None) -> int | None:
    """The compaction high-water mark for this model: when the message
    stack crosses it, agno compresses older tool results IN A WAVE
    (one cache miss, then a stable prefix again — never a sliding
    window, which would bust the prompt cache every turn).

    Default: 60% of the model's context window, clamped to [32k, 250k]
    (the ceiling is a cost bound — a 1M-context model doesn't want
    600k-token turns). Unknown context -> 100k.
    NONTAINER_STUDIO_COMPRESS_TOKENS overrides (0/off disables)."""
    raw = os.getenv("NONTAINER_STUDIO_COMPRESS_TOKENS")
    if raw:
        if raw.strip().lower() in ("0", "off", "none", "false"):
            return None
        try:
            return max(1_000, int(raw))
        except ValueError:
            pass  # unparseable -> fall through to the default
    ctx = context_window(spec)
    if ctx is None:
        return 100_000
    return min(max(int(ctx * 0.6), 32_000), 250_000)


def _split_openrouter_tag(model: str) -> tuple[str, dict | None]:
    """``qwen/qwen3.6-35b-a3b@wandb/fp8`` -> the base model id plus an
    OpenRouter provider-routing pin. ``@slug`` pins the upstream
    provider (no fallbacks — an explicit pin means THAT provider);
    ``@slug/quant`` also pins the quantization."""
    base, sep, tag = model.partition("@")
    if not sep or not tag:
        return model, None
    slug, _, quant = tag.partition("/")
    routing: dict = {"order": [slug], "allow_fallbacks": False}
    if quant:
        routing["quantizations"] = [quant]
    return base, routing


# Anthropic's thinking parameter comes in two INCOMPATIBLE shapes, and
# which one a model takes is not derivable from its name:
#
#   enabled   thinking={"type": "enabled", "budget_tokens": N}
#   adaptive  thinking={"type": "adaptive"} + output_config={"effort": …}
#
# Queried from the models API rather than guessed, because the split
# does not follow the version numbers. As of 2026-08: opus-5/sonnet-5/
# fable-5 AND opus-4-7/opus-4-8 reject `enabled`; sonnet-4-6/opus-4-6
# take either; opus-4-5 takes only `enabled`; haiku-4-5 and sonnet-4-5
# support no effort at all. Any prefix rule short enough to write down
# gets at least one of those wrong, and the failure is a hard 400 at the
# first turn — '"thinking.type.enabled" is not supported for this model'.
_THINKING_CACHE: dict[str, dict[str, Any]] = {}  # ANSWERS, never guesses
_thinking_failed_at: dict[str, float] = {}  # model id -> last failure

# The analogue of the 4096-token budget this used to send: enough to
# reason, not enough to stall a chat turn. NONTAINER_STUDIO_EFFORT
# overrides ("low" | "medium" | "high" | "xhigh" | "max").
_DEFAULT_EFFORT = "medium"


def _anthropic_thinking(model_id: str) -> dict[str, Any]:
    """The thinking/output_config kwargs this model actually accepts.

    Falls back to the legacy `enabled` shape when the capability lookup
    fails (no network, an id the API doesn't know, an SDK too old to
    report capabilities): that is what every pre-adaptive model wants,
    and it keeps a metadata call from being able to break model
    construction outright.

    The fallback is a GUESS, so it is never cached as an answer — only
    successful lookups land in ``_THINKING_CACHE``. Caching it would
    turn one DNS blip into a permanently 400ing studio: sonnet-5 is
    adaptive-only, so the guess is wrong for it, and nothing would retry
    until the process restarted. Failures instead get the same TTL
    ``_openrouter_model_meta`` uses, for the same reason — retry soon,
    but don't re-stall on every call from an offline box."""
    if model_id in _THINKING_CACHE:
        return _THINKING_CACHE[model_id]

    import time

    now = time.monotonic()
    legacy: dict[str, Any] = {"thinking": {"type": "enabled", "budget_tokens": 4096}}
    failed_at = _thinking_failed_at.get(model_id)
    if failed_at is not None and now - failed_at < _META_RETRY_SECONDS:
        return legacy

    try:
        import anthropic

        # Bounded: this sits on the model-construction path, so an
        # unreachable endpoint must fail fast rather than hold up a
        # session behind the SDK's default timeout and retries.
        client = anthropic.Anthropic(timeout=10.0, max_retries=1)
        info = client.models.retrieve(model_id)
        limit = getattr(info, "max_tokens", None)
        if isinstance(limit, int):
            _ANTHROPIC_MAX_OUTPUT[model_id] = limit
        caps = info.capabilities
        types = caps.thinking.types
        if types.adaptive.supported:
            kwargs: dict[str, Any] = {"thinking": {"type": "adaptive"}}
            if caps.effort.supported:
                kwargs["output_config"] = {"effort": _effort()}
        elif types.enabled.supported:
            kwargs = legacy
        else:
            kwargs = {}  # a model with no extended thinking at all
    except Exception:
        _thinking_failed_at[model_id] = now
        return legacy

    _THINKING_CACHE[model_id] = kwargs
    _thinking_failed_at.pop(model_id, None)
    return kwargs


def build_model(spec: str | None = None, *, stream: bool = True) -> Any:
    """spec -> a constructed agno Model (None = server default), with
    the transient-failure policy applied (see ``_with_retries``).

    ``stream=False`` is for a caller that runs the model without
    streaming. The Anthropic SDK refuses such a request when its
    max_tokens could take over ten minutes (anything above about 21k),
    so a direct Claude built for one keeps the smaller fallback cap.
    """
    return _with_retries(_construct_model(spec, stream=stream))


# Retry AT THE MODEL CALL, not at the run. agno has two retry layers and
# they mean very different things for a tool-using turn:
#
#   Model.retries  (models/base.py) wraps one invoke/stream. The retried
#     call re-sends the SAME in-flight message list, so every tool result
#     the turn has produced so far survives — "retry the last step".
#   Agent.retries  (agent/_run.py) wraps the WHOLE run: attempt > 0
#     re-reads the session from the db and rebuilds the messages from
#     persisted history + the original user message. The failed attempt's
#     tool calls were never persisted, so they are simply GONE.
#
# A long build turn can be 40 tool calls deep when a provider drops the
# stream; restarting it from the prompt loses all of that AND starts the
# model blind while its work still sits in the workspace — it then writes
# a second, divergent implementation over the first. So the fine layer is
# the only retry; Agent.retries stays at 0, and a run that still fails is
# resumed where it stopped rather than restarted (see the Agent
# construction in sessions.py).
#
# Only ModelProviderError routes through this, and agno declines to retry
# 400/401/403/404/413/422 and context-window overflow — a transient 429 or
# 5xx retries, a malformed request fails fast. Caveat: a stream that dies
# mid-flight restarts that call's stream from its start, so deltas already
# emitted can repeat inside one reply. That is one model call's worth of
# duplicated prose against a whole turn's worth of lost work.
_RETRIES = 2
_RETRY_DELAY = 2


def _with_retries(model: Any) -> Any:
    """Apply the model-call retry policy. Set as attributes rather than
    constructor kwargs: every branch below builds a different class, and
    these are plain ``Model`` dataclass fields on all of them."""
    for field, value in (
        ("retries", _RETRIES),
        ("delay_between_retries", _RETRY_DELAY),
        ("exponential_backoff", True),
    ):
        if hasattr(model, field):
            setattr(model, field, value)
    return model


#: Claude caches a prompt only when asked; the other providers studio
#: drives (OpenAI, DeepSeek, Gemini, and most of what OpenRouter routes
#: to) cache on their own. A top-level ``cache_control`` is Anthropic's
#: automatic mode: each request caches its whole prefix (tools, system
#: prompt, conversation so far), and the next turn of an agent loop
#: reads it back at a tenth of the input price, after paying 1.25x once
#: to write it. The same field works on the direct API and through
#: OpenRouter. Measured 2026-09-30 with sonnet-5 on a ~22.5k-token
#: prompt: with it, the second call read all of it from cache; without
#: it, nothing ever was.
_CLAUDE_CACHE: dict[str, Any] = {"cache_control": {"type": "ephemeral"}}


def _construct_model(spec: str | None = None, *, stream: bool = True) -> Any:
    """spec -> a constructed agno Model (None = server default)."""
    provider, model = parse_spec(spec or default_spec())
    if provider == "dummy":
        from .dummy import DummyModel

        return DummyModel()
    if provider == "anthropic":
        from agno.models.anthropic import Claude

        # native extended thinking, streamed into the transcript's
        # thinking blocks. The parameter SHAPE is per-model and looked
        # up, not assumed (see _anthropic_thinking); with the legacy
        # shape the budget must stay under max_tokens.
        thinking = _anthropic_thinking(model)  # also learns the model's limit
        max_tokens = _claude_max_tokens(_ANTHROPIC_MAX_OUTPUT.get(model))
        if not stream:
            max_tokens = min(max_tokens, _CLAUDE_FALLBACK_OUTPUT)
        return Claude(
            id=model,
            max_tokens=max_tokens,
            request_params={"extra_body": dict(_CLAUDE_CACHE)},
            **thinking,
        )
    if provider == "openai":
        # gpt-5.6 rejects tools + reasoning on chat-completions (same
        # restriction we hit via OpenRouter) — ride the Responses API,
        # which also streams reasoning summaries into thinking blocks
        if model.startswith("gpt-5.6"):
            from agno.models.openai import OpenAIResponses

            return OpenAIResponses(
                id=model, max_output_tokens=16384, reasoning_summary="auto"
            )
        from agno.models.openai import OpenAIChat

        return OpenAIChat(id=model)
    if provider == "openrouter":
        # optional `@slug[/quant]` tag: pin the upstream provider
        model, pin = _split_openrouter_tag(model)
        # gpt-5.6 rejects tools + reasoning on chat-completions (and
        # OpenRouter injects a default reasoning effort) — those models
        # ride OpenRouter's Responses endpoint instead.
        if model.startswith("openai/gpt-5.6"):
            from agno.models.openrouter import OpenRouterResponses

            # reasoning summaries are all OpenAI exposes of its CoT
            return OpenRouterResponses(
                id=model,
                max_output_tokens=16384,
                reasoning_summary="auto",
                extra_body={"provider": pin} if pin else None,
            )
        extra_body = None
        max_tokens = 16384  # the agno default (1024) truncates real coding turns
        if model.startswith("anthropic/"):
            # Claude via OpenRouter doesn't reason unless asked. The
            # signed thinking blocks survive tool round-trips only
            # because SafeOpenRouter re-merges the streamed
            # reasoning_details fragments (see _merge_reasoning_details).
            #
            # Effort, not a token budget: Claude 5.x thinks adaptively and
            # ignores a budget (measured: 11.9k reasoning tokens against
            # a 4096 "budget"). Effort is what steers it, and is what the
            # direct path sends, from the same setting.
            extra_body = {"reasoning": {"effort": _effort()}, **_CLAUDE_CACHE}
            max_tokens = _claude_max_tokens(_openrouter_max_output(model))
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
        if pin:
            # an explicit @tag outranks curated routing (gemma defaults)
            extra_body = {**(extra_body or {}), "provider": pin}
        return _safe_openrouter()(
            id=model, max_tokens=max_tokens, extra_body=extra_body
        )
    if provider == "google":
        from agno.models.google import Gemini

        # thought summaries stream into the transcript's thinking blocks
        return Gemini(id=model, include_thoughts=True)
    if provider == "ollama":
        from agno.models.ollama import Ollama

        return Ollama(id=model)
    raise ValueError(f"unknown provider {provider!r}")
