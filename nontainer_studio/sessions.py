"""Session registry: one Workspace + AppRuntime + agno Agent + SQLite
store + event log per session.

Ownership model, on display:

- WORKSPACE (files, cache, cwd): durable and versioned — a kvgit
  branch per session; an edit's rewind applies here.
- APP DB (``db`` host object): durable but HISTORYLESS — live app
  state that never time-travels. Fresh per session, untouched by
  rewinds. A published app gets a COPY at its first version and owns
  it from then on, so the two universes stop writing over each other
  the moment there are two.
- CONVERSATION: durable and versioned WITH the files — agno's session
  lives in the session's own kvgit branch (one ``KvgitStoreDb`` over
  the store, a branch per session), so a turn's files, cache, cwd and
  the agent's memory land in ONE commit and one ``ws.restore`` rewinds
  all four. agno's cross-session tables (memories, metrics) live at
  ``store/agno`` and never version — world state, not session state.
- EVENT LOG: durable but session-scoped — the transcript appends to a
  jsonl per session. An EDIT (rewind_to_event) trims the visible
  transcript too, via an appended `truncate` event, never by mutating
  the log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import petname
from nontainer import (
    PythonConfig,
    Workspace,
    delete_workspace,
    validate_session_id,
    workspace,
)
from nontainer.adapters.agno import WorkspaceTools
from nontainer.adapters.agno_db import KvgitStoreDb, fork_session
from nontainer.apps import AppRuntime, AppsConfig, enable_apps, mint_token

log = logging.getLogger(__name__)

DEFAULT_STORE = Path.home() / ".nontainer-studio"

APP_BRANCH = "_apps"
"""The reserved branch published apps are served THROUGH.

``at_tag`` reads a name off the store, and a name in the store scope
belongs to no session — so the lookup needs a workspace that stands for
no session either. This one does: it is never in the manifest, so it is
never in the rail, never opened as a session, and never deleted with
one; nothing is ever committed to it beyond the empty baseline opening a
branch writes."""

MAX_EVENTS = 10_000  # in-MEMORY tail window, not a lifetime cap


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
    posture). Apps dispatch works under dud as of stage 3c — the live
    preview and ``test_app`` (both drive ``dispatch`` host-side) run,
    and the apps.md-recommended handler pattern (state in cache/an
    external store) crosses the boundary cleanly. Two gaps remain:

    - ``curl`` is a termish command and doesn't exist in dud's real
      bash — on ANY dud backend, not just the subprocess one. Worse
      than absent: real curl IS on the guest PATH, so a `curl api/x`
      reaches the network rather than failing. Both places that could
      teach it are gated on ``ws.supports_commands`` — nontainer's apps
      primer, and the seeded skill text (see
      ``_resolve_skill_conditionals``) — so the agent is steered to
      test_app / the preview instead. Closing it needs a guest->host
      channel reachable from the shell (dud DESIGN.md, "The apps
      loop").
    - Absolute paths under the SUBPROCESS backend live in the guest's
      own temp dir rather than ``/workspace``. This one the VM backends
      do close: they mount the workspace AT ``/workspace``, so absolute
      paths match the local sandbox everywhere else.

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

    ``script_hosts`` is deliberately left at nontainer's default rather
    than emptied. Studio is not itself air-gapped, and the public CDNs
    remain useful where they resolve; what changed is that nothing the
    agent is *told to use* requires them. An air-gapped deployment can
    set ``script_hosts=()`` on top of this, and the notes then say
    scripts may load only from the app itself.
    """
    return AppsConfig(
        static_assets={"vendor": app_assets_dir()},
        frontend_notes=FRONTEND_NOTES,
        csp=_csp(),
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


FRONTEND_NOTES = """\
Components: MUI (Material UI) with React and JSX. Put your JSX in
__WS__/app/app.jsx and add ONE tag to your html:

    <div id="root"></div>
    <script type="module" src="vendor/jsx-loader.js" data-app="app.jsx"></script>

That compiles app.jsx in the browser (no build step) and resolves the
imports, so write ordinary React:

    import { useState } from 'react';
    import { createRoot } from 'react-dom/client';
    import { Button, Dialog, Table } from '@mui/material';

Import BARE names, exactly as in any React project — do NOT rewrite them
as 'vendor/mui.min.js'. Copy references/app.{html,jsx} + api-handler.py
for a working app (filters -> fetch -> stats, chart, table, dialog) and
cut it down; only the file named by data-app is compiled, so keep your
components in that one .jsx.
Also here: `import { DataGrid } from '@mui/x-data-grid'` (sorting,
filtering and pagination without writing them), and a CURATED set of
Material icons — `import { Delete, Search } from '@mui/icons-material'`.
Icons come from that BARE package name, never a per-file path
('@mui/icons-material/Delete' does NOT resolve), and only the ~66 names
the building-apps skill lists exist. There is no '@mui/lab'.
Theme: `import theme from 'house/theme'` gives you this shell's palette
already built — wrap your tree in <ThemeProvider theme={theme}> with a
<CssBaseline />. Do NOT call createTheme and pick your own colours; the
app should look like the page it is embedded in. A non-React page gets
the same palette from <link rel="stylesheet" href="vendor/theme.css">,
which defines --app-primary, --app-surface, --app-text and friends.
Charts: <script src="vendor/plotly.min.js"></script>, then Plotly.react(
el, data, layout). Plotly 3.x, the full build — every trace type,
including tile-free scattergeo/choropleth for maps.
CSS: <script src="vendor/tailwind.js"></script> for tailwind utility
classes (it compiles them in the browser; no build, no config file).
Everything above is served WITH your app from its own origin, so it works
with no network at all. Do not load any of it from a CDN.
"""
"""What the agent is told it has. Replaces nontainer's default block,
which names esm.sh and cdn.jsdelivr — instructions to fetch from the
internet, in the one part of the prompt introduced with "copy this
known-good pattern exactly". Appending a correction underneath would
have left the wrong instruction both first and more emphatic, which is
why nontainer 0.3.4 made this block replaceable rather than additive.

MUI is the highlighted component pattern because that is where the
training mass is: a model writes `<Button variant="contained">` from
memory. The mechanically cheaper option (Material Web Components, 472KB
and no transpiler) verified just as well in a spike, but a private
component library extending MUI makes MUI a dependency regardless.

The import map used to live in the reference html, which made it
machinery the agent had to reproduce in every app it wrote — and an app
whose html lacked it failed on the first import, with an error about
module specifiers rather than about the thing the agent got wrong. The
loader supplies it now (deferring to one the page declares), so the
agent's whole obligation is a script tag and ordinary React imports.
The 'do NOT rewrite them' line stays: it is the remaining edit that
would break a working app, and it is cheap to say.

The theme is NAMED here rather than left to the agent because a
component library only buys a consistent look if every app reaches for
the same palette. `createTheme` is what a model writes from memory, and
what it picks is stock Material purple — recognisably not this shell.
Two spellings for the two frontends, one palette underneath
(appassets/theme.css), so a plain-DOM app and a React one cannot drift
apart.

__WS__, not a literal path: nontainer substitutes the workspace root
into the notes AFTER splicing this block in, so the agent is told the
real path even when an embedder moves the root. Writing '<root>' here
sent it the angle brackets."""


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
    later, on the first real session open, with its usual error."""
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
      built at startup too. First-touch session opens skip the boot.
    - ``0``: no idle VM RAM — but still bake the image in a background
      thread, so a first open pays boot-only, never build+boot.

    Studio never closes sessions during a run, so dud's pool would
    otherwise sit empty until shutdown — every first switch to a
    session after a restart paid a full boot."""
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
    """Executor kwarg for ``workspace()``, added ONLY when a custom
    backend is selected — so the default path calls ``workspace()``
    with its historical signature (a nontainer without
    ``executor_factory`` still works)."""
    factory = _executor_factory()
    return {"executor_factory": factory} if factory is not None else {}


DEFAULT_TITLE = "New session"

TITLE_MAX = 60  # the rail is ~200px; anything longer is ellipsis anyway


def _clean_title(title: object) -> str | None:
    """Free text -> a rail label, or None for "no title".

    The agent writes this via a tool, so it is untrusted shape: collapse
    every run of whitespace (a newline would break the row), bound the
    length, and treat blank as absent so a cleared user title reveals the
    agent's instead of shadowing it with "".
    """
    if not isinstance(title, str):
        return None
    text = " ".join(title.split())
    return text[:TITLE_MAX] or None


# streamed chunk events — the only types that compact (a merged run is
# indistinguishable from one big delta, so clients need no special case)
_DELTA_TYPES = ("text", "thinking")


def _compact(events: list[dict]) -> list[dict]:
    """Merge contiguous same-type delta runs into single events. The
    merged event keeps the FIRST seq of its run (monotonicity for
    followers). Delta granularity is a wire concern; storing it 1:1
    inflated logs 10-40x — a reasoning turn is thousands of chunks."""
    out: list[dict] = []
    for e in events:
        t = e.get("type")
        if out and t in _DELTA_TYPES and out[-1].get("type") == t:
            out[-1] = {
                **out[-1],
                "delta": out[-1].get("delta", "") + e.get("delta", ""),
            }
        else:
            out.append(e)
    return out


STUDIO_PRIMER = (
    "You work inside nontainer-studio; the human sees your workspace "
    "live. Anything under /workspace/app serves in a PREVIEW PANE beside the "
    "chat as you build it — they watch it take shape. Before you build "
    "an app there, or rework one substantially, READ the app-building "
    "skill listed under /workspace/skills: it carries the handler "
    "contract, reference files built to be copied, and the failure "
    "modes that otherwise cost you a dozen tool calls to rediscover. "
    "After changing "
    "the app, always verify with test_app before saying it works, and "
    "assert on DATA-bearing elements (a chart rendered, a count "
    "non-zero), not just static text — a page can look loaded while "
    "every fetch failed. When endpoints misbehave, tail "
    "/workspace/app/logs/api.log: handler errors, prints, and dispatch "
    "notes "
    "land there. Files the human uploads arrive under "
    "/workspace/uploads/. In "
    "run_python, set `ui = {...}` (figure/DataFrame/image values) to "
    "render results inline in your reply. For chat reports, match the "
    "artifact to the story: when it's a few headline numbers, LEAD "
    "with a card row (stat dicts, sublabel for the trend or context) "
    "and use a callout for the one caveat or insight that shouldn't "
    "be buried in prose; when the SHAPE of the data is the story, "
    "prefer raw plotly figures in `ui` — they render interactively "
    "right in the reply. Need a static image file instead? Use "
    "matplotlib savefig; plotly's write_image cannot run here. A new "
    "session is listed as 'New session' until it has a name: once you "
    "know what this one is about — usually after the first substantial "
    "exchange — call recommend_title so the human can find it again. "
    "Every "
    "turn is a checkpoint the human can rewind by editing an earlier "
    "prompt — prefer small complete "
    "steps over big-bang changes. They may also PUBLISH the app: a "
    "frozen version of the code behind a share URL that keeps serving "
    "while you keep working, over a `db` the published app owns. "
    "Publishing again adds a version and the URL moves to it, so build "
    "toward states worth publishing."
)

DB_PRIMER = (
    "`db` is a SQLite store for LIVE app state — it does NOT "
    "time-travel with checkpoints, so no rewind ever unwrites it. "
    "Publishing copies it once into the published app's own db, and "
    "every later version of that app keeps that db: a new version "
    "meets whatever schema the last one left, so create tables with "
    "CREATE TABLE IF NOT EXISTS and read tolerantly. Use it (not "
    "`cache`) for any "
    "state the app's users mutate. `cache` is versioned workspace "
    "data: it rewinds with restores and freezes at publish. API: "
    "`db.execute(sql, params=())` for writes (INSERT / UPDATE / "
    "`CREATE TABLE IF NOT EXISTS`), `db.executemany(sql, rows)` for "
    "bulk inserts (one commit), `db.query(sql, params=()) -> list "
    "of row tuples` for reads. Thread-safe; just call it."
)


class Db:
    """A tiny thread-safe SQLite store, injected as ``db`` (the
    webapp.py idiom). Frozen serving calls handlers concurrently, so
    the store owns its own locking."""

    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._c = sqlite3.connect(self._path, check_same_thread=False)
        self._lock = threading.Lock()

    def execute(self, sql: str, params: tuple = ()) -> None:
        """A write (INSERT/UPDATE/CREATE TABLE); commits."""
        with self._lock:
            self._c.execute(sql, params)
            self._c.commit()

    def executemany(self, sql: str, rows: Iterable[tuple]) -> None:
        """A bulk write — one commit for the whole batch. The obvious
        sqlite3 API agents reach for when loading a dataset; without
        it they fall back to hand-escaped literal INSERT strings."""
        with self._lock:
            self._c.executemany(sql, rows)
            self._c.commit()

    def copy_to(self, path: str | Path) -> None:
        """A consistent copy of the whole database at ``path``, through
        SQLite's backup API under this store's lock. A plain file copy
        could read pages mid-commit: app handlers write here outside
        any turn, so nothing else serializes them with a fork."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            dst = sqlite3.connect(str(path))
            try:
                self._c.backup(dst)
            finally:
                dst.close()

    def query(self, sql: str, params: tuple = ()) -> list:
        """A read (SELECT); returns a list of row tuples."""
        with self._lock:
            return self._c.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._c.close()


@dataclass
class Session:
    name: str
    ws: Workspace
    runtime: AppRuntime
    agent: Any
    db: Db
    turn_lock: threading.Lock
    model: str | None = None
    """This session's model spec (``provider:model``). Switchable mid-
    session — chat memory lives in the db keyed by session_id, so a
    rebuilt agent keeps the conversation."""
    """One agent turn at a time per session — chat 409s while a turn
    runs. Turns run as server-side tasks decoupled from the HTTP
    request, so disconnects/reloads/session switches never abort work."""

    turn_task: Any = None
    """The running turn's asyncio task. Held so the event loop's weak
    reference isn't the only one (the classic create_task GC footgun)."""

    run_id: str | None = None
    """The running turn's agno run id, as soon as the stream reveals it
    — the handle the stop button needs (agno's cancel-by-run-id)."""

    log_path: Path | None = None
    """Durable transcript: the COMPACTED event stream, appended at
    each non-delta boundary; open() reloads the tail. Replay-vs-live
    needs no special casing — the event feed serves both from one
    cursor, and a merged delta replays exactly like a big one."""

    events: list[dict] = field(default_factory=list)
    """The transcript, server-side: user messages, streamed agent
    events, turn boundaries — each stamped with an immutable ``seq``
    (identity is the seq, NOT the list position: compaction and the
    memory window reshape the list). Subscribers replay from a seq
    cursor and then follow live — this is what makes background
    sessions work."""

    next_seq: int = 0
    """Monotonic event id; survives compaction/window drops (and, via
    the jsonl, restarts)."""

    flush_idx: int = field(default=0, repr=False)
    """Index of the first event not yet written to the jsonl. Deltas
    buffer in memory until the next non-delta event compacts + flushes
    them — disk only ever carries the compacted form."""

    new_event: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def emit(self, event: dict) -> None:
        async with self.new_event:
            event = {**event, "seq": self.next_seq}
            self.next_seq += 1
            self.events.append(event)
            # Delta chunks buffer; anything else is a boundary: compact
            # the buffered run and flush, so the log stays current to
            # within the live delta run (crash loses at most that).
            if event["type"] not in _DELTA_TYPES:
                self._compact_and_flush()
            self.new_event.notify_all()

    def _compact_and_flush(self) -> None:
        """Caller holds ``new_event``. Compact the unflushed tail,
        append it to the jsonl, then trim memory to the tail window
        (flushed events only — nothing is ever dropped before it's on
        disk)."""
        tail = _compact(self.events[self.flush_idx :])
        self.events[self.flush_idx :] = tail
        if self.log_path is not None:
            with self.log_path.open("a") as f:
                for e in tail:
                    f.write(json.dumps(e) + "\n")
        self.flush_idx = len(self.events)
        if len(self.events) > MAX_EVENTS:
            del self.events[: len(self.events) - MAX_EVENTS]
            self.flush_idx = len(self.events)

    async def follow(self, since: int):
        """Yield ``(seq, event)`` from seq ``since``, then live. Runs
        forever; the subscriber disconnecting is the exit path. The
        cursor is re-resolved against the list each step (bisect on
        seq) because compaction may reshape it between yields; a
        follower that was lagging INSIDE a delta run when its turn
        compacted skips the run's merged remainder — the price of
        first-seq merging, paid only by slow consumers mid-turn."""
        import bisect

        cursor = max(0, since)
        while True:
            async with self.new_event:
                while not self.events or self.events[-1]["seq"] < cursor:
                    await self.new_event.wait()
            while True:
                idx = bisect.bisect_left(self.events, cursor, key=lambda e: e["seq"])
                if idx >= len(self.events):
                    break
                event = self.events[idx]
                yield event["seq"], event
                cursor = event["seq"] + 1

    @property
    def busy(self) -> bool:
        return self.turn_lock.locked()


class Registry:
    """``name -> Session``; open() is lazy and idempotent."""

    def __init__(
        self,
        model_factory: Callable[..., Any],
        store: Path | str | None = None,
        default_model: str | None = None,
        apps: AppsConfig | None = None,
    ) -> None:
        self._model_factory = model_factory  # (spec) -> agno Model
        self._default_model = default_model
        self._store = Path(store) if store else DEFAULT_STORE
        # Public: the router mounts alongside `resolve`, so the serving
        # half reads the same object the authoring half was built with
        # (see apps_config).
        self.apps = apps or apps_config()
        self._sessions: dict[str, Session] = {}
        # (token, version) -> the frozen workspace serving it. Keyed by
        # BOTH because an app's URL can be repointed at another version
        # (see set_current) and the snapshots are otherwise identical
        # objects to hand out.
        self._published: dict[tuple[str, str], Workspace] = {}
        # token -> the app's own db handle, shared by every version of
        # that app: the versions are different code over one live state.
        self._app_dbs: dict[str, Db] = {}
        # Workspaces mid-construction, by name. ``workspace_for`` has to
        # answer for a session that does not exist yet: building its
        # agent constructs the toolkit, which asks the db whether it owns
        # the workspace, and the db asks back through here.
        self._opening: dict[str, Workspace] = {}
        # Reentrant: ``workspace_for`` may be called while ``open`` holds
        # this, from the same thread, on the way through _build_agent.
        self._lock = threading.RLock()
        # ONE agno db for the whole store: a branch per session, so the
        # conversation is versioned with the files it produced. The
        # store path is the same one the workspaces are built with (the
        # db finds the kvgit store under it); ``store/agno`` holds the
        # cross-session tables agno keeps outside a session — memories,
        # metrics — which must not rewind with any one branch.
        self.db = KvgitStoreDb(
            self._store,
            open=self.workspace_for,
            db_path=str(self._store / "agno"),
        )
        self._migrate_published()

    def workspace_for(self, name: str) -> Workspace:
        """The LIVE workspace for a session — the store db's ``open``.

        Live, not merely equivalent: the db writes the conversation
        through this object and commits the turn on it, so a second
        Workspace over the same branch would split one turn across two
        staging buffers. An open session hands back its own; a session
        being opened right now hands back the workspace already built
        for it (the toolkit asks during construction, before the
        session exists); anything else opens.
        """
        with self._lock:
            session = self._sessions.get(name)
            if session is not None:
                return session.ws
            opening = self._opening.get(name)
            if opening is not None:
                return opening
        return self.open(name).ws

    def list(self) -> list[dict]:
        """Open sessions plus manifest names from prior runs — the
        workspaces and dbs survive restarts, so the rail should too
        (opening stays lazy; a listed-but-unopened session constructs
        on first use).

        NEWEST FIRST: `name` is a minted slug now, so alphabetical order
        is arbitrary — a new session would land in a random rail slot.
        Sessions with no birthday (pre-`created` manifests) sort last."""
        manifest = self._manifest()
        names = set(manifest["sessions"]) | set(self._sessions)
        created = manifest["created"]
        rows = [
            {
                "name": name,
                "title": self.title_of(name, manifest),
                "busy": (s := self._sessions.get(name)) is not None and s.busy,
                "model": (s.model if s is not None else manifest["models"].get(name)),
            }
            for name in names
        ]
        rows.sort(key=lambda r: (-created.get(r["name"], 0), r["name"]))
        return rows

    # -- titles: display only, never identity ------------------------------

    def title_of(self, name: str, manifest: dict | None = None) -> str:
        """The rail label. The human's own title always wins; the agent's
        fills the gap; neither means the session hasn't been named yet.
        Pass ``manifest`` to resolve a batch without re-reading the file
        (and to stay lock-free while a caller holds ``_lock``)."""
        entry = (manifest or self._manifest())["titles"].get(name) or {}
        return entry.get("user") or entry.get("agent") or DEFAULT_TITLE

    def set_user_title(self, name: str, title: str | None) -> str:
        """The human's override — outranks the agent forever. ``None``/
        blank CLEARS it, falling back to whatever the agent last said."""
        return self._set_title(name, "user", title)

    def set_agent_title(self, name: str, title: str | None) -> str:
        """The agent's suggestion. Always stored, even when a user title
        is hiding it: clearing theirs should reveal the agent's latest,
        not a stale one."""
        return self._set_title(name, "agent", title)

    def _set_title(self, name: str, tier: str, title: str | None) -> str:
        # takes _lock: the agent's tool writes titles from a worker
        # thread, and this is a read-modify-write of the whole manifest
        with self._lock:
            manifest = self._manifest()
            entry = dict(manifest["titles"].get(name) or {})
            entry[tier] = _clean_title(title)
            manifest["titles"][name] = entry
            self._save_manifest(manifest)
            return self.title_of(name, manifest)  # manifest passed: no re-lock

    def _manifest_path(self) -> Path:
        return self._store / "sessions.json"

    def _manifest(self) -> dict:
        """{"sessions": [...], "apps": {token: app}, "published":
        {token: {branch, session, checkpoint}}, "models": {name: spec},
        "titles": {name: {user, agent}}, "created": {name: epoch}} —
        tolerant of the v1 bare-list format, and of any key simply
        being absent.

        ``apps`` is the publication registry (see :meth:`publish`);
        ``published`` is the anchor-branch shape that preceded it and
        is empty after :meth:`_migrate_published` has run once."""
        try:
            data = json.loads(self._manifest_path().read_text())
        except Exception:
            data = {}
        if isinstance(data, list):  # v1: just session names
            data = {"sessions": data}
        return {
            "sessions": data.get("sessions", []),
            "apps": data.get("apps", {}),
            "published": data.get("published", {}),
            "models": data.get("models", {}),
            "titles": data.get("titles", {}),
            "created": data.get("created", {}),
        }

    def _load_manifest(self) -> set[str]:
        return set(self._manifest()["sessions"])

    def _save_manifest(self, manifest: dict) -> None:
        """Atomic (tmp + rename): a concurrent reader mid-write would
        parse partial JSON, see an empty manifest, and 404 a session
        that exists — a transient, maddening-to-reproduce failure."""
        path = self._manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=1))
        tmp.replace(path)

    def _record(self, name: str, model: str | None = None) -> None:
        """Add to the durable session manifest (caller holds _lock)."""
        manifest = self._manifest()
        manifest["sessions"] = sorted(set(manifest["sessions"]) | {name})
        if model is not None:
            manifest["models"][name] = model
        # birthday, stamped once: slugs carry no order, so this is the
        # only thing that can sort the rail sensibly (see `list`)
        manifest["created"].setdefault(name, time.time())
        self._save_manifest(manifest)

    def _unrecord(self, name: str) -> None:
        """Remove a reservation from the manifest (caller holds _lock)
        — ``_record``'s mirror, for a minted name whose open never
        succeeded. Clears titles/created too, same as ``delete``: a
        later mint drawing this slug must not inherit a ghost's
        birthday."""
        manifest = self._manifest()
        manifest["sessions"] = [s for s in manifest["sessions"] if s != name]
        manifest["models"].pop(name, None)
        manifest["titles"].pop(name, None)
        manifest["created"].pop(name, None)
        self._save_manifest(manifest)

    # -- create: mint an identity, then open it ----------------------------

    def create(self) -> Session:
        """A brand-new session under a minted slug.

        The slug is IDENTITY (branch / db file / jsonl / routes) and never
        changes; what the human reads is the title, which starts empty.

        Minting reserves inside ``_lock`` — ``_record`` publishes the name
        to the manifest so a concurrent mint can't hand out the same one —
        and opens outside it, because ``open`` takes ``_lock`` too and it
        is NOT reentrant."""
        with self._lock:
            name = self._mint_name()
            self._record(name)  # reserve the name against a racing mint
        try:
            return self.open(name)
        except BaseException:
            # A reservation whose open failed (dud not installed, a
            # guest image that can't build) would otherwise sit in the
            # rail forever, 500ing on every click — roll it back; a
            # retried "+ New" mints fresh. (The explicit-name path
            # needs no mirror: `open` records only after success.)
            with self._lock:
                self._unrecord(name)
            raise

    def _mint_name(self) -> str:
        """A pettable slug: `sleepy-meerkat`, not `session-3` (caller
        holds _lock). petname's vocabulary makes collisions rare, and
        `known()` makes them impossible — retry, then widen to three
        words rather than ever return a taken name."""
        known = self.known()
        for attempt in range(50):
            name = petname.Generate(3 if attempt > 25 else 2, "-")
            if name not in known:
                return validate_session_id(name)
        raise RuntimeError("could not mint a free session name")

    def get(self, name: str) -> Session | None:
        return self._sessions.get(name)

    def known(self) -> set[str]:
        """Names that exist durably (manifest) or in memory — the set
        the server may lazily open on GET (never creating new ones)."""
        return self._load_manifest() | set(self._sessions)

    def open(self, name: str) -> Session:
        """Create-or-return. Raises SessionIdError for bad names."""
        with self._lock:
            existing = self._sessions.get(name)
            if existing is not None:
                return existing
            model = self._manifest()["models"].get(name) or self._default_model
            db = Db(self._store / "dbs" / f"{name}.sqlite")
            ws = workspace(
                name,
                store=self._store,
                python=self._python_config(db),
                **_ws_kwargs(),
            )
            # Published before anything can ask: _build_agent constructs
            # the toolkit, which checks that the store db owns this
            # workspace, and the db answers by calling workspace_for.
            self._opening[name] = ws
            try:
                # <root>/ui exists from the start: agents predictably
                # savefig into it directly (instead of assigning objects
                # to `ui`), and VFS open honors real-fs semantics — no
                # parent, no write. Forgive the near-miss.
                if not ws.fs.isdir(f"{ws.root}/ui"):
                    ws.fs.makedirs(f"{ws.root}/ui", exist_ok=True)
                    ws.checkpoint(info={"tool": "init"})
                # Seed skills once, at session CREATION — after that they
                # are the session's own versioned state (agents may edit
                # or add them; a reseed would clobber that).
                if not ws.fs.isdir(f"{ws.root}/skills"):
                    self._seed_skills(ws)
                session = self._assemble(name, ws, db, model)
                loaded = self._load_events(session.log_path)
                session.events.extend(loaded)
                session.next_seq = (loaded[-1]["seq"] + 1) if loaded else 0
                session.flush_idx = len(session.events)  # loaded = on disk
                self._sessions[name] = session
            finally:
                self._opening.pop(name, None)
            self._record(name, model)
            return session

    @staticmethod
    def _seed_skills(ws: Workspace) -> None:
        """Install starter skills into a fresh session: each child
        directory of NONTAINER_STUDIO_SKILLS (default: the repo's
        skills/) plus any skills EMBEDDED in granted python libraries
        (<pkg>/skills/ — the nontainer convention). Best-effort: a bad
        skill must never block a session.

        Skill text is resolved for the executor first (see
        ``_resolve_skill_text``) — the seeded copy must not teach
        affordances this session doesn't have.
        """
        from nontainer import skills

        root = Path(
            os.getenv("NONTAINER_STUDIO_SKILLS")
            or Path(__file__).resolve().parent.parent / "skills"
        ).expanduser()
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / "SKILL.md").is_file():
                    try:
                        skills.install(ws, child)
                    except Exception:
                        pass
        try:
            skills.install_from_modules(ws)
        except Exception:
            pass
        try:
            Registry._resolve_skill_conditionals(ws)
        except Exception:
            pass  # a skill that won't resolve is still better than none

    # Conditional blocks in seeded SKILL.md files. The terminal-command
    # affordances (the apps `curl` builtin) exist only on LocalExecutor;
    # under dud the terminal is real bash, where `curl api/x` silently
    # hits the NETWORK instead of the dispatcher. nontainer already
    # gates its tool-description primer on ws.supports_commands; seeded
    # skill text has to be gated the same way or it teaches a debugging
    # step that fails open.
    _IF_BLOCK = re.compile(
        r"[ \t]*<!--if:(commands|no-commands)-->[ \t]*\n(.*?)[ \t]*<!--endif-->[ \t]*\n?",
        re.DOTALL,
    )

    @staticmethod
    def _resolve_skill_text(text: str, *, commands: bool) -> str:
        """Keep the blocks matching this executor, drop the others."""
        want = "commands" if commands else "no-commands"

        def _pick(m: "re.Match[str]") -> str:
            return m.group(2) if m.group(1) == want else ""

        return Registry._IF_BLOCK.sub(_pick, text)

    @staticmethod
    def _resolve_skill_conditionals(ws: Workspace) -> None:
        """Rewrite seeded SKILL.md files in place for this executor.

        Post-install rather than pre-install because ``skills.install``
        takes a directory of bytes; rewriting the installed copy keeps
        the source skill single-sourced (one file, both rungs) instead
        of forking it into per-executor variants that drift.
        """
        root = f"{ws.root}/skills"
        if not ws.fs.isdir(root):
            return
        commands = ws.supports_commands
        changed = False
        for name in sorted(ws.fs.list(root)):
            path = f"{root}/{name}/SKILL.md"
            if not ws.fs.exists(path):
                continue
            text = ws.fs.read(path).decode("utf-8", "replace")
            resolved = Registry._resolve_skill_text(text, commands=commands)
            if resolved != text:
                ws.fs.write(path, resolved.encode())
                changed = True
        if changed and ws.caps.versioned and ws.dirty:
            ws.checkpoint(info={"tool": "skill", "skill": "resolve-conditionals"})

    @staticmethod
    def _python_config(db: Db) -> PythonConfig:
        """Safe stdlib + the data stack when installed (opportunistic:
        `pip install pandas matplotlib` and the agent's Python grows —
        the run_python tool description self-updates from the grants).
        Presets run their environment side effects here, at session
        construction: matplotlib gets Agg-pinned and font-warmed before
        any sandboxed code runs."""
        from nontainer import presets

        modules = []
        for preset in ("dataframes", "plotting"):
            try:
                modules.append(getattr(presets, preset)())
            except ImportError:
                pass
        # Crash containment: agent code runs in a separate worker (the
        # workspace fs, cache, and db stay host-side, RPC-bridged) — a
        # segfault or OOM in C-extension guts costs the turn, not the
        # server. NONTAINER_STUDIO_ISOLATION=none opts out; =kernel
        # adds syscall/network lockdown on top.
        isolation = os.getenv("NONTAINER_STUDIO_ISOLATION", "process")
        if isolation not in ("none", "process", "kernel"):
            isolation = "process"
        # The knob belongs to the in-process sandbox. On a dud rung it
        # has no meaning: a VM already exceeds any level, and the
        # subprocess rung refuses an ask for containment it cannot give.
        if os.getenv("NONTAINER_STUDIO_EXECUTOR", "").lower() in ("dud", "dud-vm"):
            isolation = "none"
        return PythonConfig(
            modules=modules,
            host_objects={"db": db},
            isolation=isolation,
            # Import the granted stack ONCE into sandtrap's forkserver
            # broker; every worker then inherits it copy-on-write. With
            # dataframes()+plotting() granted that is the difference
            # between a worker costing ~233ms / 111MB and ~12ms / 29MB
            # (measured on this venv), and studio holds a session worker
            # per open workspace for its life — so it is memory, not
            # just latency. The safety caveat is grants whose IMPORT
            # starts a thread, which would leave the broker
            # multi-threaded; studio grants only nontainer's own presets,
            # and the arrow allocator they'd otherwise trip on is pinned
            # in `nontainer_studio/__init__` before anything imports
            # pandas. Process-wide, not per-workspace — the first
            # workspace to start a worker decides for the whole server,
            # which is safe here because EVERY workspace studio builds
            # (sessions and published snapshots alike) comes from this
            # one function.
            preload_grants=True,
            # App-handler workers, kept warm per distinct view. Preloaded,
            # a pristine worker is ~12ms, so the default of 0 gives every
            # request clean process state for about the cost of reusing
            # one. Raise it only for a published app under real
            # concurrency: past the cap, calls fall back to a per-call
            # sandbox rather than queueing, so too-low is latency while
            # too-high is resident memory that nothing reclaims.
            warm_view_workers=_view_workers(),
        )

    def _assemble(
        self, name: str, ws: Workspace, db: Db, model: str | None = None
    ) -> Session:
        # Apps dispatch works on both executors (stage 3c dissolved the
        # LocalExecutor-only sandbox surface into exec_python(view=)).
        # self.apps, not a fresh AppsConfig: the router serves published
        # snapshots under this same declaration (see apps_config).
        runtime = enable_apps(ws, self.apps)
        log_dir = self._store / "events"
        log_dir.mkdir(parents=True, exist_ok=True)
        return Session(
            name=name,
            ws=ws,
            runtime=runtime,
            agent=self._build_agent(name, ws, runtime, model),
            db=db,
            turn_lock=threading.Lock(),
            model=model,
            log_path=log_dir / f"{name}.jsonl",
        )

    @staticmethod
    def _load_events(log_path: Path | None) -> list[dict]:
        """Reload a prior run's transcript tail (torn last lines from
        a crash are skipped, not fatal). Legacy logs predate stored
        seqs and compaction: seqs are assigned by line position (which
        is what truncate events' `to` referenced back then), and the
        granular delta runs collapse on the way in."""
        if log_path is None or not log_path.exists():
            return []
        events = []
        for line in log_path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        for i, e in enumerate(events):
            e.setdefault("seq", i)
        return _compact(events)[-MAX_EVENTS:]

    def _title_tool(self, name: str) -> Callable:
        """The agent's handle on the session list.

        A studio tool, not a WorkspaceTools one: titles live in the
        registry, not the workspace. The closure captures only ``self``
        and ``name`` — both stable across the model-switch rebuild, and
        nothing turn-scoped, so a rebuilt agent's tool still works.
        """

        def recommend_title(title: str) -> str:
            """Give this session a short title for the human's session list.

            Call this once you know what the session is about — usually
            right after the first substantial exchange — and again only if
            the topic changes materially, not every turn. Prefer 3-6 words
            naming the work ("Revenue dashboard", "Debugging the CSV
            import"). The human can rename a session themselves, and their
            name always wins over yours.
            """
            # returns the RESOLVED label: when a human title is in force
            # this reports theirs, so the agent can see its suggestion is
            # stored but not shown
            return f"the session list now shows {self.set_agent_title(name, title)!r}"

        return recommend_title

    @staticmethod
    def _retry_rewind_hook(ws: Workspace) -> Callable:
        """Keep the WORKSPACE in step with the agent's memory when agno
        restarts a run.

        agno's whole-run retry rebuilds the message list from persisted
        history + the user message, so a restarted attempt begins with
        no memory of the previous attempt's tool calls — while every
        file those calls wrote is still on disk. That divergence is the
        exact thing this product exists to prevent: an edit rewinds
        files, memory, and transcript together, and a retry is the same
        rewind, just triggered by the provider instead of the human.
        Left unsynchronized it produces the worst failure mode we have
        — the model, blind to work it can still see the effects of,
        builds a second divergent version beside the first.

        The seam is agno's ``pre_hooks``: they run INSIDE the attempt
        loop, after the session read and before the messages are built,
        and ``run_context.run_id`` is stable across attempts (the
        RunOutput is created once, outside the loop). So the first call
        of a run records the pre-turn head — the same commit the `user`
        event stamps as its undo anchor — and any later call under that
        run_id is by definition a retry: restore to it.

        One slot rather than a map: a session runs one turn at a time
        (``turn_lock``), so there is only ever one live run to track.
        """
        state: dict[str, str | None] = {}

        async def rewind_workspace_on_retry(run_context: Any) -> None:
            run_id = getattr(run_context, "run_id", None)
            if run_id is None:
                return
            if state.get("run_id") != run_id:  # first attempt of a new turn
                state["run_id"] = run_id
                state["head"] = ws.head
                return
            head = state.get("head")
            if head is None or ws.head == head:
                return  # nothing was committed to unwind
            # off-loop: restore takes the workspace lock and rewrites the
            # tree (and re-syncs a remote executor's guest)
            await asyncio.to_thread(ws.restore, head)

        return rewind_workspace_on_retry

    def _build_agent(
        self, name: str, ws: Workspace, runtime: AppRuntime, model: str | None = None
    ) -> Any:
        from agno.agent import Agent

        from . import providers

        toolkit = WorkspaceTools(
            ws,
            apps=runtime,
            python_primer=DB_PRIMER,
            # The conversation commits with the files. Naming the db
            # here is what stands the toolkit's own turn hook down:
            # agno runs post hooks BEFORE it persists the run, so a
            # hook-driven commit would carry the turn's files without
            # its memory. The db commits at the persist instead, and
            # the checkpoint mode stays per mutating call — this is the
            # turn's trailing commit, so the head stamped on the next
            # `user` event includes the conversation.
            session_db=self.db,
            # text-only models must not receive screenshot media — the
            # call AFTER an image-bearing tool result 400s ("no
            # endpoints support image input"), losing the turn. Model
            # switches rebuild the agent, so this stays correct.
            vision=providers.supports_vision(model or self._default_model),
        )
        # Compaction: wave-based tool-result compression at a per-model
        # high-water mark (never count-based, never a sliding window —
        # both would bust the prompt cache every turn). The transcript
        # keeps full detail either way; only the MODEL's view of old
        # tool results coarsens.
        compression = None
        limit = providers.compress_token_limit(model or self._default_model)
        if limit is not None:
            from agno.compression.manager import CompressionManager

            compression = CompressionManager(compress_token_limit=limit)

        return Agent(
            model=self._model_factory(model),
            tools=[toolkit, self._title_tool(name)],
            compress_tool_results=compression is not None,
            compression_manager=compression,
            # runs per ATTEMPT, which is what makes it the right seam for
            # keeping files and memory rewinding together (see the hook)
            pre_hooks=[self._retry_rewind_hook(ws)],
            # studio-owned context: nontainer's tool descriptions cover
            # the MECHANICS (workspace, handlers, curl); this covers the
            # product the human is looking at (preview, artifacts,
            # checkpoints, publish)
            instructions=STUDIO_PRIMER,
            # Durable chat, keyed by the session name and stored in that
            # session's own workspace branch: after a server restart the
            # agent still remembers the conversation (and the jsonl
            # event log restores the visible transcript), and a rewind
            # of the files is a rewind of the memory — one restore, not
            # two writes that can disagree.
            db=self.db,
            session_id=name,
            add_history_to_context=True,
            markdown=True,
            # A LAST-DITCH FLOOR, not the primary defense. Transient
            # provider errors are absorbed one layer down, at the model
            # call, where the retry keeps the turn's tool results (see
            # providers._with_retries). This layer restarts the WHOLE
            # run: attempt > 0 re-reads the session from the db and
            # rebuilds the messages from persisted history + the user
            # message, so every tool call the failed attempt made is
            # gone from the agent's memory while its side effects stay
            # in the workspace — the model then builds a second,
            # divergent version over the first. Kept at 1 because only
            # ModelProviderError routes through the model layer; a
            # failure of another class would otherwise cost the turn
            # outright. When it does fire, the workspace rewinds with
            # the memory (_retry_rewind_hook) so the restart is a clean
            # one, and the turn says so (server.py counts RunStarted).
            # If all attempts fail, the run lands status=error and
            # repair_aborted_run keeps it in the agent's memory.
            retries=1,
            delay_between_retries=2,
            exponential_backoff=True,
        )

    # -- fork: branch the whole universe --------------------------------------

    def fork(self, session: Session, *, conversation: str = "inherit") -> Session:
        """Branch a session into a new one under a minted slug.

        One kvgit operation carries the files, the cache, the cwd AND
        the conversation, because all four live in the branch — so the
        child opens where the parent stands rather than reconstructing
        it. ``conversation="inherit"`` keeps the agent's memory (the
        same chat, over its own files from here on) and copies the
        visible transcript to match it; ``"fresh"`` drops both, giving a
        clean chat over the forked files.

        The app db is COPIED, not shared: it is live external state with
        no history, so the two universes must not write over each other.

        Forking is a between-turns verb. A turn in flight owns the
        workspace, and kvgit refuses to fork a branch with staged
        changes — a fork of half a turn would be a state no checkpoint
        ever held.
        """
        if conversation not in ("inherit", "fresh"):
            raise ValueError(
                f"conversation must be 'inherit' or 'fresh': {conversation!r}"
            )
        # Reserve the session for the whole fork: a snapshot check would
        # let a chat request take the turn lock a moment later and land
        # a tool commit under the fork — new files with the parent's old
        # memory, the half-turn state this guard exists to prevent.
        if not session.turn_lock.acquire(blocking=False):
            raise RuntimeError("can't fork while a turn is running")
        try:
            return self._fork_locked(session, conversation=conversation)
        finally:
            session.turn_lock.release()

    def _fork_locked(self, session: Session, *, conversation: str) -> Session:
        """:meth:`fork` with the parent already reserved.

        Split out for the callers that need to do something to the
        parent's branch inside the same reservation — see
        :meth:`branch_from_version`, which rewinds the parent, forks
        there, and puts it back.
        """
        if session.ws.dirty:
            raise RuntimeError("can't fork mid-turn: the workspace has staged changes")
        with self._lock:
            name = self._mint_name()
            self._record(name, session.model)
        try:
            child_ws = fork_session(session.ws, name, conversation=conversation)
            # The fork inherits the PARENT's python config, and with it
            # the parent's `db` host object. Let it go and build the
            # child's own handle over the copied file below — two
            # universes sharing one app db is the state this copy exists
            # to prevent.
            child_ws.close()
            dst = self._store / "dbs" / f"{name}.sqlite"
            session.db.copy_to(dst)
            db = Db(dst)
            ws = workspace(
                name,
                store=self._store,
                python=self._python_config(db),
                **_ws_kwargs(),
            )
            with self._lock:
                self._opening[name] = ws
                try:
                    child = self._assemble(name, ws, db, session.model)
                    # The visible transcript must match the memory the
                    # child inherited, or the human reads a blank page
                    # over an agent that remembers everything.
                    if (
                        conversation == "inherit"
                        and session.log_path is not None
                        and child.log_path is not None
                        and session.log_path.exists()
                    ):
                        shutil.copyfile(session.log_path, child.log_path)
                    loaded = self._load_events(child.log_path)
                    child.events.extend(loaded)
                    child.next_seq = (loaded[-1]["seq"] + 1) if loaded else 0
                    child.flush_idx = len(child.events)  # loaded = on disk
                    self._sessions[name] = child
                finally:
                    self._opening.pop(name, None)
            return child
        except BaseException:
            # A reserved name whose fork failed would sit in the rail
            # forever, 500ing on every click — same rollback as create.
            with self._lock:
                self._unrecord(name)
            raise

    # -- delete: remove a session's whole universe ---------------------------

    def delete(self, session: Session) -> None:
        """Delete a session and everything it owns: the workspace
        branch, the session's app db, the transcript, the agent's chat
        record. Caller ensures not busy.

        Its published APPS are not among them. An app owns its db and
        its versions are store-scoped tags, which is the scope that
        survives ``delete_workspace`` — so the URLs someone was handed
        keep serving after the conversation that built them is gone.
        Taking one down is ``unpublish``, said about the app."""
        name = session.name
        # Before the session leaves the registry: the db reaches the
        # conversation through workspace_for, which would otherwise
        # reopen the session it is being asked to erase. Best-effort —
        # the branch deletion below takes the conversation with it
        # either way, and nothing may block a delete.
        try:
            self.db.delete_session(name)
        except Exception:
            pass
        with self._lock:
            self._sessions.pop(name, None)
            manifest = self._manifest()
            manifest["sessions"] = [s for s in manifest["sessions"] if s != name]
            manifest["models"].pop(name, None)
            # titles/created go too, or a later mint that happens to draw
            # this slug (it's free again once `sessions` forgets it) would
            # inherit a dead session's name and birthday
            manifest["titles"].pop(name, None)
            manifest["created"].pop(name, None)
            self._save_manifest(manifest)
        close_runtime = getattr(session.runtime, "close", None)
        if callable(close_runtime):  # reap dispatch workers
            close_runtime()
        session.ws.close()  # before branch deletion: it holds the branch
        session.db.close()
        self._delete_branches({name})
        (self._store / "dbs" / f"{name}.sqlite").unlink(missing_ok=True)
        (self._store / "events" / f"{name}.jsonl").unlink(missing_ok=True)

    def _delete_branches(self, names: set[str]) -> None:
        """Remove kvgit branches from the shared store. Deletion is
        nontainer's: `delete_workspace` resolves `store/kvgit` from the
        same `store` the workspaces were built with, and takes each
        branch's session-scoped tags with it while leaving store-scoped
        ones — which is what keeps a published app up after its session
        is deleted."""
        delete_workspace(names, store=self._store, backend="kvgit")

    # -- model switching ----------------------------------------------------

    def set_model(self, session: Session, spec: str) -> None:
        """Rebuild the session's agent on a different model. The chat
        db is keyed by session_id, so the new agent keeps the whole
        conversation — switch models mid-project freely. Raises
        ValueError (via the model factory) on an unknown spec."""
        session.agent = self._build_agent(
            session.name, session.ws, session.runtime, spec
        )
        session.model = spec
        with self._lock:
            manifest = self._manifest()
            manifest["models"][session.name] = spec
            self._save_manifest(manifest)

    # -- edit: rewind + retry as one verb -----------------------------------

    def rewind_to_event(self, session: Session, seq: int) -> None:
        """The rewind half of an EDIT: restore the workspace to the
        user event's pre-turn head. That one call is the whole rewind —
        the agent's memory lives in the same branch as the files, so the
        turns after that head are unsaid by the same restore that
        unwrites their files. The caller emits the `truncate` event and
        starts the new turn.

        The head, not commit order, is the anchor: the `user` event
        stamps the workspace as it stood before its turn ran, which is
        exactly the state the edited prompt should run from.

        The agent's title rewinds too — it named the session from a
        conversation that is being unsaid."""
        event = next((e for e in session.events if e.get("seq") == seq), None)
        head = event.get("head") if event else None
        if event is None or event.get("type") != "user" or not head:
            raise ValueError(f"event {seq} is not an editable user message")
        self._rewind(session, seq, head)

    def _rewind(self, session: Session, seq: int, head: str) -> None:
        """Put the files, the agent's memory and the agent's title back
        where they stood at ``head``, with the transcript cut at ``seq``.

        One ``restore`` covers the first two: the conversation lives in
        the same branch as the files. The title is a third thing, kept
        in the manifest, so it is put back by hand — the agent named the
        session from a conversation that is being unsaid.
        """
        surviving_title = None
        prior = [e for e in session.events if e["seq"] < seq]
        for _, ev in self._visible(prior):
            if ev.get("type") == "title":
                surviving_title = ev.get("title") or surviving_title
        session.ws.restore(head)
        # Best-effort within the event window: revert to the last title
        # the agent gave BEFORE the cut. None surviving is ambiguous —
        # never titled, or titled so long ago the event front-trimmed out
        # (MAX_EVENTS) — so keep what the manifest says rather than wipe a
        # name we can't prove was undone.
        if surviving_title is not None:
            self.set_agent_title(session.name, surviving_title)

    @staticmethod
    def _visible(events: list[dict]) -> list[tuple[int, dict]]:
        """The transcript PROJECTION: (seq, event) pairs with truncate
        events applied. An edit appends {type: 'truncate', to: seq}
        instead of mutating the log — it's append-only by design (SSE
        cursors, jsonl durability) — so anything reasoning about 'what
        the transcript now says' must look through this, not the raw
        list: a done event after a cut refers to a run that no longer
        exists in agent memory."""
        visible: list[tuple[int, dict]] = []
        for event in events:
            if event.get("type") == "truncate":
                to = event.get("to", 0)
                while visible and visible[-1][0] >= to:
                    visible.pop()
            else:
                visible.append((event.get("seq", 0), event))
        return visible

    # -- migration: anchor branches become apps -----------------------------

    def _migrate_published(self) -> None:
        """Bring pre-app publications forward, once, at startup.

        A publish used to fork an anchor branch and serve it over the
        session's live db, so the token named a BRANCH and the state was
        the session's. An app now owns its db and its versions are
        store-scoped tags — so each old entry becomes an app holding one
        version, ``v1``, tagged at the anchor branch's head, over a copy
        of the origin session's db. A copy is the closest state there
        is: the two were sharing one db, and the app has to stop sharing
        it here or deleting the session would take the app's state.

        An entry whose anchor branch is gone is dropped. A token that
        names no state serves nothing, and leaving it in the manifest
        only means a 404 that looks like a bug.
        """
        with self._lock:
            manifest = self._manifest()
            legacy = manifest.get("published") or {}
            if not legacy:
                return
            for token, old in legacy.items():
                entry = self._migrate_one(token, old, manifest)
                if entry is None:
                    log.warning(
                        "publish: dropped %s — its snapshot branch %r is gone",
                        token,
                        old.get("branch"),
                    )
                else:
                    manifest["apps"][token] = entry
                    log.info(
                        "publish: migrated %s to an app at v1 (was branch %r)",
                        token,
                        old.get("branch"),
                    )
            manifest["published"] = {}
            self._save_manifest(manifest)

    def _migrate_one(self, token: str, old: dict, manifest: dict) -> dict | None:
        """One anchor branch -> one app with a ``v1``, or None if the
        branch is gone. Caller holds ``_lock``."""
        branch = old.get("branch")
        checkpoint = old.get("checkpoint")
        origin = old.get("session")
        if not branch:
            return None
        tag = f"pub/{token}/v1"
        title = self.title_of(origin, manifest) if origin else DEFAULT_TITLE
        commit = tree = None
        ws = workspace(branch, store=self._store)
        try:
            # Opening a branch CREATES it, so "is it still there" cannot
            # be asked by opening. Ask by content instead: an anchor
            # never moved after the fork that made it, so its head is
            # the checkpoint the manifest recorded — while a branch this
            # very call invented has a baseline head of its own.
            if checkpoint and ws.head == checkpoint:
                commit = ws.tag(
                    tag,
                    scope="store",
                    info={
                        "token": token,
                        "session": origin,
                        "version": "v1",
                        "title": title,
                    },
                )
                tree = ws.head_tree
        finally:
            ws.close()
        # Either way the anchor branch goes: it was the old serving
        # mechanism, and the tag (store-scoped) outlives it.
        self._delete_branches({branch})
        if commit is None:
            return None
        src = self._store / "dbs" / f"{origin}.sqlite"
        dst = self._app_db_path(token)
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            db = Db(src)
            try:
                db.copy_to(dst)
            finally:
                db.close()
        return {
            "token": token,
            "session": origin,
            "title": title,
            "created": time.time(),
            "db": f"dbs/apps/{token}.sqlite",
            "current": "v1",
            "versions": {
                "v1": {
                    "tag": tag,
                    "commit": commit,
                    "tree": tree,
                    "created": time.time(),
                }
            },
        }

    # -- publish: an app is a lineage of versions ---------------------------
    #
    # An APP is a publication with a stable URL: one capability token,
    # one db of its own, and a growing set of VERSIONS. A version is a
    # store-scoped nontainer tag over the origin session's branch, which
    # is the scope that outlives a session — so deleting the session
    # that built an app leaves the app up.
    #
    # The app owns its db from its first version: the session's live db
    # is copied once (through SQLite's backup API) into
    # dbs/apps/<token>.sqlite, and every later version of that app keeps
    # it. Schema migration across versions is the app's own business,
    # which is exactly what the DB_PRIMER already tells the agent about
    # `db`: live state, no history, nothing rewinds it.

    def _app_db_path(self, token: str) -> Path:
        return self._store / "dbs" / "apps" / f"{token}.sqlite"

    def _app_db(self, entry: dict) -> Db:
        """The app's own db handle, one per app.

        Shared by every version of that app on purpose: the versions are
        different code over ONE live state, the same way the preview and
        the session share one db."""
        token = entry["token"]
        db = self._app_dbs.get(token)
        if db is None:
            db = Db(self._store / entry["db"])
            self._app_dbs[token] = db
        return db

    @staticmethod
    def _version_name(asked: str | None, entry: dict | None) -> str:
        """The name this version gets: the caller's, or the next free
        ``vN`` in the app.

        ``/`` is refused on top of kvgit's own rule (non-empty, no
        ``%``) because a version's tag is ``pub/<token>/<version>`` — a
        slash in the name would make the tag say something else."""
        taken = set((entry or {}).get("versions", {}))
        if asked is None:
            n = len(taken) + 1
            while f"v{n}" in taken:
                n += 1
            return f"v{n}"
        name = asked.strip()
        if not name or "/" in name or "%" in name:
            raise ValueError(
                f"a version name must be non-empty and free of '/' and '%': {asked!r}"
            )
        if name in taken:
            raise ValueError(f"this app already has a version {name!r}")
        return name

    @staticmethod
    def _last_published(entry: dict) -> float:
        """When this app last got a version — how "the session's most
        recent app" is decided, since an app's own birthday says only
        when the lineage started."""
        versions = entry.get("versions") or {}
        return max(
            (v.get("created", 0) for v in versions.values()),
            default=entry.get("created", 0),
        )

    def _target_app(self, name: str, app: str | None, apps: dict) -> tuple[str, dict]:
        """Which lineage this publish extends: a named token, a fresh
        one for ``"new"``, or — with nothing asked — the session's most
        recently published app, or a fresh one if it has none. The
        second element is the existing entry, or ``{}`` for a new app."""
        if app == "new":
            return mint_token(), {}
        if app:
            entry = apps.get(app)
            if entry is None:
                raise KeyError(f"no app {app!r}")
            return app, entry
        mine = [e for e in apps.values() if e.get("session") == name]
        if mine:
            newest = max(mine, key=self._last_published)
            return newest["token"], newest
        return mint_token(), {}

    def publish(
        self, name: str, *, version: str | None = None, app: str | None = None
    ) -> dict:
        """Publish the session's current state as a new version of an app.

        The version is a store-scoped tag, so it is durable and
        session-independent from the moment it exists; the app's URL
        then points at it (``current``), which is what makes publishing
        a new version the everyday verb and rolling back a pointer move
        (:meth:`set_current`).

        ``app`` picks the lineage — a token extends that app, ``"new"``
        starts one, and ``None`` means the session's most recently
        published app or a new one. ``version`` names it; the default is
        ``v1``, ``v2``, ... within the app.

        A between-turns verb, like fork: a turn in flight owns the
        workspace, and a tag made over half a turn would name a state
        no checkpoint ever held.
        """
        session = self._sessions.get(name)
        if session is None:
            raise KeyError(name)
        if not session.turn_lock.acquire(blocking=False):
            raise RuntimeError("can't publish while a turn is running")
        try:
            with self._lock:
                manifest = self._manifest()
                token, entry = self._target_app(name, app, manifest["apps"])
                version = self._version_name(version, entry)
                title = self.title_of(name, manifest)
                tag = f"pub/{token}/{version}"
                # The tag first, because it is the only step that can
                # refuse (a taken name, a name kvgit won't have) — and
                # it commits the session's staged work, so what the tag
                # names is what the human was looking at.
                commit = session.ws.tag(
                    tag,
                    scope="store",
                    info={
                        "token": token,
                        "session": name,
                        "version": version,
                        "title": title,
                    },
                )
                try:
                    if not entry:
                        self._app_db_path(token).parent.mkdir(
                            parents=True, exist_ok=True
                        )
                        session.db.copy_to(self._app_db_path(token))
                        entry = {
                            "token": token,
                            "session": name,
                            "created": time.time(),
                            "db": f"dbs/apps/{token}.sqlite",
                            "versions": {},
                        }
                    entry = dict(entry, versions=dict(entry["versions"]))
                    # the app wears the session's title as of this
                    # publish: renaming the session and publishing again
                    # should rename the app, not leave it under a name
                    # nobody uses any more
                    entry["title"] = title
                    entry["versions"][version] = {
                        "tag": tag,
                        "commit": commit,
                        "tree": session.ws.head_tree,
                        "created": time.time(),
                    }
                    entry["current"] = version
                    manifest["apps"][token] = entry
                    self._save_manifest(manifest)
                except BaseException:
                    # a tag with no manifest entry is a name nothing can
                    # reach and nothing will ever collect
                    session.ws.delete_tag(tag, scope="store")
                    raise
                self._drop_snapshots(token)
                return {
                    "token": token,
                    "url": f"/apps/{token}/",
                    "version": version,
                    "title": title,
                    "checkpoint": commit,
                    "tree": entry["versions"][version]["tree"],
                }
        finally:
            session.turn_lock.release()

    # -- serving: the URL is the app's, the state is a version's ------------

    def resolve(self, token: str) -> Workspace | None:
        """The ``build_router`` resolve hook: the frozen workspace at
        this app's CURRENT version.

        The URL belongs to the app, not to a version, so what it serves
        moves when the pointer moves — hence the cache is keyed by
        token AND version, and repointing simply drops the old entry.

        Reopening after a restart needs nothing from the origin session,
        which may be long gone: the version is a store-scoped tag and
        the db is the app's own file. ``at_tag`` does need SOME
        workspace on the store to look a name up, so a handle on the
        reserved ``_apps`` branch does the lookup and is closed again
        immediately — the frozen workspace it returns holds its own
        kvgit handle and its own executor, so the parent is scaffolding,
        not part of what serves.
        """
        entry = self._manifest()["apps"].get(token)
        if entry is None:
            return None
        version = entry.get("current")
        info = (entry.get("versions") or {}).get(version)
        if info is None:
            return None
        snapshot = self._published.get((token, version))
        if snapshot is not None:
            return snapshot
        with self._lock:
            snapshot = self._published.get((token, version))
            if snapshot is not None:
                return snapshot
            parent = workspace(
                APP_BRANCH,
                store=self._store,
                python=self._python_config(self._app_db(entry)),
                **_ws_kwargs(),
            )
            try:
                snapshot = parent.at_tag(info["tag"], scope="store")
            finally:
                parent.close()
            self._published[(token, version)] = snapshot
            return snapshot

    def _drop_snapshots(self, token: str, version: str | None = None) -> None:
        """Close cached frozen workspaces for an app — all of them, or
        one version's. Caller holds ``_lock``."""
        doomed = [
            key
            for key in self._published
            if key[0] == token and (version is None or key[1] == version)
        ]
        for key in doomed:
            self._published.pop(key).close()

    # -- the registry of apps ----------------------------------------------

    def list_apps(self) -> list[dict]:
        """Every app on this store, most recently published first."""
        rows = [self._app_row(e) for e in self._manifest()["apps"].values()]
        rows.sort(key=lambda r: -r["published"])
        return rows

    @staticmethod
    def _app_row(entry: dict) -> dict:
        """One app, as the API says it. Versions come back as a LIST in
        publish order — the order they are read in — rather than the
        manifest's name-keyed map."""
        versions = entry.get("versions") or {}
        return {
            "token": entry["token"],
            "title": entry.get("title") or DEFAULT_TITLE,
            "session": entry.get("session"),
            "created": entry.get("created", 0),
            "published": Registry._last_published(entry),
            "current": entry.get("current"),
            "url": f"/apps/{entry['token']}/",
            "versions": [
                {
                    "name": name,
                    "commit": v.get("commit"),
                    "tree": v.get("tree"),
                    "created": v.get("created", 0),
                }
                for name, v in sorted(
                    versions.items(), key=lambda kv: kv[1].get("created", 0)
                )
            ],
        }

    def session_apps(self, session: Session) -> list[dict]:
        """This session's apps, each carrying what its live workspace
        holds that the served version doesn't."""
        rows = [r for r in self.list_apps() if r["session"] == session.name]
        for row in rows:
            row["changed_since"] = self._changed_since(session, row)
        return rows

    @staticmethod
    def _changed_since(session: Session, row: dict) -> dict:
        """The distance between a session's app files and the version
        its URL serves — two answers, because they are two questions.

        ``count`` / ``paths`` are the CONTENT question: files under
        ``<root>/app`` whose bytes differ from the tagged state. A file
        re-saved with the bytes it already had is not in them.

        ``up_to_date`` is the WRITE question: kvgit stamps every write
        with when it happened, so the tree moves on any write at all,
        anywhere in the workspace. False with ``count`` 0 means "the
        session has moved on, but the app is the same app" — which is
        the common case after a turn that only touched notes.
        """
        current = next(
            (v for v in row["versions"] if v["name"] == row["current"]), None
        )
        if current is None or not current.get("commit"):
            return {"count": 0, "paths": [], "up_to_date": True}
        diff = session.ws.changed_since(current["commit"])
        prefix = f"{session.ws.root}/app/"
        paths = sorted(
            p
            for p in (diff.added | diff.removed | diff.modified)
            if p.startswith(prefix)
        )
        return {
            "count": len(paths),
            "paths": paths,
            "up_to_date": session.ws.head_tree == current.get("tree"),
        }

    # -- moving and removing publications ----------------------------------

    def set_current(self, token: str, version: str) -> dict:
        """Repoint an app's URL at one of its versions — a rollback or a
        roll forward. Tags never move; the pointer does, which is the
        whole reason the URL is the app's and not a version's."""
        with self._lock:
            manifest = self._manifest()
            entry = manifest["apps"].get(token)
            if entry is None:
                raise KeyError(token)
            if version not in (entry.get("versions") or {}):
                raise ValueError(f"this app has no version {version!r}")
            entry["current"] = version
            self._save_manifest(manifest)
            self._drop_snapshots(token)
            return self._app_row(entry)

    def unpublish(self, token: str) -> None:
        """Take an app down: every version's tag, its db, its cached
        snapshots and its manifest entry.

        The origin session is untouched — an app was never the session's
        state, only a named copy of it."""
        with self._lock:
            manifest = self._manifest()
            entry = manifest["apps"].pop(token, None)
            if entry is None:
                raise KeyError(token)
            self._drop_snapshots(token)
            db = self._app_dbs.pop(token, None)
            if db is not None:
                db.close()
            self._delete_tags(v["tag"] for v in (entry.get("versions") or {}).values())
            (self._store / entry["db"]).unlink(missing_ok=True)
            self._save_manifest(manifest)

    def delete_version(self, token: str, version: str) -> dict:
        """Remove one version of an app.

        Not the one the URL serves (it would point at nothing) and not
        the last one — taking an app down is ``unpublish``, and a verb
        that big should be the one the caller named."""
        with self._lock:
            manifest = self._manifest()
            entry = manifest["apps"].get(token)
            if entry is None:
                raise KeyError(token)
            versions = entry.get("versions") or {}
            if version not in versions:
                raise ValueError(f"this app has no version {version!r}")
            if version == entry.get("current"):
                raise ValueError(
                    f"{version!r} is what the app's URL serves — point it at "
                    "another version first"
                )
            if len(versions) == 1:
                raise ValueError(
                    "this is the app's only version — unpublish the app instead"
                )
            self._drop_snapshots(token, version)
            self._delete_tags([versions.pop(version)["tag"]])
            self._save_manifest(manifest)
            return self._app_row(entry)

    def _delete_tags(self, names: Iterable[str]) -> None:
        """Drop store-scoped tags.

        Saying it needs a workspace on the store, and no session owns
        these names — so it is said through a handle on the reserved
        ``_apps`` branch, opened for the call and closed again. No
        executor factory: nothing here runs agent code, and a dud rung
        would boot a machine to delete a name.
        """
        names = list(names)
        if not names:
            return
        ws = workspace(APP_BRANCH, store=self._store)
        try:
            for name in names:
                try:
                    ws.delete_tag(name, scope="store")
                except Exception:
                    log.warning("publish: could not delete tag %s", name)
        finally:
            ws.close()

    # -- branching a version back into a conversation -----------------------

    def branch_from_version(self, token: str, version: str) -> tuple[Session, int]:
        """Fork the origin session and rewind the child to that
        version's commit, so it opens with the files AND the
        conversation as they stood at that publish.

        Returns the child and the seq of the publish marker in its
        transcript (-1 if it holds none), which the caller cuts after.

        Raises ``KeyError`` when the origin session is gone: the app's
        files are still served from the tag, but a conversation cannot
        be branched out of a branch that no longer exists.
        """
        entry = self._manifest()["apps"].get(token)
        if entry is None:
            raise KeyError(token)
        info = (entry.get("versions") or {}).get(version)
        if info is None:
            raise ValueError(f"this app has no version {version!r}")
        origin = entry.get("session")
        if origin not in self.known():
            raise KeyError(origin)
        session = self.open(origin)
        # Rewind the PARENT, fork there, put it back — nontainer's own
        # recipe for branching from a checkpoint, and the only one that
        # gives a coherent child: forking first and rewinding the child
        # after would unwrite the fork's own commit, the one that gave
        # the child its session id, so the branch would hold the
        # conversation under the PARENT's name and the child would open
        # with no memory at all. The parent is reserved throughout, so
        # nothing can see it mid-rewind but its own live preview.
        if not session.turn_lock.acquire(blocking=False):
            raise RuntimeError("can't branch while a turn is running")
        try:
            head = session.ws.head
            session.ws.restore(info["commit"])
            try:
                child = self._fork_locked(session, conversation="inherit")
            finally:
                session.ws.restore(head)
        finally:
            session.turn_lock.release()
        cut = next(
            (
                e["seq"]
                for e in reversed(child.events)
                if e.get("type") == "publish"
                and e.get("token") == token
                and e.get("version") == version
            ),
            -1,
        )
        return child, cut

    def restore_to_publish(self, session: Session, seq: int) -> int:
        """Rewind a session to one of its own publishes: the files and
        the conversation go back to where they stood when that version
        was tagged.

        The same synchronized rewind an edit does — one ``restore``,
        because the conversation lives in the branch with the files —
        anchored on the marker's commit instead of a user event's
        pre-turn head. Returns the seq the transcript is cut AFTER: the
        marker survives its own restore, since the version it names is
        still there and is still what you came back to.
        """
        event = next((e for e in session.events if e.get("seq") == seq), None)
        head = event.get("head") if event else None
        if event is None or event.get("type") != "publish" or not head:
            raise ValueError(f"event {seq} is not a publish marker")
        self._rewind(session, seq, head)
        return seq + 1

    def close(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                close_runtime = getattr(session.runtime, "close", None)
                if callable(close_runtime):  # reap dispatch workers
                    close_runtime()
                session.ws.close()
                session.db.close()
            self._sessions.clear()
            for snapshot in self._published.values():
                snapshot.close()
            self._published.clear()
            for db in self._app_dbs.values():
                db.close()
            self._app_dbs.clear()


def repair_aborted_run(session: Session, run_id: str | None, note: str) -> None:
    """agno's history builder SKIPS runs with status=error or
    status=cancelled — so a transport hiccup at the end of a long turn
    (or a user hitting stop) would erase the whole turn from the
    agent's memory while the human transcript still shows it. That
    divergence produces confident confabulation, not "I don't
    remember".

    The messages up to the cut are real work: append a closing note
    explaining the abnormal end, mark the run completed, and the agent
    keeps its memory AND knows the turn was cut short."""
    db = getattr(session.agent, "db", None)
    if db is None or run_id is None:
        return
    try:
        from agno.db.base import SessionType
        from agno.models.message import Message
        from agno.run.base import RunStatus

        record = db.get_session(session_id=session.name, session_type=SessionType.AGENT)
        if record is None or not record.runs:
            return
        run = next(
            (r for r in record.runs if getattr(r, "run_id", None) == run_id), None
        )
        if run is None or run.status not in (RunStatus.error, RunStatus.cancelled):
            return
        run.status = RunStatus.completed
        run.messages = (run.messages or []) + [
            Message(
                role="assistant",
                content=f"[turn aborted early: {note} — the work above "
                "this point is real and completed]",
            )
        ]
        db.upsert_session(record)
    except Exception:
        pass  # repair is best-effort; never take down the turn handler
