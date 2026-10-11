"""The seam a session's turns drive a loop through.

A :class:`TurnDriver` runs the agent's loop and streams what happens as
nontainer's turn events (``nontainer.turns``): the run starting, deltas,
tool calls and their results, notes delivered on a result, folds, usage,
and how the run ended. ``turns.py`` reads that stream and nothing of the
loop behind it, so the studio's policy (the queue, waking, resuming a
provider error, the transcript's events) is one thing whatever loop a
session runs.

A session's loop is built from a :class:`DriverSpec`, whichever loop
it is: :class:`AgnoDriver` is agno's, :class:`AgexDriver` agex's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from agno.run.cancel import ais_cancelled
from nontainer.adapters.agno import keep_aborted_run
from nontainer.inbox import Inbox, Note, split
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

log = logging.getLogger(__name__)

__all__ = ["AgexDriver", "AgnoDriver", "DriverSpec", "TurnDriver"]


@dataclass
class DriverSpec:
    """What a session's loop is built from, whichever loop it is."""

    session: str
    ws: Any
    runtime: Any
    """The session's app runtime, which ``test_app`` drives."""
    inbox: Inbox
    delegates: Any
    """The session's ``Sessions`` helper, whose answers the loop
    delivers on a tool result; ``None`` where it has none."""
    model: str
    """The model spec (``provider:model``)."""
    instructions: str
    vision: bool
    """Whether the model takes images: ``view_image``, and screenshots
    seen rather than named."""
    python_primer: str
    sessions_tool: Callable[..., str] | None
    """The studio's ``sessions`` tool, where the agent may delegate."""
    compaction: Any
    """A ``nontainer.compaction.Policy``, or ``None`` for never."""
    tool_calls: int | None
    """The tool calls a turn may spend, or ``None`` for no cap."""


class TurnDriver(Protocol):
    """One session's loop, as the studio's turns drive it.

    ``run`` and ``resume`` stream one run's events: ``RunStarted`` first
    (its id is what ``cancel`` and ``resume`` take) and ``RunEnded``
    last. A run that raises ends ``failed`` rather than raising; only
    cancelling the task that reads the stream stops it otherwise.
    """

    def run(self, prompt: str) -> AsyncIterator[TurnEvent]:
        """A new run, opening with ``prompt``."""
        ...

    def resume(self, run_id: str) -> AsyncIterator[TurnEvent]:
        """Run ``run_id`` on from where it was interrupted, in place."""
        ...

    async def cancel(self, run_id: str) -> None:
        """Stop ``run_id`` at its next chance. Idempotent, and a run not
        running now records the intent (:meth:`is_cancelled`)."""
        ...

    async def is_cancelled(self, run_id: str) -> bool:
        """Whether a stop has reached ``run_id``."""
        ...

    def keep(self, run_id: str | None, note: str) -> None:
        """Keep a run the studio ended early in the agent's memory,
        closed with ``note`` saying why."""
        ...


class AgnoDriver:
    """An agno ``Agent`` as a :class:`TurnDriver`.

    Two things reach the stream from outside agno's own events: notes
    the inbox delivered on a tool result (``inbox.on_delivered``, on the
    tool's thread) and folds compaction made (:meth:`folded`, on the
    summary's thread). Each is handed to the loop the run is streamed
    on, so it lands in order: a delivery before the result it rode on,
    a fold before the model call it is for.
    """

    def __init__(self, agent: Any, inbox: Inbox, session: str) -> None:
        self.agent = agent
        self.session = session
        self._side: Callable[[TurnEvent], None] | None = None
        inbox.on_delivered = self._delivered

    # -- the driver ---------------------------------------------------------

    def run(self, prompt: str) -> AsyncIterator[TurnEvent]:
        return self._drive(
            lambda: self.agent.arun(prompt, stream=True, stream_events=True)
        )

    def resume(self, run_id: str) -> AsyncIterator[TurnEvent]:
        # agno 3 continues an errored run in place, under its own id
        return self._drive(
            lambda: self.agent.acontinue_run(
                run_id=run_id,
                session_id=self.session,
                stream=True,
                stream_events=True,
            )
        )

    async def cancel(self, run_id: str) -> None:
        await self.agent.acancel_run(run_id)

    async def is_cancelled(self, run_id: str) -> bool:
        return await ais_cancelled(run_id)

    def keep(self, run_id: str | None, note: str) -> None:
        # agno's history leaves out runs whose status is error or
        # cancelled; this marks the run completed, closed with the note
        keep_aborted_run(getattr(self.agent, "db", None), self.session, run_id, note)

    # -- what reaches the stream from outside agno --------------------------

    def folded(self, fold: Any) -> None:
        """Compaction made ``fold`` in this session's run."""
        self._hand(Compacted.of(fold))

    def _delivered(self, notes: list[Note]) -> None:
        self._hand(Delivered(notes=tuple(DeliveredNote.of(n) for n in notes)))

    def _hand(self, event: TurnEvent) -> None:
        side = self._side
        if side is None:
            log.warning(
                "%s: %s arrived with no run streaming", self.session, event.kind
            )
            return
        side(event)

    async def _drive(self, start: Callable[[], Any]) -> AsyncIterator[TurnEvent]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        ended = object()
        self._side = lambda event: loop.call_soon_threadsafe(queue.put_nowait, event)
        events = _AgnoEvents()

        async def pump() -> None:
            try:
                async for raw in start():
                    for event in events.feed(raw):
                        queue.put_nowait(event)
                queue.put_nowait(events.end())
            except Exception as e:  # noqa: BLE001 - the run's ending, not ours
                queue.put_nowait(events.end(failed=str(e)))
            finally:
                # after whatever a thread handed over before the stream
                # ended, which is already queued behind it
                loop.call_soon_threadsafe(queue.put_nowait, ended)

        task = asyncio.create_task(pump())
        try:
            while (event := await queue.get()) is not ended:
                yield event
        finally:
            self._side = None
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


class _AgnoEvents:
    """agno's stream events as turn events, for one run.

    A ``RunError`` ends the run interrupted, whatever its kind: the
    studio resumes it in place, and an exception out of the stream is
    what fails a run outright.
    """

    def __init__(self) -> None:
        self.run_id: str | None = None
        self._cancelled = False
        self._error: str | None = None

    def feed(self, ev: Any) -> list[TurnEvent]:
        out: list[TurnEvent] = []
        run_id = getattr(ev, "run_id", None)
        if run_id and self.run_id is None:
            self.run_id = run_id
            out.append(RunStarted(run_id=run_id))
        kind = getattr(ev, "event", "")
        if kind == "RunContent":
            # native model thinking rides RunContent as a per-chunk
            # reasoning_content delta (Claude thinking, OpenRouter
            # reasoning, Gemini thoughts, Responses summaries); a chunk
            # can carry thinking, prose, or both
            thinking = getattr(ev, "reasoning_content", None)
            if isinstance(thinking, str) and thinking:
                out.append(ThinkingDelta(text=thinking))
            text = getattr(ev, "content", None)
            if isinstance(text, str) and text:
                out.append(TextDelta(text=text))
        elif kind == "ReasoningContentDelta":
            # the reasoning manager's stream (reasoning=True agents)
            thinking = getattr(ev, "reasoning_content", None)
            if isinstance(thinking, str) and thinking:
                out.append(ThinkingDelta(text=thinking))
        elif kind == "ToolCallStarted":
            tool = getattr(ev, "tool", None)
            args = getattr(tool, "tool_args", None)
            out.append(
                ToolStarted(
                    call_id=str(getattr(tool, "tool_call_id", None) or ""),
                    name=str(getattr(tool, "tool_name", None) or "?"),
                    args=args if isinstance(args, dict) else {},
                )
            )
        elif kind == "ToolCallCompleted":
            # A call that raised completes too, its error as the result,
            # and agno follows it with a ToolCallError: the flag on the
            # completed call is what says so.
            tool = getattr(ev, "tool", None)
            result = getattr(tool, "result", "")
            out.append(
                ToolEnded(
                    call_id=str(getattr(tool, "tool_call_id", None) or ""),
                    name=str(getattr(tool, "tool_name", None) or "?"),
                    # the tool's own output: notes that rode out on it
                    # are the Delivered event before this one
                    result=split(result)[0]
                    if isinstance(result, str)
                    else repr(result),
                    is_error=bool(getattr(tool, "tool_call_error", False)),
                )
            )
        elif kind == "ModelRequestCompleted":
            tokens = getattr(ev, "input_tokens", None)
            if tokens:
                out.append(
                    Usage(
                        input_tokens=int(tokens),
                        cached_tokens=int(getattr(ev, "cache_read_tokens", None) or 0),
                    )
                )
        elif kind == "RunCancelled":
            self._cancelled = True
        elif kind == "RunError":
            self._error = getattr(ev, "content", None) or "provider error"
        return out

    def end(self, failed: str | None = None) -> RunEnded:
        """How the run ended."""
        if failed is not None:
            return RunEnded(status="failed", message=failed)
        if self._cancelled:
            return RunEnded(status="cancelled", message="stopped by the user")
        if self._error is not None:
            return RunEnded(status="interrupted", message=str(self._error))
        return RunEnded(status="completed")


class AgexDriver:
    """An agex session as a :class:`TurnDriver`.

    agex streams turn events itself, deliveries and folds among them,
    and keeps a cancelled or failed run with its closing note, so this
    passes its stream through. The session offers the studio's tools:
    a nontainer ``Toolset`` with the app runtime, the python primer and
    ``vision``, and the studio's own ``sessions`` tool in place of
    nontainer's (agex's ``toolset=`` and ``tools=``).
    """

    def __init__(self, spec: DriverSpec, model: Any) -> None:
        try:
            from agex import Agent
        except ImportError as e:
            raise RuntimeError(
                "the agex loop needs agex: install nontainer-studio[agex]"
            ) from e
        from agex.providers import Settings
        from nontainer.adapters.render import PYTHON_UI_NOTE, resolve_tools_mode
        from nontainer.adapters.tools import Toolset

        from .providers import _effort

        ws = spec.ws
        primer = spec.python_primer
        if resolve_tools_mode(ws, "auto") == "split":
            # the studio renders artifacts beside the reply, so it is the
            # one to promise they do (as the agno toolkit's run_python)
            primer += PYTHON_UI_NOTE.replace(
                "__WS__", "" if ws.root == "/" else ws.root
            )
        toolset = Toolset(
            ws, apps=spec.runtime, python_primer=primer, vision=spec.vision
        )
        tools = []
        if spec.sessions_tool is not None:
            tools.append(_sessions_tool(ws, spec.delegates, spec.sessions_tool))
        agent = Agent(
            model,
            primer=spec.instructions,
            settings=Settings(thinking=_effort()),
            # counted per call, as agno's tool_call_limit is, however
            # many calls a reply makes
            max_tool_calls=spec.tool_calls,
            compaction=spec.compaction,
        )
        self.session = agent.session(
            ws,
            inbox=spec.inbox,
            sessions=spec.delegates,
            toolset=toolset,
            tools=tools,
        )
        self._cancelled: set[str] = set()

    def run(self, prompt: str) -> AsyncIterator[TurnEvent]:
        return self._follow(self.session.stream(prompt))

    def resume(self, run_id: str) -> AsyncIterator[TurnEvent]:
        # agex continues the last turn, which is the interrupted one
        return self._follow(self.session.stream(resume=True))

    async def cancel(self, run_id: str) -> None:
        # recorded, so a stop pressed while no turn runs (between a
        # provider error and its resume) is still seen
        self._cancelled.add(run_id)
        self.session.cancel()

    async def is_cancelled(self, run_id: str) -> bool:
        return run_id in self._cancelled

    def keep(self, run_id: str | None, note: str) -> None:
        """agex keeps a cancelled or failed run with its closing note.
        An interrupted one the turn stops resuming (its resumes spent,
        or a stop pressed while it waited) would stay interrupted, with
        nothing saying it stopped, so it is closed here with ``note``:
        cancelled when the stop was pressed, failed otherwise."""
        runs = self.session.runs
        if not runs or runs[-1].run_id != run_id or runs[-1].status != "interrupted":
            return
        self.session.abandon(
            note, status="cancelled" if run_id in self._cancelled else "failed"
        )

    def folded(self, fold: Any) -> None:
        """agex streams its own folds; nothing reaches it from outside."""

    async def _follow(self, stream: Any) -> AsyncIterator[TurnEvent]:
        ended = False
        try:
            async for event in stream:
                yield event
            ended = True
        finally:
            if not ended:
                # an agex turn goes on when nobody watches it; the studio
                # stopping to watch (shutting down) means the turn stops
                self.session.cancel()


def _sessions_tool(ws: Any, delegates: Any, call: Callable[..., str]) -> Any:
    """The studio's ``sessions`` function as a tool in the protocol's
    shape, with the arguments nontainer's own ``sessions`` tool takes."""
    from nontainer.adapters.tools import Tool, ToolOutput, Toolset

    (nontainers,) = [
        t for t in Toolset(ws, sessions=delegates).tools() if t.name == "sessions"
    ]
    return Tool(
        name="sessions",
        description=call.__doc__ or nontainers.description,
        parameters=nontainers.parameters,
        call=lambda **arguments: ToolOutput(text=call(**arguments)),
    )
