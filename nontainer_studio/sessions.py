"""Session registry: one Workspace + AppRuntime + agno Agent + SQLite
store + event log per session.

Ownership model, on display:

- WORKSPACE (files, cache, cwd): durable and versioned — a kvgit
  branch per session; an edit's rewind applies here.
- APP DB (``db`` host object): durable but HISTORYLESS — live app
  state that survives publish and never time-travels. Fresh per
  session, shared with published snapshots (a view of the same
  universe), untouched by rewinds.
- CONVERSATION + event log: durable but session-scoped — agno persists
  chat (SqliteDb when sqlalchemy is installed, JsonDb otherwise) keyed
  by session_id, and the transcript event log appends to a jsonl per
  session. Rewinds are SYNCHRONIZED: the agent's memory (agno runs)
  rewinds with the workspace. An EDIT (rewind_to_event) trims the
  visible transcript too — via an appended `truncate` event, never by
  mutating the log.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import petname
from nontainer import PythonConfig, Workspace, validate_session_id, workspace
from nontainer.adapters.agno import WorkspaceTools
from nontainer.apps import AppRuntime, enable_apps, mint_token

DEFAULT_STORE = Path.home() / ".nontainer-studio"

MAX_EVENTS = 10_000  # in-MEMORY tail window, not a lifetime cap

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
    "live. Anything under /app serves in a PREVIEW PANE beside the "
    "chat as you build it — they watch it take shape. After changing "
    "the app, always verify with test_app before saying it works, and "
    "assert on DATA-bearing elements (a chart rendered, a count "
    "non-zero), not just static text — a page can look loaded while "
    "every fetch failed. When endpoints misbehave, tail "
    "/app/logs/api.log: handler errors, prints, and dispatch notes "
    "land there. Files the human uploads arrive under /uploads/. In "
    "run_python, set `ui = {...}` (figure/DataFrame/image values) to "
    "render results inline in your reply. For chat reports, match the "
    "artifact to the story: when it's a few headline numbers, LEAD "
    "with a card row (stat dicts, sublabel for the trend or context) "
    "and use a callout for the one caveat or insight that shouldn't "
    "be buried in prose; when the SHAPE of the data is the story, "
    "prefer raw plotly figures in `ui` — they render interactively "
    "right in the reply. Need a static image file instead? Use "
    "matplotlib savefig; plotly's write_image cannot run here. Every "
    "turn is a checkpoint the human can rewind by editing an earlier "
    "prompt — prefer small complete "
    "steps over big-bang changes. They may also PUBLISH the app: a "
    "frozen copy of the code serving live, behind a share URL, over "
    "the same live `db`."
)

DB_PRIMER = (
    "`db` is a shared SQLite store for LIVE app state — it survives "
    "publish and is shared with published copies of your app; it does "
    "NOT time-travel with checkpoints. Use it (not `cache`) for any "
    "state the app's users mutate. `cache` is versioned workspace "
    "data: it rewinds with restores and freezes at publish. API: "
    "`db.execute(sql, params=())` for writes (INSERT / UPDATE / "
    "`CREATE TABLE IF NOT EXISTS`), `db.query(sql, params=()) -> list "
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
    ) -> None:
        self._model_factory = model_factory  # (spec) -> agno Model
        self._default_model = default_model
        self._store = Path(store) if store else DEFAULT_STORE
        self._sessions: dict[str, Session] = {}
        self._published: dict[str, Workspace] = {}  # token -> frozen snapshot
        self._lock = threading.Lock()

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
        """{"sessions": [...], "published": {token: {branch, session,
        checkpoint}}, "models": {name: spec}, "titles": {name: {user,
        agent}}, "created": {name: epoch}} — tolerant of the v1
        bare-list format, and of any key simply being absent."""
        try:
            data = json.loads(self._manifest_path().read_text())
        except Exception:
            data = {}
        if isinstance(data, list):  # v1: just session names
            data = {"sessions": data}
        return {
            "sessions": data.get("sessions", []),
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
        return self.open(name)

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
            ws = workspace(name, store=self._store, python=self._python_config(db))
            # /ui exists from the start: agents predictably savefig
            # into it directly (instead of assigning objects to `ui`),
            # and VFS open honors real-fs semantics — no parent, no
            # write. Forgive the near-miss.
            if not ws.fs.isdir("/ui"):
                ws.fs.makedirs("/ui", exist_ok=True)
                ws.checkpoint(info={"tool": "init"})
            # Seed skills once, at session CREATION — after that they
            # are the session's own versioned state (agents may edit or
            # add them; a reseed would clobber that).
            if not ws.fs.isdir("/skills"):
                self._seed_skills(ws)
            session = self._assemble(name, ws, db, model)
            loaded = self._load_events(session.log_path)
            session.events.extend(loaded)
            session.next_seq = (loaded[-1]["seq"] + 1) if loaded else 0
            session.flush_idx = len(session.events)  # loaded = on disk
            self._sessions[name] = session
            self._record(name, model)
            return session

    @staticmethod
    def _seed_skills(ws: Workspace) -> None:
        """Install starter skills into a fresh session: each child
        directory of NONTAINER_STUDIO_SKILLS (default: the repo's
        skills/) plus any skills EMBEDDED in granted python libraries
        (<pkg>/skills/ — the nontainer convention). Best-effort: a bad
        skill must never block a session."""
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
        # Crash containment: agent code runs in a forked worker (the
        # workspace fs, cache, and db stay host-side, RPC-bridged) — a
        # segfault or OOM in C-extension guts costs the turn, not the
        # server. NONTAINER_STUDIO_ISOLATION=none opts out; =kernel
        # adds syscall/network lockdown on top.
        isolation = os.getenv("NONTAINER_STUDIO_ISOLATION", "process")
        if isolation not in ("none", "process", "kernel"):
            isolation = "process"
        return PythonConfig(
            modules=modules, host_objects={"db": db}, isolation=isolation
        )

    def _assemble(
        self, name: str, ws: Workspace, db: Db, model: str | None = None
    ) -> Session:
        runtime = enable_apps(ws)
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

    def _chat_db(self) -> Any:
        """agno's durable chat store: sqlite when sqlalchemy is
        installed (agno's SqliteDb requires it), JsonDb otherwise —
        opportunistic, like the data-stack grants."""
        try:
            from agno.db.sqlite import SqliteDb

            return SqliteDb(db_file=str(self._store / "chat.sqlite"))
        except ImportError:
            from agno.db.json import JsonDb

            return JsonDb(db_path=str(self._store / "chat"))

    def _build_agent(
        self, name: str, ws: Workspace, runtime: AppRuntime, model: str | None = None
    ) -> Any:
        from agno.agent import Agent

        from . import providers

        toolkit = WorkspaceTools(
            ws,
            apps=runtime,
            python_primer=DB_PRIMER,
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
            tools=[toolkit],
            compress_tool_results=compression is not None,
            compression_manager=compression,
            # studio-owned context: nontainer's tool descriptions cover
            # the MECHANICS (workspace, handlers, curl); this covers the
            # product the human is looking at (preview, artifacts,
            # checkpoints, publish)
            instructions=STUDIO_PRIMER,
            # Durable chat, keyed by the session name: after a server
            # restart the agent still remembers the conversation (and
            # the jsonl event log restores the visible transcript).
            db=self._chat_db(),
            session_id=name,
            add_history_to_context=True,
            markdown=True,
        )

    # -- delete: remove a session's whole universe ---------------------------

    def delete(self, session: Session) -> None:
        """Delete a session and everything it owns: the workspace
        branch, the app db, the transcript, the agent's chat record —
        and any published snapshots (they're views of this universe;
        their branches and tokens go too). Caller ensures not busy."""
        name = session.name
        doomed = {name}
        with self._lock:
            self._sessions.pop(name, None)
            manifest = self._manifest()
            for token, entry in list(manifest["published"].items()):
                if entry.get("session") == name:
                    snapshot = self._published.pop(token, None)
                    if snapshot is not None:
                        snapshot.close()
                    doomed.add(entry["branch"])
                    del manifest["published"][token]
            manifest["sessions"] = [s for s in manifest["sessions"] if s != name]
            manifest["models"].pop(name, None)
            self._save_manifest(manifest)
        self._wipe_chat(session)
        close_runtime = getattr(session.runtime, "close", None)
        if callable(close_runtime):  # reap dispatch workers
            close_runtime()
        session.ws.close()  # before branch deletion: it holds the branch
        session.db.close()
        self._delete_branches(doomed)
        (self._store / "dbs" / f"{name}.sqlite").unlink(missing_ok=True)
        (self._store / "events" / f"{name}.jsonl").unlink(missing_ok=True)

    @staticmethod
    def _wipe_chat(session: Session) -> None:
        """Drop the agno session record (best-effort: fakes and
        JsonDb quirks must never block a delete)."""
        db = getattr(session.agent, "db", None)
        if db is None:
            return
        try:
            db.delete_session(session_id=session.name)
        except Exception:
            pass

    def _delete_branches(self, names: set[str]) -> None:
        """Remove kvgit branches. kvgit can't delete the current
        branch, so deletions run from a hidden ``__void__`` anchor
        branch (created on first delete; never listed — the rail is
        manifest-driven). Orphaning instead would be worse: recreating
        a deleted name would resume the old branch, resurrecting
        'deleted' files."""
        path = self._store / "kvgit"
        if not path.is_dir():
            return  # non-kvgit or never-materialized store
        import kvgit

        def close(staged: Any) -> None:
            store = getattr(staged.versioned, "store", None)
            if callable(getattr(store, "close", None)):
                store.close()

        anchor = next(iter(names))
        probe = kvgit.store(kind="disk", path=str(path), branch=anchor)
        try:
            if "__void__" not in probe.list_branches():
                probe.create_branch("__void__")
        finally:
            close(probe)
        admin = kvgit.store(kind="disk", path=str(path), branch="__void__")
        try:
            branches = set(admin.list_branches())
            for branch in names & branches:
                admin.delete_branch(branch)
        finally:
            close(admin)

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
        user event's pre-turn head and truncate agent memory to the
        turns before it in the transcript. Position in the event log
        (not commit order) decides what survives — commit order can't
        tell a no-file-change turn from its predecessor (same head),
        the transcript can. The caller emits the `truncate` event and
        starts the new turn."""
        event = next((e for e in session.events if e.get("seq") == seq), None)
        head = event.get("head") if event else None
        if event is None or event.get("type") != "user" or not head:
            raise ValueError(f"event {seq} is not an editable user message")
        last_kept_run_id = None
        prior = [e for e in session.events if e["seq"] < seq]
        for _, ev in self._visible(prior):
            if ev.get("type") == "done":
                last_kept_run_id = ev.get("run_id") or last_kept_run_id
        session.ws.restore(head)
        self._truncate_chat(session, last_kept_run_id)

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

    @staticmethod
    def _truncate_chat(session: Session, last_kept_run_id: str | None) -> None:
        """Slice the agno session's runs after the last kept turn.
        agno has turn structure (runs ARE turns) but no rewind verb —
        get_session -> slice -> upsert_session composes one."""
        db = getattr(session.agent, "db", None)
        if db is None:  # e.g. test fakes without chat storage
            return
        from agno.db.base import SessionType

        record = db.get_session(session_id=session.name, session_type=SessionType.AGENT)
        if record is None or not record.runs:
            return
        if last_kept_run_id is None:
            record.runs = []
        else:
            index = next(
                (
                    i
                    for i, run in enumerate(record.runs)
                    if getattr(run, "run_id", None) == last_kept_run_id
                ),
                None,
            )
            if index is None:
                return  # unknown mapping: leave memory alone, never corrupt
            record.runs = record.runs[: index + 1]
        db.upsert_session(record)

    # -- publish: a VIEW of the same universe ------------------------------

    def publish(self, name: str) -> tuple[str, str | None]:
        """Freeze the current state behind a capability token. The
        snapshot branch never moves; its handlers share the session's
        LIVE db (fork inherits host_objects) — frozen code, live state,
        the idiomatic shape."""
        session = self._sessions.get(name)
        if session is None:
            raise KeyError(name)
        branch = f"{name}-pub-{secrets.token_hex(4)}"
        snapshot = session.ws.fork(branch)
        token = mint_token()
        with self._lock:
            self._published[token] = snapshot
            # The snapshot BRANCH is durable (it's a kvgit branch);
            # persist the token -> branch mapping too, or restarts
            # orphan real snapshots behind forgotten URLs.
            manifest = self._manifest()
            manifest["published"][token] = {
                "branch": branch,
                "session": name,
                "checkpoint": snapshot.head,
            }
            self._save_manifest(manifest)
        return token, snapshot.head

    def resolve(self, token: str) -> Workspace | None:
        """The ``build_router`` resolve hook (token -> frozen
        Workspace). Falls back to the manifest after a restart: the
        snapshot branch persisted, so reopen it lazily — with the
        parent session's live db as its host object (frozen code,
        live state, same as at publish time)."""
        snapshot = self._published.get(token)
        if snapshot is not None:
            return snapshot
        entry = self._manifest()["published"].get(token)
        if entry is None:
            return None
        with self._lock:
            snapshot = self._published.get(token)
            if snapshot is not None:
                return snapshot
            parent = self._sessions.get(entry["session"])
            db = (
                parent.db
                if parent is not None
                else Db(self._store / "dbs" / f"{entry['session']}.sqlite")
            )
            snapshot = workspace(
                entry["branch"], store=self._store, python=self._python_config(db)
            )
            self._published[token] = snapshot
            return snapshot

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
