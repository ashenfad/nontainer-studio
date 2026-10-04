"""One session as the server holds it: its workspace, agent, transcript
and queues, the app db it serves, and what its transcript says has been
delivered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nontainer import Workspace
from nontainer.apps import AppRuntime
from nontainer.errors import (
    JobRunning,
    SessionIdError,
    SessionsError,
)
from nontainer.inbox import Inbox, Note

from .config import _delegate_wakes
from .delegates import answer_message, answer_run

log = logging.getLogger(__name__)

MAX_EVENTS = 10_000  # in-MEMORY tail window, not a lifetime cap


class SweptSessionError(SessionIdError):
    """A name that names nothing openable: the delegates record still
    lists it, and its branch is gone.

    A ``SessionIdError`` because it is the same kind of answer — this
    name cannot become a session — and its own class because the
    reason differs and the answer a server gives differs with it: a
    malformed name is the caller's mistake, where this is a name that
    was valid and whose state has since been collected.
    """


class ReservedSessionError(SessionIdError):
    """A name a published app still names as its origin session.

    The session was deleted and its app row was kept on purpose: an app
    outlives the session that built it. The row names that session, and
    every later publish, the transcript marker and branching from a
    version read that name. A fresh session opened under it would
    publish over the dead session's URL and serve its database, so the
    name stays taken for as long as the app is published; unpublishing
    hands it back.
    """


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


class Db:
    """A tiny thread-safe SQLite store, injected as ``db`` (the
    webapp.py idiom). Frozen serving calls handlers concurrently, so
    the store owns its own locking."""

    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
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

    def query(self, sql: str, params: tuple = ()) -> list:
        """A read (SELECT); returns a list of row tuples."""
        with self._lock:
            return self._c.execute(sql, params).fetchall()

    def reset(self) -> None:
        """Drop every table, so the store reads as new. What a test
        calls first on ``testdb``, the in-memory store handed to
        ``call(..., db=testdb)``, so no test seeds rows into the live
        store every published version serves over and none inherits
        the last test's rows."""
        with self._lock:
            names = [
                row[0]
                for row in self._c.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for name in names:
                self._c.execute(f'DROP TABLE IF EXISTS "{name}"')
            self._c.commit()

    def close(self) -> None:
        with self._lock:
            self._c.close()


def _read_log(log_path: Path | None) -> list[dict]:
    """A transcript log, whole, compacted (torn last lines from a
    crash are skipped, not fatal). Legacy logs predate stored seqs and
    compaction: seqs are assigned by line position (which is what
    truncate events' `to` referenced back then), and the granular
    delta runs collapse on the way in."""
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
    return _compact(events)


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

    wsgit: bool = False
    """Whether the agent can type ``ws-git`` in this session's terminal:
    ``NONTAINER_STUDIO_WSGIT`` and then ``register_wsgit``'s own answer,
    kept rather than re-derived. The second is False where the executor
    can neither run an injected command nor ferry a ``ws-*`` verb into a
    guest. What teaches the verb reads this."""

    delegates: Any = None
    """This session's ``nontainer.sessions.Sessions`` — the job table
    over the delegates it forked, and the object the ``sessions`` tool
    was registered against. Built here rather than left to the adapter
    because the studio owns both ends of it: notification (a rail
    indicator, the answer injected next turn) reads these jobs, and
    closing the session has to join their workers."""

    loop: Any = None
    """The event loop the running turn is on.

    A tool runs on a worker thread, so anything discovered there — a
    message delivered from the inbox, say — reaches the transcript
    through this: ``emit`` is a coroutine on the loop that owns the
    event buffer. A session's turns do not all run on the same loop (a
    delegate's runner makes one of its own), so it is recorded per
    turn rather than once.
    """

    in_turn: bool = False
    """Whether the holder of ``turn_lock`` is an agent turn.

    The lock is also taken as a RESERVATION — a publish holds it so
    that nothing lands between the tag it writes and the marker it
    appends — and the difference matters to a message that arrives
    meanwhile: a turn will read it at its next tool result, where a
    reservation has no delivery to make, so a message arriving then is
    refused rather than left queued for nobody.
    """

    undone_delegates: set = field(default_factory=set)
    """Delegates an edit unsaid: asked after the message it rewound to,
    so the conversation that exists now never asked for them. Their
    answers are never delivered and they are listed nowhere; their
    branches stay, and age out like any other."""

    wakes_left: int = field(default_factory=_delegate_wakes)
    """Turns delegates' answers may still start before the human speaks
    again (see ``_delegate_wakes``)."""

    wake_ok: bool = True
    """False after a turn the human stopped or that errored, until they
    send a message: a stopped session must stay stopped, and an answer
    arriving must not take the stop back."""

    wake_cap_noted: bool = False
    """Whether the human has been told that answers are waiting because
    the wake budget is spent, so the notice is said once."""

    def human_spoke(self) -> None:
        """A message from the human: waking may start again, with a full
        budget."""
        self.wake_ok = True
        self.wakes_left = _delegate_wakes()
        self.wake_cap_noted = False

    inbox: Inbox = field(default_factory=Inbox)
    """Messages queued while a turn runs, delivered to the model with
    its next tool result.

    A run is opaque: between the moment it starts and the moment it
    ends there is no seam a human's sentence can reach the model
    through except the text a tool call comes back with. The queue is
    the studio's half of that — POST /chat on a busy session fills it
    instead of refusing — and it lives on the session rather than on
    the toolkit because the toolkit is rebuilt on every model switch
    while the queue must not be.
    """

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
            # ts: when it happened, in epoch seconds. The transcript
            # reads durations off it ("Thought for 12s"); a merged delta
            # run keeps its first chunk's.
            event = {**event, "seq": self.next_seq, "ts": round(time.time(), 3)}
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

    def answered_delegates(self) -> list:
        """Delegate jobs with an answer this session has not read yet.

        A cancelled job is never among them: cancelling means the answer
        is discarded when it arrives, so there is nothing to deliver and
        nothing to keep waiting for. Neither is an EXPIRED one: a
        delegate's branch is retained on an idle TTL, and a job whose
        branch was swept has had its answer dropped with it — asking for
        it raises. Both are the same rule, which is that this lists what
        a turn could actually deliver, so it is also what the rail's
        count means. Nor is one an edit unsaid: the conversation that
        exists now never asked for it.

        Read per ANSWER, not per delegate: a delegate given a second
        task (a resume) answers again under the same name, and the
        transcript showing its first answer says nothing about this
        one (see :meth:`_shows`)."""
        if self.delegates is None:
            return []
        done = [
            job
            for job in self.delegates.list()
            if job.status not in ("running", "cancelled", "expired")
            and job.name not in self.undone_delegates
        ]
        unread = self._unshown(done)
        return [job for job in done if job.name in unread]

    def _unshown(self, jobs: list) -> set:
        """Names of those of ``jobs`` whose current answer the
        transcript does not show — the tail, then the whole log when
        the tail is full and cannot answer (as :meth:`undelivered`)."""
        shown = _deliveries(self.events)
        missing = {job.name for job in jobs if not _shows(shown, job)}
        if missing and len(self.events) >= MAX_EVENTS and self.log_path is not None:
            shown = _deliveries(_read_log(self.log_path))
            missing = {
                job.name
                for job in jobs
                if job.name in missing and not _shows(shown, job)
            }
        return missing

    def delivered_delegates(self) -> set:
        """Job names whose answers the transcript still shows.

        Delivery is a fact of the TRANSCRIPT, not of memory. An edit
        rewinds the files, the agent's memory and the visible transcript
        together, so an answer whose `delegate` event went with them has
        not been delivered to the conversation that exists now, and the
        next turn has to carry it again — a flag set when it was first
        shown would say otherwise and lose it for good. Read through the
        truncate projection for the same reason every other reader of
        "what the transcript now says" does: the log is append-only, and
        a cut is an event rather than a deletion.
        """
        return self._delivered_in(self.events)

    @staticmethod
    def _delivered_in(events: list) -> set:
        return {
            event["name"]
            for _, event in _visible(events)
            if event.get("type") == "delegate" and event.get("name")
        }

    def undelivered(self, names: Iterable[str]) -> set:
        """Those of ``names`` whose answers the transcript does not
        show.

        Memory holds the transcript's tail, ``MAX_EVENTS`` long, and a
        restart reloads only that much; a delivery older than the
        window is on disk and not in the list. So a name the tail does
        not show is checked against the whole log before it counts as
        undelivered, and only then: the log is read when the tail is
        full and a name is missing from it, which is the one case the
        tail cannot answer. A rewind still unsays a delivery either
        way, since the log is read through the same truncate projection.
        """
        missing = set(names) - self.delivered_delegates()
        if missing and len(self.events) >= MAX_EVENTS and self.log_path is not None:
            missing -= self._delivered_in(_read_log(self.log_path))
        return missing

    def take_delegate_answers(self) -> list:
        """Those answers, as ``(job name, Answer)``.

        Nothing is marked here: the caller emits a `delegate` event per
        answer, and that event IS the record of delivery. So a turn that
        dies between collecting and emitting delivers again next turn,
        rather than dropping an answer nobody ever read."""
        out = []
        for job in self.answered_delegates():
            try:
                answer = self.delegates.result(job.name)
            except (JobRunning, SessionsError):
                # Raced the landing, or the job was cancelled or its
                # branch swept between the listing and here (an expired
                # job raises `BranchExpired`, which is a `SessionsError`).
                # Skip it: an unfinished job belongs on a later turn, and
                # a cancelled or expired one is never asked for again —
                # neither is listed as deliverable once its status says
                # so.
                continue
            out.append((job.name, answer))
        return out


_EMIT_TIMEOUT = 10.0
"""Seconds a delivery waits for its transcript event to land. A bound
rather than a promise: the wait is on a tool's worker thread, and a
loop that has stopped answering must not hold the tool result the
model is waiting for."""


def _delivery_event(note: Note) -> dict:
    """The transcript event for a note the model has just read.

    A delegate's answer taken mid-turn is the SAME fact as one
    collected between turns, so it is the same `delegate` event: the
    delivery record, `Session.undelivered` and the rail's waiting count
    all read that event and would each miss an answer written down any
    other way. Everything else is the person this session works for,
    speaking mid-turn — an `interject`, which carries no `head` because
    there is no pre-turn commit to rewind to: the turn it landed in
    began before it was said, so an edit cannot start from here.
    """
    if note.kind == "mechanism" and note.job:
        answer = note.answer
        if answer is None:
            return {
                "type": "delegate",
                "name": note.job,
                "status": "answered",
                "text": note.text,
            }
        return {
            "type": "delegate",
            "name": note.job,
            "status": answer.status,
            "text": answer_message(note.job, answer),
            **answer_run(answer),
        }
    return {"type": "interject", "id": note.id, "text": note.text}


def _record_delivery(session: "Session") -> Callable:
    """The session's ``Inbox.on_delivered``: write down what the model
    was just handed.

    Called on the worker thread the tool ran on, since that is where
    the delivery happens, so each event is handed to the loop the turn
    is on and waited for: the transcript then says the note arrived
    BEFORE the tool result it rode out with, which is the order it
    happened in. A failure is logged and nothing more — the notes are
    already in the result the model is about to read, and an exception
    escaping here would replace that result with an error.
    """

    def record(notes: "list[Note]") -> None:
        loop = session.loop
        for note in notes:
            try:
                if loop is None:
                    raise RuntimeError("no event loop is carrying this turn")
                future = asyncio.run_coroutine_threadsafe(
                    session.emit(_delivery_event(note)), loop
                )
                future.result(timeout=_EMIT_TIMEOUT)
            except Exception:  # noqa: BLE001 - the tool result wins
                log.warning(
                    "inbox: note %s reached the model but not the transcript",
                    note.id,
                    exc_info=True,
                )

    return record


def record_fold(session: "Session", fold: Any) -> None:
    """Put a compaction marker in the transcript.

    Called by nontainer's compaction on the worker thread the summary
    was written on, mid-turn, so the event is handed to the loop the
    turn is on and waited for, as a delivery's is. A failure is logged
    and nothing more: the fold is recorded either way, and the marker
    is only the person's view of it.
    """
    loop = session.loop
    try:
        if loop is None:
            raise RuntimeError("no event loop is carrying this turn")
        event = {
            "type": "compaction",
            "turns": fold.runs,
            "summary": fold.summary,
            "tokens_before": fold.tokens_before,
            "tokens_after": fold.tokens_after,
        }
        asyncio.run_coroutine_threadsafe(session.emit(event), loop).result(
            timeout=_EMIT_TIMEOUT
        )
    except Exception:  # noqa: BLE001 - the fold stands without its marker
        log.warning(
            "compaction: a fold reached the model but not the transcript", exc_info=True
        )


def _load_events(log_path: Path | None) -> list[dict]:
    """Reload a prior run's transcript tail."""
    return _read_log(log_path)[-MAX_EVENTS:]


def _deliveries(events: list) -> dict[str, list[tuple[Any, Any]]]:
    """``name -> [(started, ts), ...]`` for the `delegate` events the
    transcript shows: which answer each delivered, by the ``started``
    of the run that wrote it, and when it was written."""
    out: dict[str, list[tuple[Any, Any]]] = {}
    for _, event in _visible(events):
        if event.get("type") == "delegate" and event.get("name"):
            out.setdefault(event["name"], []).append(
                (event.get("started"), event.get("ts"))
            )
    return out


def _shows(shown: dict, job: Any) -> bool:
    """Whether ``shown`` (see :func:`_deliveries`) holds ``job``'s
    current answer.

    An answer is named by its delegate and the start of the run that
    wrote it. A delegate resumed with a second task keeps its name and
    starts a new run, so its first answer's event does not cover the
    second, which would otherwise never wake the session or reach it.
    An event written before events carried ``started`` covers whatever
    answer had landed by the time it was written."""
    for started, ts in shown.get(job.name, ()):
        if started is not None:
            if started == job.started:
                return True
        elif ts is None or job.finished is None or ts >= job.finished:
            return True
    return False


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
