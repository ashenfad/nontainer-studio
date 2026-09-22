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
"""

from __future__ import annotations

from typing import Any, Optional

from agno.compression.manager import CompressionManager
from agno.models.message import Message
from nontainer.inbox import split


class InboxAwareCompression(CompressionManager):
    """The studio's compression manager: an inbox block survives a
    compression pass unchanged.

    The narrowest seam agno offers is the per-message compression call,
    and there are two of them — one sync, one async — because the sync
    and async run loops each have their own. Both split the message,
    compress a copy carrying only the tool's own output, and put the
    block back on the answer.
    """

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
