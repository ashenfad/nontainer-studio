"""What the environment decides: where the store lives, which executor
runs agent code and how, the apps config, the delegation settings, and
the host objects only an agent session gets.

Each reader takes its variable when asked, so a test or an embedder can
set one before the thing it governs is built.
"""

from __future__ import annotations

import functools
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from nontainer import Workspace
from nontainer.apps import AppsConfig

from .prompts import (
    FRONTEND_NOTES,
    HANDLER_EXAMPLE,
)

DEFAULT_STORE = Path.home() / ".nontainer-studio"


def _executor_factory() -> Callable[[], Any] | None:
    """Select the execution backend from the environment.

    ``NONTAINER_STUDIO_EXECUTOR=dud`` runs agent code on a real
    machine with NO containment (dud's subprocess backend: real bash,
    real python, real files, running as you) instead of the in-process
    sandtrap+termish LocalExecutor. It buys fidelity, not isolation —
    for isolation without a VM, leave this unset. ``=dud-vm`` selects
    a real disposable microVM (vfkit on macOS, firecracker on
    Linux/KVM), so isolation is real; boots a ``python:slim``
    matched to the host interpreter (see ``_vm_config``) with the
    kernel from ``$DUD_KERNEL``/``~/.dud``, fails closed off macOS or
    without a kernel, and defaults the pool's VM budget (see
    ``_ensure_vm_cap``). Unset (the default) returns None — nontainer
    builds its LocalExecutor and studio behaves exactly as before. The
    dud import is lazy so the default install needs neither dud nor a
    nontainer new enough to accept ``executor_factory`` (see
    ``_ws_kwargs``).

    Caveat: the subprocess backend has NO isolation (own-machine
    posture). Apps dispatch works under dud — the live preview and
    ``test_app`` (both drive ``dispatch`` host-side) run, and the
    apps.md-recommended handler pattern (state in cache/an external
    store) crosses the boundary cleanly. The workspace's own verbs
    (``ws-curl``, ``ws-git``, ``ws-pytest``, ``ws-vitest``) relay out of
    the guest to the host that answers them, so the apps loop, the
    session's git and the unit-test tier are the same on every rung.
    They are spelled ``ws-`` because a real ``curl`` is on the guest's
    PATH and would reach the network instead of the app.

    One gap remains: absolute paths under the SUBPROCESS backend live
    in the guest's own temp dir rather than ``/workspace``. The VM
    backends close it — they mount the workspace AT ``/workspace``, so
    absolute paths match the local sandbox everywhere else.

    The analyst loop (terminal + run_python over the real data stack)
    is unaffected.
    """
    choice = os.getenv("NONTAINER_STUDIO_EXECUTOR", "").lower()
    if choice not in ("dud", "dud-vm"):
        return None
    from nontainer.executor_dud import DudExecutor

    if choice == "dud-vm":
        _ensure_vm_cap()
        vm = _vm_config()
        # "vm", not "vfkit": dud resolves the rung per platform, so this
        # works on Linux/KVM too. Pinning a hypervisor here would defeat
        # the alias that exists precisely to avoid that.
        return lambda: DudExecutor(backend="vm", vm=vm)
    # Explicit: DudExecutor defaults to a VM now, and =dud means the
    # zero-isolation rung by deliberate choice (warned about at startup).
    return lambda: DudExecutor(backend="subprocess")


def apps_config() -> AppsConfig:
    """The ONE ``AppsConfig`` for this process.

    It governs two lifecycles that must not disagree: **authoring**
    (``enable_apps`` — test_app's request interception, the budgets a
    handler runs under, and what the agent is told in its tool
    description) and **serving** (``build_router`` — the CSP a published
    snapshot carries, and which static assets exist there).

    Studio used to build one at each site and let both fall through to
    the library defaults, so they agreed by luck rather than by
    construction. That is the shape apps.md's "one declaration, four
    surfaces" rule exists to prevent: customize ``script_hosts`` on the
    authoring side alone and an app verifies green under test_app, then
    breaks published under a CSP that never heard about the change.
    Nothing in nontainer forced the split — it is studio's discipline to
    keep, so it is kept here, once.

    The frontend stack is VENDORED (see ``appassets/``): MUI with React
    and JSX, plotly, and tailwind, all served from the app's own origin,
    so an agent on a locally-hosted model with no internet still gets a
    page that renders. ``static_assets`` puts the bytes in place and
    ``frontend_notes`` says they exist — the pair is one decision, since
    a library the agent isn't told about may as well not be here.

    ``script_hosts`` is empty: an app's scripts load from the app's own
    origin and nowhere else, under test_app and when published alike.
    Everything an agent is told to use is vendored, so a CDN tag would
    only ever be a stray one, and a stray one that worked in the preview
    and failed on an air-gapped machine is the failure this rule exists
    to make impossible; the notes tell the agent scripts may load only
    from the app itself.
    """
    return AppsConfig(
        static_assets={"vendor": app_assets_dir()},
        frontend_notes=FRONTEND_NOTES,
        handler_example=HANDLER_EXAMPLE,
        csp=_csp(),
        script_hosts=(),
    )


def _csp() -> str | None:
    """``NONTAINER_STUDIO_CSP``: override the app policy, or ``"none"``
    to drop it. ``None`` leaves nontainer to derive one from
    ``script_hosts``.

    On the CONFIG, not on ``build_router``. Since nontainer 0.3.5
    test_app sends this policy during verification, so a policy declared
    only at the router would be one verification never sees — an app
    could pass under the derived default and be served under this. That
    is the divergence the single config exists to prevent, and it was
    live here until 0.3.5 made the field available.

    Setting it still breaks the link to ``script_hosts`` on purpose: an
    explicit policy is used verbatim, so it must carry the script hosts
    itself — and ``'wasm-unsafe-eval'`` if any vendored library has a
    wasm core."""
    csp = os.getenv("NONTAINER_STUDIO_CSP")
    if csp is None:
        return None  # derived from script_hosts
    return "" if csp.lower() == "none" else csp


def app_assets_dir() -> Path:
    """Where the vendored browser libraries live.

    ``NONTAINER_STUDIO_APP_ASSETS`` overrides, mirroring
    ``NONTAINER_STUDIO_SKILLS`` — an embedder swapping in its own design
    system replaces the directory and the notes together."""
    override = os.getenv("NONTAINER_STUDIO_APP_ASSETS")
    return Path(override) if override else Path(__file__).parent / "appassets"


def _view_workers() -> int:
    """``NONTAINER_STUDIO_VIEW_WORKERS``, the warm app-handler pool size.

    A cache size, not a limit — nothing here caps how many workers a
    burst of concurrent requests can create. Default 0 (a pristine
    worker per handler call), which is only affordable because
    ``preload_grants`` makes one cheap; see ``_python_config``.
    Unparseable or negative values fall back to the default rather than
    raising, matching how ISOLATION handles a bad value.
    """
    try:
        return max(0, int(os.getenv("NONTAINER_STUDIO_VIEW_WORKERS", "0")))
    except ValueError:
        return 0


def _delegate_ttl_hours() -> float:
    """``NONTAINER_STUDIO_DELEGATE_TTL``, how long a delegate's branch
    is retained, in hours.

    Retention for a delegate's branch is an idle TTL: a delegate
    nobody has dealt with for this long has its branch deleted and its
    answer dropped, and reading an answer or keeping the job is
    dealing with it. Default 24 hours. ``0`` disables the sweep
    outright — every branch an agent ever forked stays until a human
    deletes the session that asked.

    The number is HOURS because that is the unit the decision is made
    in ("overnight", "a working day"), and the agent is told it in the
    same unit (see :func:`_retention_primer`). Unparseable or negative
    values fall back to the default rather than raising, matching how
    the other settings handle a bad value.
    """
    try:
        hours = float(os.getenv("NONTAINER_STUDIO_DELEGATE_TTL", "24"))
    except ValueError:
        return 24.0
    return max(0.0, hours)


def _delegate_depth() -> int:
    """``NONTAINER_STUDIO_DELEGATE_DEPTH``, how deep delegation may
    nest.

    A delegate is a full agent on the same model, with delegate
    workers of its own, so nesting multiplies rather than adds: with
    nothing bounding it, one ask can grow a tree nobody asked for on
    the asker's budget. The cap counts hops from the session a human
    started, and it is checked when a delegate asks for one of its
    own: ``2`` lets that session delegate and lets its delegates
    delegate, and refuses the generation after them. ``0`` turns the
    cap off. Unparseable or negative values fall back to the default
    rather than raising, matching how the other settings handle a bad
    value.
    """
    try:
        return max(0, int(os.getenv("NONTAINER_STUDIO_DELEGATE_DEPTH", "2")))
    except ValueError:
        return 2


def _delegate_tool_calls() -> int:
    """``NONTAINER_STUDIO_DELEGATE_TOOL_CALLS``, how many tool calls a
    delegate may spend in one turn.

    A turn is one agno run and one agno run is a tool loop with no
    bound of its own; a delegate's loop has no human watching it and
    no stop button over it, so a delegate that has found a rhythm it
    cannot break out of spends the session's budget on it. Default 60,
    which is generous for a real task and short of a loop. ``0``
    turns the cap off. Unparseable or negative values fall back to the
    default rather than raising, matching how the other settings
    handle a bad value.
    """
    try:
        return max(0, int(os.getenv("NONTAINER_STUDIO_DELEGATE_TOOL_CALLS", "60")))
    except ValueError:
        return 60


def _delegate_wakes() -> int:
    """``NONTAINER_STUDIO_DELEGATE_WAKES``, how many turns delegates'
    answers may start on a session before the human says anything.

    An answer that lands while the session is idle starts a turn for it
    (see ``Registry.may_wake``), so a parent can merge what is done and
    re-ask what failed without the human nudging it. A woken turn can
    delegate again, and that delegate's answer wakes it again, with
    nobody in the loop; the count bounds that, and every message the
    human sends refills it. Default 10. ``0`` turns waking off: answers
    then wait for the human's next message. Unparseable or negative
    values fall back to the default rather than raising, matching how
    the other settings handle a bad value.
    """
    try:
        return max(0, int(os.getenv("NONTAINER_STUDIO_DELEGATE_WAKES", "10")))
    except ValueError:
        return 10


def _flag(name: str) -> bool:
    """An on/off environment knob. ``1``/``true``/``yes``/``on`` (in any
    case) turn it on; anything else, including unset, leaves it off."""
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def wsgit_enabled() -> bool:
    """Whether the agent's terminal carries the ``ws-git`` verb:
    ``NONTAINER_STUDIO_WSGIT`` asks for it, and
    ``NONTAINER_STUDIO_SESSIONS`` asks for it too.

    Delegation without the verb is the degraded half of itself — a
    delegate's files stay on its own branch and its answer is all that
    ever comes back — so a studio told to hand an agent the `sessions`
    tool is told to hand it what brings a delegate's work over.
    Versioning without delegation is a state worth having, so the verb
    alone still means exactly the verb.

    Off, ``register_wsgit`` is never called, so the verb is absent from
    the terminal and the primer teaches no spelling for it. The
    workspace is versioned either way — every mutating tool call still
    commits, and the human's rewind, fork, publish and restore are
    host-side verbs over that history — so this decides what the AGENT
    can type, nothing about what the studio can do.
    """
    return _flag("NONTAINER_STUDIO_WSGIT") or _flag("NONTAINER_STUDIO_SESSIONS")


def sessions_tool_enabled() -> bool:
    """``NONTAINER_STUDIO_SESSIONS``: whether the agent is given the
    ``sessions`` tool, its handle on delegation and on what the human
    has published.

    On, it also turns the ``ws-git`` verb on, because a delegate's work
    comes back through that verb and nothing else does
    (:func:`wsgit_enabled`).

    Off, the tool is not registered and the primer says nothing about
    delegating or about published apps. The session's ``Sessions``
    helper is built regardless: the retention sweep, the drill-down
    routes and the delegates listing all read it, and with no tool to
    fork through they simply find nothing new.
    """
    return _flag("NONTAINER_STUDIO_SESSIONS")


def _ensure_vm_cap() -> None:
    """Default dud's VM budget for the studio's long-running posture.

    Studio never closes sessions during a run, so under dud-vm every
    session ever touched holds one bound VM for the process lifetime
    (and each publish resolve holds another) — unbounded RAM growth
    over a day of session switching. dud's pool bounds exactly this
    when ``DUD_VM_MAX_TOTAL`` is set: past the cap it reclaims the
    longest-quiet VM (idle first, then LRU bound), and the reclaimed
    session's owner transparently recovers on its next call
    (``SessionLost`` → re-acquire from the warm pool + re-push, ~a
    second) — the disposable thesis as an eviction policy. The pool
    reads the env once, at construction, so both entry points that can
    build it (prewarm at server start, the session factory) default it
    here first; an operator's own value always wins."""
    os.environ.setdefault("DUD_VM_MAX_TOTAL", "4")


def _vm_config() -> dict[str, Any]:
    """The dud-vm boot config sessions run on (also the prewarm target).

    The VM boots bare python:slim, so studio's data stack has to be
    layered into the guest image (dud fetches guest-arch wheels).
    Versions are PINNED to this venv's: cache values are pickles, and
    sessions authored on one executor get read on the other — an
    unpinned guest resolved pandas 2.x against a 3.x host and cached
    DataFrames failed to unpickle (Categorical __setstate__). The
    guest IMAGE tracks the host interpreter's minor for the same
    reason (pickle portability spans package versions AND the
    interpreter) — and so the pinned versions always have wheels for
    the guest's python: a 3.13 host pinning a version with no cp312
    wheel would brick the image build against a hardcoded 3.12 guest.

    ``medium`` defaults to ``auto``: with packages layered in, dud
    resolves that to an erofs root — demand-paged, so guest RAM is
    pages touched (~80 MB at boot) instead of a ~400 MB RAM-resident
    initramfs, and boots skip the unpack. ``memory_mib`` stays high as
    a CEILING (VZ allocates lazily; an erofs guest won't use it).
    ``NONTAINER_STUDIO_VM_MEDIUM`` overrides (e.g. ``initramfs`` to
    fall back). The first session builds+caches the image; later
    sessions and restarts reuse it (see ``start_vm_prewarm``).
    """
    import importlib.metadata as _md

    # `nontainer` is here for one reason: dud resolves `outputs_hook`
    # as an ordinary import INSIDE the guest, and the hook is
    # `nontainer.dud_outputs:flatten`. Without it in the image a rich
    # `ui` value never becomes an artifact on this rung -- and it fails
    # silently, because a `ui` dict holding a DataFrame is simply
    # unrepresentable and gets dropped whole. Cheap: 7 pure-python
    # wheels, ~2 MB, against an image that already carries pandas.
    #
    # Pinned to the HOST's version like the rest, and for a sharper
    # reason here: the guest hook and the host executor agree on a wire
    # shape (the artifact claim), so a guest running a different
    # nontainer than the host is a contract skew, not just a version
    # drift. Note that an EDITABLE host checkout ahead of PyPI cannot be
    # matched -- the guest gets the published build of that version.
    packages = []
    for name in ("nontainer", "numpy", "pandas", "pyarrow", "matplotlib", "plotly"):
        try:
            packages.append(f"{name}=={_md.version(name)}")
        except _md.PackageNotFoundError:
            pass  # not installed host-side -> not granted guest-side
    return {
        "image": f"python:3.{sys.version_info.minor}-slim",
        "packages": packages,
        "memory_mib": 4096,
        "medium": os.getenv("NONTAINER_STUDIO_VM_MEDIUM", "auto"),
    }


def _bake_image(cfg: dict[str, Any]) -> None:
    """Eagerly build (only) the guest image for ``cfg`` — no VM booted.

    Best-effort, same posture as prewarm: a failure here surfaces
    later, on a session's first boot, with its usual error."""
    try:
        from dud.images import build as build_rootfs

        build_rootfs(cfg["image"], packages=cfg["packages"], medium=cfg["medium"])
    except Exception:
        pass


def start_vm_prewarm() -> "threading.Thread | None":
    """Eagerly prep VMs at server start (dud-vm only, no-op otherwise).

    Every studio session shares one boot config, so the image is fully
    determined at startup — there is never a reason to make the first
    user pay the cold build. ``NONTAINER_STUDIO_VM_WARM`` picks how
    eager:

    - ``>= 1`` (default 1): boot-and-park that many warm VMs; the
      first thing a boot does is build the image, so a cold cache gets
      built at startup too. A session's first boot takes a parked VM
      and skips the boot.
    - ``0``: no idle VM RAM — but still bake the image in a background
      thread, so a first boot pays boot-only, never build+boot.

    A session boots its VM on its first turn, not when it is opened
    (see ``server._warm``). Studio never closes sessions during a run,
    so dud's pool would otherwise sit empty until shutdown — the first
    turn in each session after a restart would pay a full boot."""
    if os.getenv("NONTAINER_STUDIO_EXECUTOR", "").lower() != "dud-vm":
        return None
    _ensure_vm_cap()  # before the pool exists — it reads the env once
    cfg = _vm_config()
    n = int(os.getenv("NONTAINER_STUDIO_VM_WARM", "1"))
    if n > 0:
        from dud.backends.pool import shared_pool

        shared_pool().prewarm(n, **cfg)  # background by default
        return None
    import threading

    t = threading.Thread(
        target=lambda: _bake_image(cfg), name="dud-image-bake", daemon=True
    )
    t.start()
    return t


def _ws_kwargs() -> dict[str, Any]:
    """Executor kwarg for ``Store.open()``, added ONLY when a custom
    backend is selected — so the default path opens a session with
    nothing but its python config, and nontainer picks the executor."""
    factory = _executor_factory()
    return {"executor_factory": factory} if factory is not None else {}


#: The Python timeout of an agent session with host objects that call
#: out: above the slowest such call (a deep search's 150s ceiling), while
#: still bounding a runaway loop.
AGENT_PYTHON_TIMEOUT = 180.0


def _agent_host_objects() -> dict[str, Any]:
    """The host objects only an agent session gets, keyed by name."""
    from .media import Media, media_enabled
    from .web import web_enabled

    objects: dict[str, Any] = {}
    if web_enabled():
        objects["web"] = _shared_web()
    if media_enabled():
        # One per session: it writes into the session's workspace, and
        # is bound to it once that is open (see _bind_host_objects). The
        # connection pool is the process's.
        objects["media"] = Media(_shared_media_client())
    return objects


@functools.cache
def _shared_media_client() -> Any:
    """One OpenRouter client for every session's ``Media``, so a session
    that closes leaves no connections of its own behind."""
    from .media import make_client

    return make_client(os.environ["OPENROUTER_API_KEY"])


def _bind_host_objects(ws: Workspace) -> None:
    """Hand the workspace to the host objects that write into it. They
    are built with the Python config, before the workspace exists."""
    for obj in ws.runtime.python_config.host_objects.values():
        bind = getattr(obj, "_bind", None)
        if callable(bind):
            bind(ws)


def _unbind_host_objects(ws: Workspace) -> None:
    """Take the workspace back from them as it closes, so a closed
    session's workspace is not kept alive by its own config."""
    for obj in ws.runtime.python_config.host_objects.values():
        unbind = getattr(obj, "_unbind", None)
        if callable(unbind):
            unbind()


@functools.cache
def _shared_web() -> Any:
    """One ``Web`` for the process: it holds a connection pool and no
    per-session state."""
    from .web import Web

    return Web(os.environ["OPENROUTER_API_KEY"])
