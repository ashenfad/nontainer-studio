"""How a turn runs: the driver's turn events turned into transcript
events, the chain of turns a message and its queue lead to, resuming a
run that hit a provider error, keeping a cut run in the agent's memory,
and waking a session whose delegates have answered.

The loop itself is the session's driver (``drivers.py``); nothing here
knows which loop it is.

Decoupled from any request: a turn is a server-side task, and the
routes in ``server.py`` only start one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from nontainer.adapters.render import artifact_kind, parse_artifacts_note
from nontainer.turns import (
    Compacted,
    Delivered,
    DeliveredNote,
    RunEnded,
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolEnded,
    ToolStarted,
    TurnEvent,
    Usage,
)

from . import delegates
from .config import _delegate_wakes

log = logging.getLogger(__name__)

STOPPED_AT_SHUTDOWN = "the studio shut down while this turn was running"
"""Why a turn ended when nothing in it went wrong.

The human sees the turn stop mid-sentence and needs the reason to be
the studio rather than the model, on a delegate's transcript as on any
other session's.
"""


RESUME_BACKOFFS: tuple[float, ...] = (5.0, 30.0, 60.0)
"""Seconds a turn waits before each resume of a run that ended in a
provider error, one entry per resume it gets.

The model call has already retried the failure with its own backoff by
the time the run gives up, so what is left is an outage measured in
seconds to minutes rather than a blip. One resume after five seconds
caught some and missed others: a provider that stalled for two minutes
stalled again on a resume five seconds later, and the turn ended with
the human asked to type "go on" to a provider that was fine again
by then. The waits grow so a later resume lands after the outage
rather than inside it. A stop pressed during any wait is honored and
the run is not resumed.
"""


def _gave_up(failures: int, last: str) -> str:
    """What a turn that ran out of resumes says, to the human (the
    error event) and to the model (the kept run's closing note).

    A bare provider string ("The operation was aborted") names no
    cause, and agents reading it blamed whatever was nearest, the
    delegates that had just answered. This one says it was the
    provider, that the work is kept, and what continues it."""
    times = "once" if failures == 1 else f"{failures} times in a row"
    return (
        f"the model provider failed {times} (last: {last}). Everything up "
        "to here is kept; send a message to continue."
    )


def _short(value: Any, limit: int = 2_000) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def _short_middle(value: Any, limit: int = 2_000) -> str:
    """Cap by cutting the MIDDLE — for tracebacks, where the last line
    (the exception) is the one that matters."""
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return text[:head] + "\n…[truncated]…\n" + text[-tail:]


def _tool_args(args: Any) -> Any:
    """Structured args when possible (the client renders tool calls
    per-type: highlighted code, file diffs, terminal commands), a
    capped string otherwise. Values are capped generously — file
    contents ARE the rendering."""
    if isinstance(args, dict):
        shaped = {
            k: _short(v, 16_000) if isinstance(v, str) else v for k, v in args.items()
        }
        try:
            json.dumps(shaped)
            return shaped
        except (TypeError, ValueError):
            pass
    return _short(args if args is not None else "")


def _delivery_event(note: DeliveredNote) -> dict:
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
        return delegates.answer_event(note.job, answer)
    return {"type": "interject", "id": note.id, "text": note.text}


def _client_events(ev: TurnEvent) -> list[dict]:
    """The transcript events for one turn event. ``RunStarted`` and
    ``RunEnded`` are the turn's own business (see ``_follow_run``)."""
    if isinstance(ev, TextDelta):
        return [{"type": "text", "delta": ev.text}]
    if isinstance(ev, ThinkingDelta):
        return [{"type": "thinking", "delta": ev.text}]
    if isinstance(ev, ToolStarted):
        return [
            {
                "type": "tool_start",
                "name": ev.name,
                "call_id": ev.call_id or None,
                "args": _tool_args(ev.args),
            }
        ]
    if isinstance(ev, ToolEnded):
        events: list[dict] = [
            {
                "type": "tool_end",
                "name": ev.name,
                "call_id": ev.call_id or None,
                "is_error": ev.is_error,
                "result": _short(ev.result),
            }
        ]
        # First-class artifact events: parse the RAW (uncapped) result —
        # the `[ui artifacts: ...]` note rides at the string's tail, so a
        # long tool result would truncate it away if we parsed _short().
        # The note stays inside tool_end.result (the model-facing
        # affordance); these events are additive, and the client dedupes
        # by path against its legacy note-regex fallback.
        for name, path in parse_artifacts_note(ev.result):
            events.append(
                {
                    "type": "artifact",
                    "name": name,
                    "path": path,
                    "kind": artifact_kind(path),
                }
            )
        return events
    if isinstance(ev, Delivered):
        # A message queued mid-turn rides out on the tool result that
        # delivered it, and the transcript shows it as its own event,
        # in the slot it arrived in, before that result.
        return [_delivery_event(note) for note in ev.notes]
    if isinstance(ev, Compacted):
        return [
            {
                "type": "compaction",
                "turns": ev.runs,
                "summary": ev.summary,
                "tokens_before": ev.tokens_before,
                "tokens_after": ev.tokens_after,
            }
        ]
    if isinstance(ev, Usage):
        # context-usage telemetry for the UI (one per model call; the
        # frontend keeps only the latest)
        return [
            {
                "type": "usage",
                "input_tokens": ev.input_tokens,
                "cached_tokens": ev.cached_tokens,
            }
        ]
    return []


def _settle_inbox(session: Any) -> None:
    """Close the delivery of notes this turn already handed the model.

    A loop may settle nothing on a cancelled or errored run (agno runs
    no post hook for one) — and ``_keep_aborted_run``
    keeps the run's messages in the agent's memory, which means the
    model DID read whatever rode out with them. Left unsettled, the
    next turn's pre hook would put those notes back in the queue and
    say them all over again.
    """
    session.inbox.settle()


def _keep_aborted_run(session: Any, run_id: str | None, note: str) -> None:
    """Keep a run that errored or was cancelled in the agent's memory,
    closed with ``note`` as the reason it ended early.

    A loop's history can leave such a run out (agno's does), so without
    this the model forgets a turn whose files are still in the
    workspace. The work up to the cut is real: the driver keeps the run,
    closed with a note saying the turn was cut short.

    Best-effort. It runs on every way a turn can fail, including the
    ones where the turn handler is already unwinding, so a failure here
    is logged and swallowed: losing the note costs the model some
    memory, while raising would cost the transcript its `done`.
    """
    try:
        session.driver.keep(run_id, note)
    except Exception:
        log.warning(
            "could not keep aborted run %s of session %s",
            run_id,
            session.name,
            exc_info=True,
        )


def _warm(session: Any) -> None:
    """Start the session's worker, or boot its guest on a dud rung,
    while the model reads the prompt.

    nontainer starts either on the first execution, so without this
    the first tool call of a session pays for it: about a second and a
    half on dud-vm, a few milliseconds for a process worker. A turn
    starts it on a worker thread instead, where the model call hides
    it, and a session opened only to be read never starts one.

    Best-effort, so a failure is only logged. A failed start leaves
    nothing behind, and the first execution tries again and reports
    whatever it meets.

    The turn does not wait for it. A turn with no tool call can end
    mid-boot, and the session can then be deleted or the server shut
    down while the warm still runs. That is safe: the executor holds
    one lock across the whole start and across its close, and the
    workspace closes its executor before its store. So a close waits
    for a start in flight and then parks what it started, and a warm
    that arrives after the close does nothing. Waiting here instead
    would keep the session busy until a cold boot finished.
    """
    try:
        session.ws.runtime.warm()
    except Exception:
        log.debug("could not warm session %s", session.name, exc_info=True)


class _RunState:
    """What one turn's stream has revealed so far: the run id, and
    whether the run was cancelled or ended in a provider error.

    Held outside the loop that fills it, so an exception out of the
    loop leaves behind the run id the abort path needs."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.cancelled = False
        self.errored: str | None = None


class _RunFailed(Exception):
    """A run that ended ``failed``: anything but a provider error the
    turn may resume from. Raised so the turn ends the way it does on
    any exception out of the run."""


async def _follow_run(
    session: Any, stream: AsyncIterator[TurnEvent], state: _RunState
) -> None:
    """Stream one run's turn events into the session's transcript.

    Records the run id on the session as soon as the stream reveals it
    — the handle the stop button cancels by. An interrupted run (a
    provider error) is recorded in ``state`` and not emitted: whether
    it becomes the turn's `error` is the turn's decision, made once it
    knows whether the run resumed. A failed one raises
    :class:`_RunFailed`.
    """
    async for ev in stream:
        if isinstance(ev, RunStarted):
            state.run_id = ev.run_id
            session.run_id = ev.run_id
        elif isinstance(ev, RunEnded):
            if ev.status == "cancelled":
                state.cancelled = True
                await session.emit({"type": "notice", "text": "turn stopped"})
            elif ev.status == "interrupted":
                state.errored = ev.message or "provider error"
            elif ev.status == "failed":
                raise _RunFailed(ev.message or "the run failed")
        else:
            for payload in _client_events(ev):
                await session.emit(payload)


async def _stopped_while_waiting(
    session: Any, run_id: str | None, seconds: float
) -> bool:
    """Wait ``seconds``; True as soon as a stop reaches ``run_id``.

    The stop button cancels by run id through the driver, which records
    the intent even for a run that is not running at the moment.
    Reading that record is how a stop pressed between a failure and its
    resume is seen at all."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        if run_id is not None and await session.driver.is_cancelled(run_id):
            return True
        left = deadline - loop.time()
        if left <= 0:
            return False
        await asyncio.sleep(min(0.1, left))


def _snapshot_delegates(session: Any, registry: Any) -> None:
    """Write down what this session's delegate job table now says —
    retention outlives the table (see ``Registry.snapshot_delegates``).
    A caller without a registry has nothing to write it to."""
    if registry is not None:
        registry.snapshot_delegates(session.name)


async def _run_turn(session: Any, message: str | None, registry: Any = None) -> None:
    """The human's message, and every turn it leads to, as one
    server-side task DECOUPLED from any HTTP request: events land in
    the session's buffer, subscribers follow from a cursor.
    Disconnects, reloads, and session switches never abort work.
    Caller holds the turn lock; released here.

    Usually that is one turn. A message queued while this one ran rides
    out with the agent's next tool result — but a run can end without
    making another tool call, and then the queue is still full with
    nobody to hand it to. So the lock is held across the chain: what is
    still queued when a turn finishes normally starts the next turn, as
    an ordinary message of the human's, until the queue is empty.

    Delegates' answers ride the same chain. One that landed after the
    turn's last tool call has no tool result to ride out on, so once
    the queue is empty, waiting answers start a woken turn when
    ``Registry.may_wake`` allows it. ``message`` None starts the chain
    with one: that is how an idle session is woken (see
    ``_wake_on_answers``).

    ``registry`` is passed so the turn can write down what its
    delegates' job table now says, and so it can name the delegates a
    restart left without answers. Without one the turn runs exactly as
    before and does neither — every caller that has a registry passes
    it."""
    # What the lock is held FOR, which is what decides whether a
    # message arriving now is queued or refused.
    session.in_turn = True
    asyncio.get_running_loop().run_in_executor(None, _warm, session)
    from_queue: list[str] = []
    try:
        while True:
            ran = await _one_turn(session, message, registry, from_queue)
            if ran is False:
                # Stopped or errored. Whatever is queued stays queued,
                # for the human's next explicit send: a turn they
                # stopped must stay stopped, and starting another one
                # with their words in it would take the stop back. So
                # does waking, for the same reason.
                session.wake_ok = False
                break
            queued = session.inbox.drain()
            if not queued:
                if ran is None or registry is None or not registry.may_wake(session):
                    break
                message, from_queue = None, []
                continue
            # A drained note is delivered-but-unsettled, which the next
            # turn's pre hook would put back in the queue. These are
            # not going to a tool result — they ARE the next turn's
            # message — so the delivery is closed here.
            session.inbox.settle()
            message = "\n\n".join(note.text for note in queued)
            from_queue = [note.id for note in queued]
    finally:
        session.in_turn = False
        session.turn_lock.release()
        # After the lock: the agent may have asked for a delegate or
        # kept one during the turn, and both live only in a job table
        # until this writes them down.
        await asyncio.to_thread(_snapshot_delegates, session, registry)
        # An answer that landed after the chain's last look but before
        # the lock came free was passed over: the session was busy when
        # it arrived. This is the look after, so none waits for the
        # human for want of an idle moment.
        if registry is not None:
            await _maybe_wake(session, registry)
        # And after that, the session's own name, read off the
        # transcript this turn just extended. It is a second model run,
        # so it happens where it can cost nothing: the turn is over,
        # the lock is released, and the next turn may start on top of
        # it — a failure logs and leaves the name the session had.
        if registry is not None:
            await _name_the_session(session, registry)


#: How soon to look again at a session whose answer arrived while a
#: reservation held it, in seconds.
RESERVED_RETRY = 0.5


async def _wake_on_answers(registry: Any) -> None:
    """Start a turn on an idle session as soon as one of its delegates
    answers.

    nontainer calls ``on_answer`` on the delegate's worker thread; the
    registry passes the parent's name to the hook installed here, which
    hands it to this loop. A session in a turn is passed over: it reads
    the answer with its next tool result, or on the chain ``_run_turn``
    runs when the turn ends, which looks once more after it lets go of
    the session. One held by a reservation instead is looked at again
    until it is free, since a reservation looks for nothing when it
    lets go. A failure is logged and the next answer is still
    heard. The hook comes out when this stops.
    """
    loop = asyncio.get_running_loop()
    landed: asyncio.Queue[str] = asyncio.Queue()
    registry.set_wake_hook(
        lambda name: loop.call_soon_threadsafe(landed.put_nowait, name)
    )
    try:
        while True:
            name = await landed.get()
            session = registry.get(name)
            if session is None:
                continue  # released since: its answers wait for it
            try:
                woke = await _maybe_wake(session, registry)
            except Exception:
                log.warning("waking %s failed", name, exc_info=True)
                continue
            if not woke and session.turn_lock.locked() and not session.in_turn:
                # Held by a reservation (a publish, a restore, a fork),
                # not a turn. A turn looks for answers when it lets go;
                # a reservation does not, so look again shortly.
                loop.call_later(RESERVED_RETRY, landed.put_nowait, name)
    finally:
        registry.set_wake_hook(None)


async def _maybe_wake(session: Any, registry: Any) -> bool:
    """Start a woken turn on ``session`` if it is idle and may be woken;
    True when one started."""
    if session.in_turn or session.turn_lock.locked():
        return False
    if not registry.may_wake(session):
        await _note_spent_wakes(session, registry)
        return False
    if not session.turn_lock.acquire(blocking=False):
        return False
    session.in_turn = True
    session.turn_task = asyncio.create_task(_run_turn(session, None, registry))
    return True


async def _note_spent_wakes(session: Any, registry: Any) -> None:
    """Tell the human, once, that answers are waiting for them because
    the session has been woken as often as it may be without them. Not
    when waking is off: then waiting for the human is the setting, not a
    budget running out."""
    if (
        _delegate_wakes() == 0
        or session.wake_cap_noted
        or not session.wake_ok
        or session.wakes_left > 0
        or session.delegates is None
        or not session.answered_delegates()
        or registry.is_delegate(session.name)
    ):
        return
    session.wake_cap_noted = True
    await session.emit(
        {
            "type": "notice",
            "text": (
                "Delegates have answered, but their answers have already "
                "started as many turns as they may since your last message. "
                "The agent will read them when you next write."
            ),
        }
    )


async def _one_turn(
    session: Any,
    message: str | None,
    registry: Any = None,
    from_queue: "list[str] | tuple[str, ...]" = (),
) -> bool | None:
    """One agent turn; True when it finished normally.

    False means the run was cancelled or errored — the two endings
    after which nothing may start another turn on the human's behalf.

    ``message`` None is a turn delegates' answers started rather than
    the human: it opens with a `wake` event instead of a `user` one, so
    it is no edit anchor and no message of theirs, and the model is sent
    the answers and ``WAKE_MESSAGE``. None comes back when there turned
    out to be no answer left to deliver, and no turn ran.

    ``from_queue`` names the queued messages this turn was started
    with, when it was: the shell has them on screen as waiting, and
    this is what tells it they are waiting no longer.
    """

    def snapshot() -> None:
        _snapshot_delegates(session, registry)

    woken = message is None
    if woken:
        if not session.answered_delegates():
            # Cancelled or swept since the wake was decided: no turn,
            # and nothing spent.
            return None
        session.wakes_left -= 1
    state = _RunState()
    try:
        # head here = the workspace BEFORE this turn: the user event's
        # stamp is the undo anchor (check it out = unwind this turn)
        if woken:
            opening = {"type": "wake", "head": session.ws.head}
        else:
            opening = {"type": "user", "text": message, "head": session.ws.head}
        if from_queue:
            opening["from_queue"] = list(from_queue)
        await session.emit(opening)
        # Delegates answer between turns, and nontainer holds the answer
        # until something collects it. This turn is that something: the
        # answers go into the transcript where the human can read them,
        # and ahead of the human's message in what the model is sent,
        # because they arrived first and the message is the instruction.
        # Marked as mechanism, never as the human asking (answer_message).
        answers = session.take_delegate_answers()
        # And the ones a restart parted from their answers. Both are
        # read before either is emitted: each is filtered on what the
        # transcript already shows, and an emitted event is part of
        # that.
        notes = [
            (name, delegates.orphan_message(name, versioning=session.wsgit))
            for name in (
                registry.orphaned_delegates(session) if registry is not None else []
            )
        ]
        for name, answer in answers:
            await session.emit(delegates.answer_event(name, answer))
        for name, note in notes:
            # The same event, because it is the same fact in the same
            # slot: what became of a delegate this session asked for.
            # Its status is what is true of it — asked, and never
            # answered here.
            await session.emit(
                {
                    "type": "delegate",
                    "name": name,
                    "status": "unanswered",
                    "text": note,
                }
            )
        if answers:
            # Reading an answer is dealing with the delegate, and
            # nontainer moved its `touched` when this collected it.
            # Record that before the turn runs: a long turn must not be
            # what decides whether a delivered answer counts as recent.
            await asyncio.to_thread(snapshot)
        prompt = "\n\n".join(
            [delegates.answer_message(n, a) for n, a in answers]
            + [note for _, note in notes]
            + [delegates.WAKE_MESSAGE if woken else message]
        )
        await _follow_run(session, session.driver.run(prompt), state)
        failures = 0
        while state.errored is not None and not state.cancelled:
            failures += 1
            if failures > len(RESUME_BACKOFFS):
                break
            await _resume(session, state, failures)
        if state.cancelled:
            # a loop's history may leave a cancelled run out; keeping
            # it keeps the partial work in the agent's memory
            await asyncio.to_thread(
                _keep_aborted_run, session, state.run_id, "stopped by the user"
            )
            _settle_inbox(session)
        elif state.errored is not None:
            # Every resume failed too. The same skip-on-replay problem
            # for an errored run: without keeping it, "please continue"
            # replans from scratch while the workspace holds the work.
            said = _gave_up(failures, _short_middle(str(state.errored), 300))
            await session.emit({"type": "error", "message": _short_middle(said)})
            await asyncio.to_thread(_keep_aborted_run, session, state.run_id, said)
            _settle_inbox(session)
    except asyncio.CancelledError:
        # Cut from outside the loop, which is the studio shutting down
        # on a turn it cannot wait out. The `error` event is what the
        # human reads and what a delegate's runner reads its status
        # off, and keeping the run is what keeps the work the turn
        # really did in the agent's memory. Both run inline: the loop
        # this turn is on is closing under it, so there is no thread to
        # hand them to.
        await session.emit({"type": "error", "message": STOPPED_AT_SHUTDOWN})
        _keep_aborted_run(session, state.run_id, STOPPED_AT_SHUTDOWN)
        _settle_inbox(session)
        raise
    except Exception as e:
        # A failed run, or an exception out of the run loop — from the
        # first run or from its resume — ends the turn here. A loop's
        # history may skip an errored run (agno's does), so the run is
        # kept to hold the turn's real work in the agent's memory.
        await session.emit({"type": "error", "message": _short_middle(str(e))})
        await asyncio.to_thread(
            _keep_aborted_run, session, state.run_id, _short_middle(str(e), 300)
        )
        _settle_inbox(session)
        state.errored = state.errored or str(e)
    finally:
        # done BEFORE the lock releases: the buffer is the permanent
        # source of truth (replays reconstruct it forever), so a next
        # turn's `user` event must never precede this turn's `done`.
        # It carries the turn's run id and the workspace head at
        # turn end — the commit <-> conversation mapping that lets a
        # rewind put the agent's memory back in sync with the files.
        session.run_id = None
        await session.emit(
            {"type": "done", "run_id": state.run_id, "head": session.ws.head}
        )
    return not state.cancelled and state.errored is None


async def _resume(session: Any, state: _RunState, attempt: int = 1) -> None:
    """Resume a run that ended in a provider error, in place: one of the
    resumes ``RESUME_BACKOFFS`` allows, ``attempt`` counting from 1.

    A provider failure is an interruption, not a restart. By the time a
    run reports one, the model call has already retried it, so the run
    stopped mid-turn with its tool calls done and their files written.
    Restarting from the user message would forget that work while the
    files stay; resuming the SAME run keeps every message it has, and
    the model picks up after its last tool result. The driver continues
    the run in place under its own run id (agno would fork a completed
    run instead, which is why the run is kept only after this).

    A bounded number of times, with a growing wait (``RESUME_BACKOFFS``).
    When the last resume fails too, the turn ends: the run is kept as an
    interrupted turn, and the human decides what happens next. A stop
    pressed during a wait is honored — the turn ends stopped, not
    resumed.

    Only an interrupted run leads here. A failed one is not a provider
    hiccup the next call can clear — it is the studio, a tool hook or
    the loop itself failing — and resuming into it would repeat it.

    On return ``state`` says how the turn ended: ``cancelled`` for a
    stop, ``errored`` holding the resume's own error when it failed
    the same way, and neither when the resumed run completed. An
    resume that fails outright (the loop refusing it, say) raises to the
    turn's own handler.
    """
    wait = RESUME_BACKOFFS[attempt - 1]
    text = "provider error — resuming the turn where it stopped"
    if attempt > 1:
        text = (
            f"provider error again — resuming in {wait:g}s "
            f"(try {attempt} of {len(RESUME_BACKOFFS)})"
        )
    await session.emit({"type": "notice", "text": text})
    if await _stopped_while_waiting(session, state.run_id, wait):
        state.cancelled = True
        await session.emit({"type": "notice", "text": "turn stopped"})
        return
    # The part of the run that ran is kept by the resume, notes and
    # all: the model read whatever rode out on its tool results, so
    # their delivery is closed here rather than at the end of a run
    # that may not finish.
    _settle_inbox(session)
    state.errored = None
    assert state.run_id is not None  # an interrupted run has started
    await _follow_run(session, session.driver.resume(state.run_id), state)


async def _name_the_session(session: Any, registry: Any) -> None:
    """Generate the session's title when the cadence says one is due,
    and mark the moment in the transcript.

    The `title` event is not the write path — the manifest is — it is
    the temporal record, and it buys two things the manifest cannot. It
    marks WHEN the session got that name, so an edit's rewind can put
    the title back the way the conversation was; and it lets the shell
    relabel now instead of on the next rail poll. `title` is the label
    now in force, which under a human title is theirs — the row the
    human is looking at does not move — while `agent` is the generated
    name stored beneath it and `at_seq`/`turns` are the transcript
    cursor it was read from. A rewind puts those three back together:
    the name without its cursor would leave the cadence counting from
    a transcript that no longer exists.
    """
    try:
        stored = await asyncio.to_thread(registry.retitle, session)
    except Exception as e:  # noqa: BLE001 - a name is never worth a turn
        log.info("titles: %s went unnamed (%s)", session.name, e)
        return
    if stored:
        shown = await asyncio.to_thread(registry.title_of, session.name)
        await session.emit({"type": "title", "title": shown, **stored})
