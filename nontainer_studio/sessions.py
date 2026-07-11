"""Session registry: one Workspace + AppRuntime + agno Agent + SQLite
store + event log per session.

Ownership model, on display:

- WORKSPACE (files, cache, cwd): durable and versioned — a kvgit
  branch per session; restores and forks apply here.
- APP DB (``db`` host object): durable but HISTORYLESS — live app
  state that survives publish and never time-travels. Fresh per
  session, copied on fork (a new universe), shared with published
  snapshots (a view of the same universe), untouched by restore.
- CONVERSATION + event log: durable but session-scoped — agno persists
  chat (SqliteDb when sqlalchemy is installed, JsonDb otherwise) keyed
  by session_id, and the transcript event log appends to a jsonl per
  session. Restore is SYNCHRONIZED: the agent's memory (agno runs)
  rewinds with the workspace via the head+run_id stamps on each
  turn's done event, while the visible transcript keeps its record.
  Fork = fresh chat over branched files (conversation never forks).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nontainer import PythonConfig, Workspace, workspace
from nontainer.adapters.agno import WorkspaceTools
from nontainer.apps import AppRuntime, enable_apps, mint_token

DEFAULT_STORE = Path.home() / ".nontainer-studio"

MAX_EVENTS = 10_000  # per session; a demo bound, not a product knob

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

    def copy_to(self, path: str | Path) -> "Db":
        """Consistent point-in-time copy (sqlite backup API) — fork
        gives the new universe its own store."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(path))
        with self._lock:
            self._c.backup(dst)
        dst.close()
        return Db(path)

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
    reference isn't the only one (the classic create_task GC footgun) —
    and it's the handle a future cancel button needs."""

    log_path: Path | None = None
    """Durable transcript: emit appends each event as a jsonl line;
    open() reloads the tail. Replay-vs-live needs no special casing —
    the event feed already serves both from one cursor."""

    events: list[dict] = field(default_factory=list)
    """The transcript, server-side: user messages, streamed agent
    events, turn boundaries. Subscribers replay from a cursor and then
    follow live — this is what makes background sessions work."""

    new_event: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def emit(self, event: dict) -> None:
        async with self.new_event:
            # Control events bypass the cap: dropping a `done` would
            # leave clients busy forever and pollers hanging.
            if len(self.events) < MAX_EVENTS or event["type"] in ("done", "error"):
                self.events.append(event)
                if self.log_path is not None:
                    with self.log_path.open("a") as f:
                        f.write(json.dumps(event) + "\n")
                if len(self.events) == MAX_EVENTS:
                    self.events.append({"type": "error", "message": "event log full"})
            self.new_event.notify_all()

    async def follow(self, since: int):
        """Yield events from ``since``, then live. Runs forever; the
        subscriber disconnecting is the exit path."""
        cursor = max(0, since)
        while True:
            async with self.new_event:
                while cursor >= len(self.events):
                    await self.new_event.wait()
            while cursor < len(self.events):
                yield cursor, self.events[cursor]
                cursor += 1

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
        on first use)."""
        manifest = self._manifest()
        names = set(manifest["sessions"]) | set(self._sessions)
        return [
            {
                "name": name,
                "busy": (s := self._sessions.get(name)) is not None and s.busy,
                "model": (
                    s.model if s is not None else manifest["models"].get(name)
                ),
            }
            for name in sorted(names)
        ]

    def _manifest_path(self) -> Path:
        return self._store / "sessions.json"

    def _manifest(self) -> dict:
        """{"sessions": [...], "published": {token: {branch, session,
        checkpoint}}, "models": {name: spec}} — tolerant of the v1
        bare-list format."""
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
        }

    def _load_manifest(self) -> set[str]:
        return set(self._manifest()["sessions"])

    def _save_manifest(self, manifest: dict) -> None:
        self._manifest_path().parent.mkdir(parents=True, exist_ok=True)
        self._manifest_path().write_text(json.dumps(manifest, indent=1))

    def _record(self, name: str, model: str | None = None) -> None:
        """Add to the durable session manifest (caller holds _lock)."""
        manifest = self._manifest()
        manifest["sessions"] = sorted(set(manifest["sessions"]) | {name})
        if model is not None:
            manifest["models"][name] = model
        self._save_manifest(manifest)

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
            session = self._assemble(name, ws, db, model)
            session.events.extend(self._load_events(session.log_path))
            self._sessions[name] = session
            self._record(name, model)
            return session

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
        return PythonConfig(modules=modules, host_objects={"db": db})

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
        a crash are skipped, not fatal)."""
        if log_path is None or not log_path.exists():
            return []
        events = []
        for line in log_path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events[-MAX_EVENTS:]

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

        toolkit = WorkspaceTools(ws, apps=runtime, python_primer=DB_PRIMER)
        return Agent(
            model=self._model_factory(model),
            tools=[toolkit],
            # Durable chat, keyed by the session name: after a server
            # restart the agent still remembers the conversation (and
            # the jsonl event log restores the visible transcript).
            db=self._chat_db(),
            session_id=name,
            add_history_to_context=True,
            markdown=True,
        )

    # -- fork: a NEW universe --------------------------------------------

    def fork(self, name: str, new_name: str, checkpoint: str | None = None) -> Session:
        """Fork a session: the workspace branches (O(1)); the app db is
        COPIED so the child's experiments can't contaminate the parent.
        With ``checkpoint``, the child is then rewound to that commit —
        fork-from-here. The db copy is as-of-now either way: external
        state has no history to rewind to (that's the lesson).

        The child gets a fresh agent (empty chat) — conversation
        belongs to the harness, not the workspace."""
        parent = self._sessions.get(name)
        if parent is None:
            raise KeyError(name)
        with self._lock:
            if new_name in self._sessions:
                raise ValueError(f"session {new_name!r} already exists")
            # Branch the workspace, then REOPEN it with its own config:
            # ws.fork() inherits host_objects (the parent's live db),
            # which is right for publish but wrong for a new universe.
            parent.ws.fork(new_name).close()
            db = parent.db.copy_to(self._store / "dbs" / f"{new_name}.sqlite")
            ws = workspace(new_name, store=self._store, python=self._python_config(db))
            if checkpoint is not None:
                ws.restore(checkpoint)
            # deliberately NOT copied: chat + transcript (conversation
            # belongs to the harness; a fork is a fresh conversation
            # over the branched files)
            session = self._assemble(new_name, ws, db, parent.model)
            self._sessions[new_name] = session
            self._record(new_name, parent.model)
            return session

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
                # kvgit bug (as of 0.3.x): delete_branch leaves the
                # prev-HEAD recovery backup, and a same-name branch
                # opened later "recovers" the deleted state — files
                # rising from the grave. Remove the backup too.
                # TODO: fix upstream in kvgit, then drop this.
                try:
                    admin.versioned.store.remove(
                        f"__branch_head_prev__{branch}"
                    )
                except Exception:
                    pass
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

    # -- restore: synchronized time travel ---------------------------------

    def restore(self, session: Session, checkpoint: str) -> None:
        """Rewind the workspace AND the agent's memory to a checkpoint,
        together — no post-restore contradiction between what the agent
        remembers and what the files say, so nothing to "inform" it of.

        The mapping: each turn's `done` event records the workspace
        head at turn end plus the turn's agno run_id. Turns whose head
        is at-or-before the target commit are kept; the agno session's
        runs are truncated after the last kept turn. The visible
        transcript is NOT touched — memory follows the workspace, the
        record belongs to the human. (The app db doesn't rewind either;
        external state has no history.)"""
        history = [c.id for c in session.ws.history()]  # newest -> oldest
        if checkpoint not in history:
            raise ValueError(f"unknown checkpoint {checkpoint!r}")
        position = {commit: i for i, commit in enumerate(history)}
        target = position[checkpoint]

        last_kept_run_id = None
        for event in session.events:
            if event.get("type") != "done":
                continue
            head = event.get("head")
            if head in position and position[head] >= target:
                # at-or-before the target: this turn survives the rewind
                last_kept_run_id = event.get("run_id") or last_kept_run_id

        session.ws.restore(checkpoint)
        self._truncate_chat(session, last_kept_run_id)

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
                session.ws.close()
                session.db.close()
            self._sessions.clear()
            for snapshot in self._published.values():
                snapshot.close()
            self._published.clear()


def repair_aborted_run(session: Session, run_id: str | None, note: str) -> None:
    """agno's history builder SKIPS runs with status=error — so a
    transport hiccup (API credit, network) at the end of a long turn
    would erase the whole turn from the agent's memory while the human
    transcript still shows it. That divergence produces confident
    confabulation, not "I don't remember".

    The messages up to the failure are real work: append a closing
    note explaining the abnormal end, mark the run completed, and the
    agent keeps its memory AND knows the turn was cut short."""
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
        if run is None or run.status != RunStatus.error:
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
