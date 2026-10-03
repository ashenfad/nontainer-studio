"""Tool-result compression that keeps a mid-run message verbatim.

agno compresses old tool results by asking a model to summarise them,
which is right for the tool's own output and wrong for anything else
that rides in it. A message the human sent mid-turn is appended to the
tool result that carried it, so a plain compression pass would hand a
person's words to a summariser and put the paraphrase in the model's
memory: the agent would go on acting on a retelling of an instruction
it was given exactly once.

So the block is cut off before the compressor sees it and re-appended
after, byte for byte. Only the tool's half is summarised, and what the
person said stands as they said it.

The other thing it does is keep a compression of an EARLIER run's tool
result. agno compresses what it sends, and an earlier run is sent as
copies of its messages that are dropped before the run is stored, so
agno itself keeps only the compressions it made in the run they belong
to. A tool result that first crossed the watermark in a later run was
compressed again on every turn after: one model call per result per
turn, growing with the conversation, and each fresh summary a slightly
different prompt, so the cache missed too. The compression is copied
onto the session's own message, which agno stores at the end of the
run, and the next run's copy arrives already compressed.
"""

from __future__ import annotations

from typing import Any, Optional

from agno.compression.manager import CompressionManager
from agno.models.message import Message
from nontainer.inbox import split


class InboxAwareCompression(CompressionManager):
    """The studio's compression manager: an inbox block survives a
    compression pass unchanged, and an earlier run's tool result is
    compressed once.

    ``hold_session`` is a pre hook: agno hands it the session the run
    loaded, which is the object it stores when the run ends.

    The narrowest seam agno offers is the per-message compression call,
    and there are two of them — one sync, one async — because the sync
    and async run loops each have their own. Both split the message,
    compress a copy carrying only the tool's own output, and put the
    block back on the answer.
    """

    #: The session the current run loaded; set by ``hold_session``.
    _session: Any = None

    def hold_session(self, session: Any) -> None:
        """Pre hook: remember the run's session, whose messages are the
        originals the history copies were made from."""
        self._session = session

    def compress(
        self, messages: list[Message], run_metrics: Optional[Any] = None
    ) -> None:
        pending = _pending_history(messages)
        super().compress(messages, run_metrics=run_metrics)
        self._keep(pending)

    async def acompress(
        self, messages: list[Message], run_metrics: Optional[Any] = None
    ) -> None:
        pending = _pending_history(messages)
        await super().acompress(messages, run_metrics=run_metrics)
        self._keep(pending)

    def _keep(self, pending: list[Message]) -> None:
        """Copy what this pass compressed onto the stored originals.

        A run resumed after a provider error skips the pre hooks, so the
        session held may be an earlier run's: writing to it changes
        nothing that is stored, and the result is compressed again next
        turn, as it was before.
        """
        done = {m.id: m.compressed_content for m in pending if m.compressed_content}
        if not done or self._session is None:
            return
        for run in self._session.runs or []:
            for original in run.messages or []:
                if original.id in done and original.compressed_content is None:
                    original.compressed_content = done[original.id]

    def _compress_tool_result(
        self,
        tool_result: Message,
        run_metrics: Optional[Any] = None,
    ) -> Optional[str]:
        bare, notes = _cut(tool_result)
        if not notes:
            return super()._compress_tool_result(tool_result, run_metrics=run_metrics)
        compressed = super()._compress_tool_result(
            _bare_copy(tool_result, bare), run_metrics=run_metrics
        )
        return None if compressed is None else compressed + notes

    async def _acompress_tool_result(
        self,
        tool_result: Message,
        run_metrics: Optional[Any] = None,
    ) -> Optional[str]:
        bare, notes = _cut(tool_result)
        if not notes:
            return await super()._acompress_tool_result(
                tool_result, run_metrics=run_metrics
            )
        compressed = await super()._acompress_tool_result(
            _bare_copy(tool_result, bare), run_metrics=run_metrics
        )
        return None if compressed is None else compressed + notes


def _pending_history(messages: list[Message]) -> list[Message]:
    """The earlier runs' tool results this pass may compress: copies,
    with an id to find their originals by."""
    return [
        m
        for m in messages
        if m.role == "tool" and m.from_history and m.id and m.compressed_content is None
    ]


def _cut(message: Message) -> tuple[str, str]:
    """``(the tool's own output, the inbox block)`` for a tool message;
    an empty block when it carries none, which is the usual case.

    A block is recognised by the length trailer it closes with, so a
    tool that merely PRINTED the delimiter is left whole.
    """
    content = getattr(message, "content", None)
    if not isinstance(content, str):
        return ("", "")
    return split(content)


def _bare_copy(message: Message, bare: str) -> Message:
    """The same message with only the tool's own output in it.

    A copy rather than a temporary mutation: the async path compresses
    every pending tool result concurrently, and the messages it works
    on are the agent's live memory.
    """
    copy = getattr(message, "model_copy", None)
    if copy is not None:
        return copy(update={"content": bare})
    import copy as copy_module

    other = copy_module.copy(message)
    other.content = bare
    return other
