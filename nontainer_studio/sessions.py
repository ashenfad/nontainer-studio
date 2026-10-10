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
  the agent's memory land in ONE commit and one ``ws.checkout`` puts
  all four back. agno's cross-session tables (memories, metrics) live at
  ``store/agno`` and never version — world state, not session state.
- EVENT LOG: durable but session-scoped — the transcript appends to a
  jsonl per session. An EDIT (rewind_to_event) trims the visible
  transcript too, via an appended `truncate` event, never by mutating
  the log.

``Registry`` here is the core: opening, forking, deleting and rewinding
sessions and building their agents. The rest of it is mixed in, each
part from a module of its own: the manifest and db handles
(``manifest``), titles (``titles``), skill seeding (``skills``),
delegates (``delegates``) and publishing (``publishing``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import petname
from nontainer import (
    PythonConfig,
    Store,
    Workspace,
    validate_session_id,
)
from nontainer.adapters.agno import WorkspaceTools
from nontainer.adapters.agno_db import KvgitStoreDb
from nontainer.apps import AppRuntime, AppsConfig, enable_apps
from nontainer.inbox import Inbox
from nontainer.sessions import Sessions, run_action
from nontainer.wsgit import register_wsgit

from .config import (
    AGENT_PYTHON_TIMEOUT,
    DEFAULT_STORE,
    WORKSPACE_IGNORE,
    _agent_host_objects,
    _bind_host_objects,
    _delegate_depth,
    _delegate_tool_calls,
    _delegate_ttl_hours,
    _unbind_host_objects,
    _view_workers,
    _ws_kwargs,
    apps_config,
    executor_runs_commands,
    sessions_tool_enabled,
    wsgit_enabled,
)
from .delegates import DELEGATE_TURNS, DelegationMixin, StudioRunner
from .manifest import ManifestMixin
from .prompts import (
    SESSIONS_TOOL_DESCRIPTION,
    STUDIO_PRIMER,
    _delegation_primer,
    _depth_primer,
    _depth_refusal,
    _python_primer,
    _render_others,
    _render_published,
    _retention_primer,
    _unit_test_primer,
    _versioning_primer,
)
from .publishing import PublishingMixin
from .session import (
    Db,
    ReservedSessionError,
    Session,
    SweptSessionError,
    _load_events,
    _visible,
)
from .skills import SkillsMixin
from .titles import (
    TitlesMixin,
    _title_cursor,
)

log = logging.getLogger(__name__)


def _running_delegates(session: Session | None) -> int:
    """How many of ``session``'s delegates are running; 0 when it is not
    open or delegates nothing."""
    if session is None or session.delegates is None:
        return 0
    try:
        return sum(1 for job in session.delegates.list() if job.status == "running")
    except Exception:  # noqa: BLE001 - the rail asks often; a count can wait
        return 0


class Registry(
    ManifestMixin, TitlesMixin, SkillsMixin, DelegationMixin, PublishingMixin
):
    """``name -> Session``; open() is lazy and idempotent."""

    def __init__(
        self,
        model_factory: Callable[..., Any],
        store: Path | str | None = None,
        default_model: str | None = None,
        apps: AppsConfig | None = None,
        delegate_turns: int = DELEGATE_TURNS,
        delegate_ttl: float | None = None,
        delegate_depth: int | None = None,
        delegate_tool_calls: int | None = None,
    ) -> None:
        self._model_factory = model_factory  # (spec) -> agno Model
        self._default_model = default_model
        # The budget every delegate runs under unless its asker names
        # one. A studio setting, not nontainer's: nontainer passes the
        # value through and never interprets it (see delegates.py).
        self._delegate_turns = delegate_turns
        # Retention for a delegate's branch, in HOURS; 0 is off. A
        # studio setting for the same reason the budget is one:
        # nontainer supplies the sweep and deliberately schedules
        # nothing, so the policy and the clock are the embedder's.
        self.delegate_ttl_hours = (
            _delegate_ttl_hours() if delegate_ttl is None else max(0.0, delegate_ttl)
        )
        # How deep delegation may nest, counted in hops from the
        # session a human started; 0 is off. A studio setting because
        # what it bounds is the studio's own doing: nontainer forks a
        # branch, and the agent, the workers and the model behind each
        # one are what this process puts on it.
        self.delegate_depth = (
            _delegate_depth() if delegate_depth is None else max(0, int(delegate_depth))
        )
        # Tool calls one delegate turn may spend; 0 is off. Only a
        # delegate's agent carries it: a human's turn has somebody
        # watching it and a stop button over it, and a delegate's turn
        # has neither.
        self.delegate_tool_calls = (
            _delegate_tool_calls()
            if delegate_tool_calls is None
            else max(0, int(delegate_tool_calls))
        )
        # Delegates the sweep flagged kept in nontainer's job table to
        # spare a subtree, which is not a keep anybody asked for (see
        # `_pin`). In memory only, because the flag it stands for is:
        # both die with this process.
        self._pinned: set[str] = set()
        # Where delegates run: every helper's runs are tasks on this
        # loop. The server's own while it serves (`serve_on`), so a
        # delegate's turns emit where the routes that follow them
        # listen; a loop of the registry's own on a thread otherwise.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._own_loop: _LoopThread | None = None
        # Set by the shutdown sweep and never cleared: a run that
        # starts after the sweep has already run answers at once, so
        # nothing the sweep could not see starts a turn afterwards.
        self._stopping = False
        #: Called with a session's name when one of its delegates
        #: answers (see ``DelegationMixin.set_wake_hook``).
        self._wake_hook: Callable[[str], None] | None = None
        # The store is the object that owns what outlives a session:
        # opening one, deleting one, and the tag scope that belongs to
        # none of them. Studio's own bookkeeping (the app dbs, the
        # transcripts, agno's cross-session tables) sits BESIDE it
        # under the same directory, which is what `.path` is for —
        # nothing the store deletes ever reaches those files.
        self._store = Store(Path(store) if store else DEFAULT_STORE)
        # Public: the router mounts alongside `resolve`, so the serving
        # half reads the same object the authoring half was built with
        # (see apps_config).
        self.apps = apps or apps_config()
        self._sessions: dict[str, Session] = {}
        # token -> (version, the frozen workspace serving it). The
        # VERSION rides along because an app's URL can be repointed
        # (see set_current) and a stale snapshot is indistinguishable
        # from a fresh one; it is also what keeps `resolve` off the
        # disk on the hot path — one dict lookup per served request,
        # the manifest read only on a miss.
        self._published: dict[str, tuple[str, Workspace]] = {}
        # Requests holding each served snapshot (by id), and snapshots
        # dropped from `_published` while held: those close when the
        # last request holding them lets go (see acquire_snapshot).
        self._holds: dict[int, int] = {}
        self._retired: dict[int, Workspace] = {}
        # store-relative db path -> the ONE open handle to that file.
        # A fork, a delegate and a published app name the db of the
        # session they came from, so several rows name one file;
        # handing them one object is what serializes their writes (see
        # _db_handle).
        self._dbs: dict[str, Db] = {}
        # Workspaces mid-construction, by name. ``workspace_for`` has to
        # answer for a session that does not exist yet: building its
        # agent constructs the toolkit, which asks the db whether it owns
        # the workspace, and the db asks back through here.
        self._opening: dict[str, Workspace] = {}
        # Reentrant: ``workspace_for`` may be called while ``open`` holds
        # this, from the same thread, on the way through _build_agent.
        self._lock = threading.RLock()
        # ONE agno db for the whole store: a branch per session, so the
        # conversation is versioned with the files it produced. It is
        # handed the store itself, so it reads through the repository
        # the workspaces write through — one pool of connections, on
        # whatever backend the store keeps its data in, closed with the
        # store. ``store/agno`` holds the cross-session tables agno
        # keeps outside a session — memories, metrics — which must not
        # rewind with any one branch.
        self.db = KvgitStoreDb(
            self._store,
            open=self.workspace_for,
            db_path=str(self._store.path / "agno"),
        )
        # First: everything below reads a session's db off its row.
        self._migrate_db_rows()
        self._migrate_published()
        self._migrate_publications()
        self._reconcile_pointers()
        # Last: the migrations above write rows that name db files, and
        # a file named by nothing is only an orphan once they have.
        self.sweep_dbs()
        # After it, and in this order: a swept delegate's row stops
        # naming its db, which is what can make that file an orphan —
        # so this one sweeps again itself when it takes anything.
        self.sweep_delegates()

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
        # Delegates are the parent's business, not the rail's: they are
        # forked by a tool call, answered on a later turn, and deleted
        # with the session that asked. Listing them would put a row in
        # the rail for every question an agent ever farmed out. By the
        # record, so a session that merely LOOKS like a child of another
        # is listed like the ordinary session it is.
        names = {n for n in names if n not in manifest["delegates"]}
        created = manifest["created"]
        forked: dict[str, int] = {}
        for entry in manifest["delegates"].values():
            forked[entry["parent"]] = forked.get(entry["parent"], 0) + 1
        rows = []
        for name in names:
            live = self._sessions.get(name)
            rows.append(
                {
                    "name": name,
                    "title": self.title_of(name, manifest),
                    "busy": live is not None and live.busy,
                    "model": (
                        live.model if live is not None else manifest["models"].get(name)
                    ),
                    # Notification is the studio's half of delegation:
                    # nontainer holds the answer until something asks for
                    # it, and this is the count the rail shows so a human
                    # can see one arrived on a session they are not
                    # looking at. It clears when the session's next turn
                    # takes the answers.
                    "delegates": len(live.answered_delegates()) if live else 0,
                    # How many this session has on record, answered or
                    # not. The badge counts what a turn would deliver;
                    # this is what there is to LIST, so the rail can
                    # offer the listing on a session whose delegates
                    # have all been read (see `delegate_rows`).
                    "delegate_count": forked.get(name, 0),
                    # Delegates at work right now: a parent waiting on
                    # them has ended its turn, and would read as done.
                    "delegates_running": _running_delegates(live),
                }
            )
        rows.sort(key=lambda r: (-created.get(r["name"], 0), r["name"]))
        return rows

    def others(self, owner: str) -> list[dict]:
        """The `others` action's rows: every session the rail lists but
        ``owner``, those working now first and then the most recently
        active, each with the apps it published.

        An agent asked to put a question to another session, or to
        build on what one did, had bare branch names to choose from.
        The title is the human's word for a session where they gave
        one, and the published app's description is the closest thing
        a session has to a summary of what it holds.

        ``active`` is the time of the session's latest commit — every
        finished turn commits — or ``None`` where the store cannot say.
        """
        apps: dict[str, list[dict]] = {}
        for app in self.list_apps():
            if app.get("session"):
                apps.setdefault(app["session"], []).append(app)
        # Where each session's delegates were forked from, when not from
        # it: a session that only ASKED another one about its work gets a
        # title from that work, and the row has to say it is second-hand.
        asked: dict[str, list[str]] = {}
        for entry in self._manifest()["delegates"].values():
            if entry.get("undone"):
                continue  # an edit unsaid the ask, as every listing reads it
            source = entry.get("from")
            seen = asked.setdefault(entry["parent"], [])
            if source and source not in seen:
                seen.append(source)
        rows = []
        for row in self.list():
            if row["name"] == owner:
                continue
            rows.append(
                {
                    "name": row["name"],
                    "title": row["title"],
                    "busy": row["busy"],
                    "delegates": row["delegate_count"],
                    "active": self._last_commit_time(row["name"]),
                    "apps": apps.get(row["name"], []),
                    "asked": asked.get(row["name"], []),
                }
            )
        # A turn in progress has not committed yet, so a busy session's
        # last commit is its previous turn: working now outranks it.
        rows.sort(key=lambda r: (not r["busy"], -(r["active"] or 0), r["name"]))
        return rows

    def _last_commit_time(self, name: str) -> float | None:
        """When ``name``'s branch last moved, unix seconds. ``None`` for
        a session with no branch yet (listed, never opened) or a store
        that keeps no commits — a missing time is a gap in a listing,
        never a reason to fail it."""
        try:
            head = next(self._store.repo.log(branch=name, limit=1), None)
        except Exception:  # noqa: BLE001 - see above
            return None
        return head.time if head is not None else None

    @property
    def stopping(self) -> bool:
        """Whether the studio is shutting down its delegates' work."""
        with self._lock:
            return self._stopping

    def serve_on(self, loop: asyncio.AbstractEventLoop) -> None:
        """Run delegates on ``loop`` from now on: the server's, set as
        it starts and before it opens a session. A helper is bound to
        its loop when it is built, so one built earlier keeps the loop
        it was built with."""
        with self._lock:
            self._loop = loop

    def delegate_loop(self) -> asyncio.AbstractEventLoop:
        """The loop a new session's delegates run on: the server's, or,
        outside one, a loop of the registry's own on a thread, started
        the first time a session needs it."""
        with self._lock:
            if self._loop is not None:
                return self._loop
            if self._own_loop is None:
                self._own_loop = _LoopThread()
            return self._own_loop.loop

    # -- titles: display only, never identity ------------------------------

    # -- the db: a store with an id of its own ------------------------------
    #
    # `db` stands in for the production database an agent acts on, and
    # nobody clones that when they open a branch. So a new session
    # mints a db with an ID of its own, and a fork, a delegate and a
    # published app NAME that same file in their own row.
    #
    # The id is minted, never derived from a session name. A name is
    # handed back the moment its session is deleted, while the file can
    # outlive it in a fork's row or a publication's — and a path spelled
    # from a name would hand the next holder of that slug somebody
    # else's rows. So the manifest is the only thing that says where a
    # db is, and every row says it.

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
            # Reserve the name against a racing mint, and mint the db
            # in the same breath: the name is free again after a
            # delete, the db id never is.
            self._record(name, db=self._mint_db_path(self._manifest()))
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
        known = self.known() | self._names_apps_keep(self._manifest())
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

    def open(self, name: str, *, minting: bool = False) -> Session:
        """Create-or-return. Raises ``SessionIdError`` for a name that
        cannot become a session: a malformed one, or a swept delegate's
        (``SweptSessionError``).

        ``minting`` says the caller is CREATING this session right now
        — :meth:`open_delegate`, which writes the record for a branch
        it is about to assemble — and stands the swept-name refusal
        down for it. That refusal is about a name arriving from
        outside for a delegate there is nothing left of.
        """
        with self._lock:
            existing = self._sessions.get(name)
            if existing is not None:
                return existing
            manifest = self._manifest()
            if (
                not minting
                and name in manifest["delegates"]
                and not self._store.exists(name)
            ):
                # A delegate whose branch the retention sweep took. The
                # create-or-return rule below would mint a fresh empty
                # session under that name, which is the one answer that
                # is wrong for it: the record says whose delegate it is
                # and the parent is still reading the exchange, so what
                # would open is a blank session wearing the name of work
                # that is gone. The UI refuses to click one; this is the
                # same refusal for every other caller.
                raise SweptSessionError(
                    f"{name} was a delegate of "
                    f"{manifest['delegates'][name]['parent']} and its branch "
                    "has been swept — there is nothing left to open"
                )
            if (
                not minting
                and name not in manifest["sessions"]
                and name in self._names_apps_keep(manifest)
            ):
                # A deleted session whose app is still published. Its
                # app row names this session, so a session created
                # under the name would publish over that app's URL and
                # serve its database. The name is the app's until the
                # app is unpublished.
                raise ReservedSessionError(
                    f"{name} was deleted, and an app it published is still "
                    "served under its name — unpublish the app to use the "
                    "name again"
                )
            model = manifest["models"].get(name) or self._default_model
            # A name that is already a session opens the file its row
            # names — its own, or a parent's. A name that is not is
            # starting empty, and mints a db of its own.
            rel = self._db_of(name, manifest) or self._mint_db_path(manifest)
            db = self._db_handle(rel)
            ws = None
            try:
                ws = self._open_workspace(name, self._python_config(db, agent=True))
                _bind_host_objects(ws)
                # Published before anything can ask: _build_agent
                # constructs the toolkit, which checks that the store db
                # owns this workspace, and the db answers by calling
                # workspace_for.
                self._opening[name] = ws
                try:
                    # <root>/ui exists from the start: agents predictably
                    # savefig into it directly (instead of assigning
                    # objects to `ui`), and VFS open honors real-fs
                    # semantics — no parent, no write. Forgive the
                    # near-miss.
                    if not ws.files.fs.isdir(f"{ws.root}/ui"):
                        ws.files.fs.makedirs(f"{ws.root}/ui", exist_ok=True)
                        ws.commit(info={"tool": "init"})
                    session = self._assemble(name, ws, db, model)
                    loaded = self._load_events(session.log_path)
                    session.events.extend(loaded)
                    session.next_seq = (loaded[-1]["seq"] + 1) if loaded else 0
                    session.flush_idx = len(session.events)  # loaded = on disk
                    self._sessions[name] = session
                finally:
                    self._opening.pop(name, None)
            except BaseException:
                # A store that will not build, a guest image that will
                # not come up, a bad skill: nothing the attempt opened
                # may outlive it. An open workspace pins its branch, and
                # every verb that removes one closes the session first —
                # a step nobody can take for a session that does not
                # exist. A handle left in the map holds its file open
                # until the process ends AND keeps the sweep off it, so
                # one failed open would leak a store nothing can reach.
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        # The reason the open failed is the one worth
                        # reading; a cleanup that throws on top of it
                        # sends the reader after the wrong fault.
                        log.warning("open %s: closing the workspace failed", name)
                # No-ops where the file is a parent's and the parent is
                # live, which is what fork and open_delegate hand it.
                self._forget_db(rel)
                raise
            self._record(name, model, db=rel)
            return session

    @staticmethod
    def _python_config(db: Db, *, agent: bool = False) -> PythonConfig:
        """Safe stdlib + the data stack when installed (opportunistic:
        `pip install pandas matplotlib` and the agent's Python grows —
        the run_python tool description self-updates from the grants).
        Presets run their environment side effects here, at session
        construction: matplotlib gets Agg-pinned and font-warmed before
        any sandboxed code runs.

        ``agent`` is for a session an agent works in, as opposed to a
        published snapshot. Only an agent session gets the host objects
        that spend the operator's API key (see ``_agent_host_objects``):
        a published app serves anyone holding its link."""
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
        extra = _agent_host_objects() if agent else {}
        return PythonConfig(
            modules=modules,
            # A second store beside the live one, empty and in memory,
            # for tests: what `call(..., db=testdb)` takes, so a test
            # never seeds rows into the store every published version
            # serves over and never reaches for sqlite3 itself, which
            # the sandbox refuses because a connection's own SQL
            # (ATTACH, .backup) reaches the host filesystem beneath the
            # workspace. It lives on this side; the sandbox sees the
            # same three methods plus reset().
            host_objects={
                "db": db,
                "testdb": Db(":memory:", live=db),
                **extra,
            },
            # A host call's wait counts against the timeout, and a deep
            # search alone runs 20-40s.
            **({"timeout": AGENT_PYTHON_TIMEOUT} if extra else {}),
            isolation=isolation,
            # Import the granted stack ONCE into sandtrap's forkserver
            # broker; every worker then inherits it copy-on-write. With
            # dataframes()+plotting() granted that is the difference
            # between a worker costing ~233ms / 111MB and ~12ms / 29MB
            # (measured on this venv), and studio holds a session worker
            # for the life of every workspace that has run a turn — so
            # it is memory, not just latency. The safety caveat is
            # grants whose IMPORT starts a thread, which would leave the
            # broker multi-threaded; studio grants only nontainer's own presets,
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

    def _open_workspace(self, name: str, python: Any) -> Workspace:
        """Open ``name``'s workspace with its starter skills mounted and
        the studio's ignore patterns.

        The starter set depends on the executor and on whether the
        session gets ``ws-git``, and mounts are fixed when a workspace
        is built, so both are predicted from the configuration first.
        The session's own answers are what decide (a gated skill reads
        them), so where they differ from the prediction, which takes an
        executor that cannot carry the verb, the workspace is opened
        again with the right set. Opening is cheap: nothing runs until
        the first tool call.
        """
        modules = getattr(python, "modules", None) or ()

        def opened(commands: bool, wsgit: bool) -> Workspace:
            return self._store.open(
                name,
                python=python,
                mounts=self._skill_mounts(
                    commands=commands, wsgit=wsgit, modules=modules
                ),
                ignore=WORKSPACE_IGNORE,
                **_ws_kwargs(),
            )

        guess = (executor_runs_commands(), wsgit_enabled())
        ws = opened(*guess)
        got = (bool(ws.runtime.supports_commands), self._wsgit(ws))
        if got != guess:
            ws.close()
            ws = opened(*got)
            self._wsgit(ws)
        return ws

    @staticmethod
    def _wsgit(ws: Workspace) -> bool:
        """Put ``ws-git`` in this workspace's terminal, and say whether
        it is there.

        nontainer leaves the registration to the embedder and no adapter
        calls it, so the verb exists only where the studio asks for it:
        ``NONTAINER_STUDIO_WSGIT`` decides whether to ask, and
        ``register_wsgit`` answers whether this executor can carry it.
        The primer teaches the verb under this answer, so does a
        delegate's brief, and so does a skill whose workflow is the
        verb — nothing re-derives it from the knob.

        Safe to ask twice, which opening a session does: registration is
        a no-op once the verb is there, and the answer does not change
        within a session.
        """
        return wsgit_enabled() and register_wsgit(ws)

    def _assemble(
        self, name: str, ws: Workspace, db: Db, model: str | None = None
    ) -> Session:
        # Apps dispatch works on both executors (stage 3c dissolved the
        # LocalExecutor-only sandbox surface into exec_python(view=)).
        # self.apps, not a fresh AppsConfig: the router serves published
        # snapshots under this same declaration (see apps_config).
        runtime = enable_apps(ws, self.apps)
        wsgit = self._wsgit(ws)
        log_dir = self._store.path / "events"
        log_dir.mkdir(parents=True, exist_ok=True)
        # One helper per session, built here so the studio holds it: the
        # adapter would build its own from the runner and keep it where
        # nothing else could read the answers back or close the workers.
        delegates = Sessions(
            ws,
            StudioRunner(self, name, self._delegate_turns),
            budget=self._delegate_turns,
            loop=self.delegate_loop(),
            # nontainer says when an answer lands; whether that starts a
            # turn is the studio's (see DelegationMixin._answer_landed).
            on_answer=lambda job, answer: self._answer_landed(name),
        )
        # Built here and handed to the toolkit rather than taken from
        # it: a model switch rebuilds the toolkit, and a queue that
        # moved with it would drop whatever was waiting.
        inbox = Inbox()
        session = Session(
            name=name,
            ws=ws,
            runtime=runtime,
            driver=self._build_driver(
                name, ws, runtime, model, delegates, wsgit=wsgit, inbox=inbox
            ),
            db=db,
            turn_lock=threading.Lock(),
            model=model,
            wsgit=wsgit,
            delegates=delegates,
            inbox=inbox,
            log_path=log_dir / f"{name}.jsonl",
        )
        # What an edit unsaid stays unsaid across a restart.
        session.undone_delegates = {
            child
            for child, entry in self._manifest()["delegates"].items()
            if entry["parent"] == name and entry.get("undone")
        }
        return session

    _load_events = staticmethod(_load_events)

    def _sessions_tool(self, owner: str, delegates: Any) -> Callable:
        """The agent's handle on delegation, and on what the human has
        published.

        nontainer's toolkit registers a tool of this name over the same
        helper; the studio registers this one instead, because one
        action needs an answer only the studio can give. `published`
        reads the app registry, which is the studio's half of
        publishing and nothing nontainer holds; every other action is
        dispatched by nontainer, unchanged, with the arguments the
        model sent.

        The closure captures the session's own helper — the job table
        the studio reads answers out of — and ``self``, both stable
        across the model-switch rebuild. ``owner`` is the session the
        tool belongs to, spelled apart from the tool's own ``name``
        argument, which is the child a caller is addressing.
        """

        # The signature is nontainer's, argument for argument, so one
        # spelling works wherever this agent runs. `paths` is annotated
        # loose for nontainer's reason: models send lists as JSON
        # strings, and pydantic would reject one on the annotation
        # before `coerce_paths` got its chance.
        def sessions_tool(
            action: str,
            task: str = "",
            name: str = "",
            paths: "list[str] | str | None" = None,
            inherit: str = "",
            fork_from: str = "",
            resume: str = "",
            wait: bool = False,
        ) -> str:
            """Delegate to a fork of this session, and read it back."""
            if action == "published":
                return _render_published(self.list_apps())
            if action == "others":
                return _render_others(self.others(owner), time.time())
            if action == "ask" and self.at_depth_cap(owner):
                # Before nontainer sees it: the fork is the expensive
                # half, and a refusal the agent can read and act on
                # beats a branch with an agent on it that nobody
                # wanted. Only `ask` — reading, keeping and cancelling
                # what this session already has cost nothing and are
                # how it finishes with them.
                return _depth_refusal(self.delegate_depth)
            return run_action(
                delegates,
                action,
                task=task,
                name=name,
                paths=paths,
                inherit=inherit,
                fork_from=fork_from,
                resume=resume,
                wait=wait,
            )

        sessions_tool.__name__ = "sessions"
        sessions_tool.__doc__ = SESSIONS_TOOL_DESCRIPTION
        return sessions_tool

    def _build_driver(
        self,
        name: str,
        ws: Workspace,
        runtime: AppRuntime,
        model: str | None = None,
        delegates: Any = None,
        *,
        wsgit: bool = False,
        inbox: Inbox,
    ) -> Any:
        """The loop a session's turns drive: its agno agent, as a
        driver. The driver takes the inbox's deliveries into the turn's
        stream, so it is built over the session's own inbox."""
        from .drivers import AgnoDriver

        agent = self._build_agent(
            name, ws, runtime, model, delegates, wsgit=wsgit, inbox=inbox
        )
        return AgnoDriver(agent, inbox, name)

    def _build_agent(
        self,
        name: str,
        ws: Workspace,
        runtime: AppRuntime,
        model: str | None = None,
        delegates: Any = None,
        *,
        wsgit: bool = False,
        inbox: Inbox | None = None,
    ) -> Any:
        """``wsgit`` is whether the ``ws-git`` verb was installed on this
        workspace, which decides the primer's ws-git half and which
        delegation half is true. It defaults to the conservative answer:
        an agent that is not told about a verb it has loses a spelling,
        where one told about a verb it lacks loses a turn.

        ``inbox`` is the session's queue of mid-run messages; the
        toolkit is given it rather than minting its own so the queue
        survives the rebuild a model switch does."""
        from agno.agent import Agent

        from . import providers

        toolkit = WorkspaceTools(
            ws,
            apps=runtime,
            # No `sessions` here. The studio registers a tool of that
            # name itself, over the session's own helper, because one
            # of its actions reads the app registry; passing a helper
            # here would register nontainer's beside it and the model
            # would be handed the name twice.
            python_primer=_python_primer(),
            # The conversation commits with the files. Naming the db
            # here is what stands the toolkit's own turn hook down:
            # agno runs post hooks BEFORE it persists the run, so a
            # hook-driven commit would carry the turn's files without
            # its memory. The db commits at the persist instead, and
            # the commit mode stays per mutating call — this is the
            # turn's trailing commit, so the head stamped on the next
            # `user` event includes the conversation.
            session_db=self.db,
            # text-only models must not receive screenshot media — the
            # call AFTER an image-bearing tool result 400s ("no
            # endpoints support image input"), losing the turn. Model
            # switches rebuild the agent, so this stays correct.
            vision=providers.supports_vision(model or self._default_model),
            inbox=inbox,
        )
        # Assigned rather than passed as `sessions=`: the toolkit reads
        # this to take delegate answers at a tool result — the answer
        # arrives mid-turn instead of on the next one — while passing
        # it to the constructor would ALSO register nontainer's own
        # `sessions` tool beside the studio's.
        if delegates is not None:
            toolkit.sessions = delegates
        # The `sessions` tool is registered only where there is a helper
        # to delegate through, which is nontainer's gate for it too: an
        # agent told to delegate with nothing to delegate to spends a
        # call finding out. NONTAINER_STUDIO_SESSIONS is the studio's
        # own gate on top of that — the helper is built either way, and
        # with the knob off nothing hands the agent a name for it.
        delegation = delegates is not None and sessions_tool_enabled()

        # Compaction: past a per-model budget, every earlier turn is
        # folded into one summary for the model, while the transcript
        # keeps everything (nontainer's docs/compaction.md). The marker
        # goes to whichever session is live under this name when a fold
        # happens: the first build runs before the session exists, and
        # a model switch rebuilds this agent.
        compaction = None
        policy = providers.compaction_policy(model or self._default_model)
        if policy is not None:
            from nontainer.adapters.agno_compaction import CompactingCompression

            compaction = CompactingCompression(
                ws, policy, on_fold=lambda fold: self._fold_landed(name, fold)
            )

        # A turn is one agno run and one agno run is a tool loop with
        # no bound of its own. A human's session needs none — somebody
        # is watching it and the stop button reaches it — but a
        # delegate's turn has neither, so the calls it may spend are
        # capped. Whether this session is a delegate is the record the
        # studio wrote when it opened one, which is written before the
        # open that builds this agent.
        tool_calls = self.delegate_tool_calls if self.is_delegate(name) else 0

        return Agent(
            model=self._model_factory(model),
            tools=[toolkit]
            + ([self._sessions_tool(name, delegates)] if delegation else []),
            compression_manager=compaction,
            # Tool calls this run may spend, and None where there is
            # no cap. Past the limit agno answers each further call
            # with a tool result saying the limit is reached and not
            # to retry, and the run carries on from there — so this
            # bounds what a delegate DOES, and a model that keeps
            # calling tools past the refusal is still in its own loop
            # until it writes prose or the provider stops it. The turn
            # budget is the other end of that: a turn that ends
            # without prose spends one, and running out of turns
            # resolves the answer as `capped`.
            tool_call_limit=tool_calls or None,
            # `begin_turn` puts back in the queue any note a run handed
            # out and then dropped. agno runs pre hooks when a run
            # starts and not when a run is continued, so a turn resumed
            # after a provider error does not come through here.
            pre_hooks=[toolkit.begin_turn],
            # `end_turn` commits nothing here — the session db owns the
            # commit — so all it does is settle the notes this turn
            # delivered: the turn that read them is over, and nothing
            # will hand them to the model a second time.
            post_hooks=[toolkit.end_turn],
            # Delivery itself, on the SYNC spelling even though the
            # studio drives `arun`. agno runs a sync tool through
            # `asyncio.to_thread` — unless a tool hook is a coroutine
            # function, and then it runs the whole call inline on the
            # event loop instead. Every tool here is sync, so the async
            # hook would hold the server's loop for the length of each
            # one: no streaming, no stop button, no other session
            # moving. The sync hook keeps the tool in its thread, and
            # the queue is drained there.
            tool_hooks=[toolkit.deliver],
            # studio-owned context: nontainer's tool descriptions cover
            # the MECHANICS (workspace, handlers, curl); this covers the
            # product the human is looking at (preview, artifacts,
            # commits, publish)
            # The retention sentence rides with delegation: what it
            # describes is a delegate's branch, and an agent with no way
            # to fork one has nothing for it to be about.
            instructions=(
                STUDIO_PRIMER
                + _versioning_primer(wsgit)
                + _unit_test_primer(ws)
                + _delegation_primer(delegation, wsgit)
                + _depth_primer(delegation and self.at_depth_cap(name))
                + (_retention_primer(self.delegate_ttl_hours) if delegation else "")
            ),
            # Durable chat, keyed by the session name and stored in that
            # session's own workspace branch: after a server restart the
            # agent still remembers the conversation (and the jsonl
            # event log restores the visible transcript), and a rewind
            # of the files is a rewind of the memory — one checkout, not
            # two writes that can disagree.
            db=self.db,
            session_id=name,
            add_history_to_context=True,
            # Every earlier run, not agno's default of the last 3. A
            # window that slides each run forgets the start of the
            # conversation, and a delegate's answer wakes the parent as
            # a run of its own, so after one round of delegation the
            # agent no longer remembered asking. It also changes the
            # prompt's prefix on every run, so no turn hit the prompt
            # cache. agno slices `runs[-n:]`; the largest int is all.
            num_history_runs=sys.maxsize,
            markdown=True,
            # No run-level retry (agno's `retries` stays at its default
            # of 0). A run-level retry restarts the run from the user
            # message: it forgets every tool call the failed attempt
            # made while the files those calls wrote stay in the
            # workspace, and undoing the files instead would also undo
            # whatever else was written during the turn. A provider
            # failure is an interruption, not a restart:
            # - the model call retries a transient error itself and
            #   keeps the turn's tool results (providers._with_retries);
            # - past that the run ends in error, and the turn resumes
            #   the SAME run in place, from where it stopped, after a
            #   back-off, as many times as turns.RESUME_BACKOFFS has
            #   entries (turns._resume);
            # - if every resume fails too, the run is kept in the
            #   agent's memory as an interrupted turn, and the workspace
            #   keeps what it wrote.
        )

    def _fold_landed(self, name: str, fold: Any) -> None:
        """A fold was made in ``name``'s turn: hand it to the turn's
        stream, which marks it in the transcript."""
        session = self.get(name)
        if session is not None:
            session.driver.folded(fold)

    # -- delegates: sessions the registry did not create ----------------------

    # -- retention: a delegate's branch is not forever ---------------------
    #
    # nontainer supplies the mechanism and schedules nothing:
    # `Sessions.sweep(idle)` deletes the branch of every finished,
    # unheld, unkept job in ITS table whose `Job.touched` is older than
    # `idle`, marks it `expired` and drops its answer. The policy and
    # the clock are the studio's, and so is one thing the mechanism
    # cannot reach: a job table is per `Sessions` object, which this
    # process builds per LIVE session, so a delegate asked for before
    # the last restart is in no table at all. The manifest is what
    # remembers it, and the two sweeps below are the two halves of one
    # rule.

    def release(self, name: str) -> None:
        """Close a session's live handles and leave everything on disk.

        :meth:`delete`'s opposite number: the branch, the app db and the
        transcript all stay, and reopening the name rebuilds the session
        over them. This is what a finished delegate wants — its branch
        is what the parent merges from, and an agent, a workspace handle
        and a sqlite connection per delegate that has already answered
        outlive every reason to hold them.

        The db handle goes only if nothing live is running over it: a
        delegate's db is the file its parent's row names too.
        """
        with self._lock:
            session = self._sessions.pop(name, None)
            rel = self._db_of(name, self._manifest())
        if session is None:
            return
        self._close_session(session)
        if rel is not None:
            with self._lock:
                self._forget_db(rel)

    @staticmethod
    def _close_session(session: Session) -> None:
        """Release one session's handles, in dependency order."""
        if session.delegates is not None:
            # waits for the delegates' runs; their branches stay, because
            # a delegate's work is in the store and closing the helper
            # that asked for it must not throw that away
            session.delegates.close()
        close_runtime = getattr(session.runtime, "close", None)
        if callable(close_runtime):  # reap dispatch workers
            close_runtime()
        _unbind_host_objects(session.ws)
        session.ws.close()
        # NOT the db: the handle belongs to the registry, because the
        # file behind it is named by other rows — a fork's, a
        # delegate's, a published app's — that may still be running.

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

        The app db is NAMED, not copied: it is a handle to an external
        store, and branching a session no more clones that store than
        branching a repo clones production. Both universes write to the
        one file.

        Forking is a between-turns verb. A turn in flight owns the
        workspace, and kvgit refuses to fork a branch with staged
        changes — a fork of half a turn would be a state no commit
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

    def _fork_locked(
        self,
        session: Session,
        *,
        conversation: str,
        at: str | None = None,
        transcript: list[dict] | None = None,
    ) -> Session:
        """:meth:`fork` with the parent already reserved.

        ``at`` branches from an earlier commit of the parent rather
        than its head — the child gets the files and the conversation as
        they stood there, with its own identity written on top, and the
        parent is not touched at all (see :meth:`branch_from_version`).
        Staged work is only in the way of a fork from the HEAD, which
        has to commit it to see current state; a fork from a named
        past leaves it exactly where it is.

        ``transcript`` gives the child that log instead of a copy of the
        parent's, which is what a fork from a past commit needs: the
        parent's log runs on past it, and appending a cut cannot undo an
        EARLIER cut already in it (a truncate only pops what is still
        visible), so a child forked behind an edit would show a
        transcript that stops before the state it actually holds.
        """
        if at is None and session.ws.uncommitted:
            raise RuntimeError("can't fork mid-turn: the workspace has staged changes")
        with self._lock:
            name = self._mint_name()
            rel = self._db_of(session.name, self._manifest())
            self._record(name, session.model, db=rel)
        try:
            # A fresh fork holds no conversation at all, and the agent
            # starts one on the child's first turn.
            child_ws = session.ws.fork(
                name, at=at, inherit="full" if conversation == "inherit" else "fresh"
            )
            # The fork inherits the PARENT's python config, and with it
            # a `db` host object built for another session's workspace.
            # Let it go and reopen over a config of the child's own —
            # naming the same FILE, through the same handle, so the two
            # universes' writes queue rather than race.
            child_ws.close()
            with self._lock:
                db = self._db_handle(rel)
            ws = self._open_workspace(name, self._python_config(db, agent=True))
            _bind_host_objects(ws)
            with self._lock:
                self._opening[name] = ws
                try:
                    child = self._assemble(name, ws, db, session.model)
                    # The visible transcript must match the memory the
                    # child inherited, or the human reads a blank page
                    # over an agent that remembers everything.
                    if conversation == "inherit" and child.log_path is not None:
                        if transcript is not None:
                            with child.log_path.open("w") as f:
                                for event in transcript:
                                    f.write(json.dumps(event) + "\n")
                        elif session.log_path is not None and session.log_path.exists():
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
        branch, the transcript, the agent's chat record. Caller ensures
        not busy.

        Its DB is not among them, and no deletion removes one. It is a
        handle to an external store that a fork, a delegate or a
        published app may name too; a file no row names any more is
        collected by :meth:`sweep_dbs`, which reads the whole manifest
        rather than guessing from here.

        Its published APPS are not among them either. Each version is a
        publication version on a branch of its own that belongs to no
        session, and the app's row goes on naming the db — so the URLs
        someone was handed keep serving, over the same store, after the
        conversation that built them is gone.
        Taking one down is ``unpublish``, said about the app.

        Its DELEGATES are. A delegate has no row in the rail of its own
        and exists to answer the session that forked it, so it goes when
        that session goes, and the whole subtree with it (a delegate may
        delegate). By the RECORD of who forked whom, not by the shape of
        the names: a session that merely looks like a child of this one
        is an ordinary session and stays. Deleting a delegate on its own
        works the same way and takes its record with it.
        """
        name = session.name
        doomed = [name] + self.delegates_of(name)
        for victim in doomed:
            # The conversation lives in the branch, and the branch
            # deletion below takes it with everything else.
            with self._lock:
                live = self._sessions.pop(victim, None)
                manifest = self._manifest()
                rel = self._db_of(victim, manifest)
                manifest["sessions"].pop(victim, None)
                manifest["models"].pop(victim, None)
                # titles/created go too, or a later mint that happens to
                # draw this slug (it's free again once `sessions` forgets
                # it) would inherit a dead session's name and birthday
                manifest["titles"].pop(victim, None)
                manifest["created"].pop(victim, None)
                manifest["delegates"].pop(victim, None)
                self._pinned.discard(victim)
                self._save_manifest(manifest)
            if live is not None:
                # before branch deletion: an open workspace holds its branch
                self._close_session(live)
            if rel is not None:
                with self._lock:
                    self._forget_db(rel)
            (self._store.path / "events" / f"{victim}.jsonl").unlink(missing_ok=True)
        self._delete_branches(set(doomed))

    def _delete_branches(self, names: set[str]) -> None:
        """Remove sessions from the store. Deletion is nontainer's: the
        store deletes the branches it opened, and takes each branch's
        session-scoped tags with it while leaving the store scope
        alone — and a publication lives on a branch of its own, which
        is what keeps a published app up after its session is
        deleted. It is a store-level verb, not a live-session one, so
        every caller closes the session's workspace first: an open
        handle pins its branch."""
        self._store.delete(names)

    # -- model switching ----------------------------------------------------

    def set_model(self, session: Session, spec: str) -> None:
        """Rebuild the session's driver on a different model. The chat
        db is keyed by session_id, so the new driver keeps the whole
        conversation — switch models mid-project freely. Raises
        ValueError (via the model factory) on an unknown spec."""
        session.driver = self._build_driver(
            session.name,
            session.ws,
            session.runtime,
            spec,
            session.delegates,
            wsgit=session.wsgit,
            inbox=session.inbox,
        )
        session.model = spec
        with self._lock:
            manifest = self._manifest()
            manifest["models"][session.name] = spec
            self._save_manifest(manifest)

    # -- edit: rewind + retry as one verb -----------------------------------

    def rewind_to_event(self, session: Session, seq: int) -> None:
        """The rewind half of an EDIT: check out the user event's
        pre-turn head. That one call is the whole rewind — the agent's
        memory lives in the same branch as the files, so the turns
        after that head are unsaid by the same checkout that unwrites
        their files. The caller emits the `truncate` event and starts
        the new turn.

        The commit id, not commit order, is the anchor: the `user` event
        stamps the workspace as it stood before its turn ran, which is
        exactly the state the edited prompt should run from. It stays a
        valid anchor no matter how often the session has been rewound,
        because a checkout appends rather than dropping what came after
        it — every head the transcript ever stamped is still in the log.

        The agent's title rewinds too — it named the session from a
        conversation that is being unsaid."""
        event = next((e for e in session.snapshot() if e.get("seq") == seq), None)
        head = event.get("head") if event else None
        if event is None or event.get("type") != "user" or not head:
            raise ValueError(f"event {seq} is not an editable user message")
        self._rewind(session, seq, head)

    def _rewind(self, session: Session, seq: int, head: str) -> None:
        """Put the files, the agent's memory and the session's name back
        where they stood at ``head``, with the transcript cut at ``seq``.

        One ``checkout`` covers the first two: the conversation lives in
        the same branch as the files. The name is a third thing, kept in
        the manifest, so it is put back by hand — it was read out of a
        conversation that is being unsaid.

        What the human sees is a rewind; what the branch records is a
        new commit holding the old content. Nothing is lost either way,
        and the redo is the same verb said about the commit this one
        stepped off.
        """
        cut = next((e for e in session.snapshot() if e.get("seq") == seq), None)
        surviving = None
        prior = [e for e in session.snapshot() if e["seq"] < seq]
        for _, ev in self._visible(prior):
            # A title event carries the label that was in force and,
            # under ``agent``, the generated name beneath it — plus the
            # transcript cursor that name was read from. An event from
            # before the two were told apart carries only the label,
            # and back then that WAS the generated name.
            if ev.get("type") == "title" and (ev.get("agent") or ev.get("title")):
                surviving = ev
        session.ws.checkout(head)
        # The delegates asked for after the edited message go with the
        # conversation that asked for them. A message from before events
        # were stamped with a time cannot place them, so none is unsaid.
        if cut is not None and cut.get("ts") is not None:
            self.undo_delegates_since(session, cut["ts"])
        # Best-effort within the event window: revert to the last name
        # generated BEFORE the cut. None surviving is ambiguous — never
        # named, or named so long ago the event front-trimmed out
        # (MAX_EVENTS) — so keep what the manifest says rather than wipe
        # a name we can't prove was undone.
        if surviving is not None:
            self._restore_generated_title(
                session.name,
                surviving.get("agent") or surviving.get("title"),
                _title_cursor(surviving),
            )

    _visible = staticmethod(_visible)

    # -- migration: anchor branches become apps -----------------------------

    # -- migration: tagged versions become publications ---------------------

    # -- publish: an app is a lineage of versions ---------------------------
    #
    # An APP is a publication with a stable URL: one capability token,
    # one db of its own, and a growing set of VERSIONS. A version is a
    # version of a nontainer PUBLICATION — a derived commit holding the
    # files under `app/` and the filesystem rows that describe them,
    # on a branch of its own that belongs to no session. So the URL
    # freezes the app and not the conversation that built it: the
    # notes, the uploads, the skills and the transcript sitting outside
    # `app/` are not in what a capability URL hands out, and deleting
    # the session leaves every version of the app exactly as it was.
    #
    # A SESSION HAS ONE APP. Publishing is always a new version of it,
    # and the only way to a second app is to fork the session: the app
    # entry names the session it came from, and everything downstream
    # reads that one row — the publish marker, the count of app files
    # changed since a version, and branching from a version back into
    # the conversation that made it. A session holding two apps would make
    # each of those ask "which one", with no answer the human ever
    # gave. Installs from when the lineage could be chosen may still
    # hold several apps for one session; the newest by publish is the
    # one it goes on extending, and the rest keep serving.
    #
    # The row's name is the session's for as long as the app lives.
    # Deleting the session keeps the row, and a name the row still
    # holds is neither minted nor accepted for a new session: one
    # would extend the dead session's app, publishing over a URL
    # somebody holds and serving that app's database. Unpublishing
    # drops the row and hands the name back.
    #
    # nontainer's registry is generic — a name, its versions, and which
    # one is current. The token, the route and the db are the studio's,
    # and live in the app entry here, keyed by token: the publication
    # is named for the token (see `_pub_name`), which is what ties the
    # two tables together.
    #
    # The app NAMES a db, it does not own one. `db` is a handle to an
    # external store, and a published app is a deployment against that
    # store: the app entry records the file the session was using,
    # every version of the app serves over it, and that row is what
    # keeps the file from being swept once the session is deleted.
    # Schema migration across versions is the app's own business, as it
    # is in any deployment — which is what the DB_PRIMER tells the
    # agent about `db`: live state, no history, nothing rewinds it.
    #
    # An app published before db ids holds a COPY at
    # dbs/apps/<token>.sqlite. Its row names that file, so it opens,
    # serves and is collected exactly like any other — a copy already
    # made is a fact, and merging its rows into anything is not the
    # studio's to do.

    # -- serving: the URL is the app's, the state is a version's ------------

    # -- the registry of apps ----------------------------------------------

    # -- moving and removing publications ----------------------------------

    # -- branching a version back into a conversation -----------------------

    async def aclose(self) -> None:
        """:meth:`close`, from the loop the delegates run on: their runs
        are stopped, then awaited there, and the rest of the close runs
        on a thread. The server closes through this, since closing a
        helper from its own loop would wait on runs that loop is the
        one to finish."""
        self.stop_delegate_runs()
        with self._lock:
            helpers = [
                s.delegates for s in self._sessions.values() if s.delegates is not None
            ]
        await asyncio.gather(*(h.aclose() for h in helpers), return_exceptions=True)
        await asyncio.to_thread(self.close)

    def close(self) -> None:
        # First, before anything joins anything: closing a session
        # waits for its delegates' runs, and a delegate mid-turn would
        # hold that wait for the rest of its turns. Stopping the runs
        # is what turns Ctrl-C into a wait of seconds.
        self.stop_delegate_runs()
        # Sessions go OUTSIDE the lock. Closing one waits for its
        # delegates' runs, and a run still starting is inside
        # `open_delegate` on a thread, waiting for this same lock —
        # holding it across the wait is a deadlock between the two.
        with self._lock:
            live = list(self._sessions.values())
            self._sessions.clear()
        for session in live:
            self._close_session(session)
        with self._lock:
            for _, snapshot in self._published.values():
                snapshot.close()
            self._published.clear()
            # Shutting down: a request still holding one of these is
            # being cut off with the server anyway.
            for snapshot in self._retired.values():
                snapshot.close()
            self._retired.clear()
            self._holds.clear()
            for db in self._dbs.values():
                db.close()
            self._dbs.clear()
            # Last: the store outlives every workspace opened through
            # it, so it closes once nothing is still holding a branch.
            self._store.close()
            own, self._own_loop = self._own_loop, None
        if own is not None:
            own.stop()


class _LoopThread:
    """An event loop on a daemon thread of its own: where delegates run
    when no server loop is there to run them on."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, name="studio-delegates", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the loop and close it. Called once every helper on it
        has closed, so no run is left to cut short."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join()
        self.loop.close()
