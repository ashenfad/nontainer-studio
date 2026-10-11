"""The scripted test model (``dummy.py``) as an agex provider.

The same directives, read from the same message, so a test's script
runs on either loop: ``!tool`` lines are called until a tool has run
since the message, then the ``!text`` reply is streamed in two deltas,
``!think`` comes first, and a message with no directives is echoed.
Each ``!fail`` raises an overloaded provider's error (529, which agex
counts as transient, so the run is interrupted and the studio resumes
it) in place of the reply call, once per line. Failures are counted per
user message, by its id, as on agno: a resumed run sends the same
message and sees the failure as spent, while a new turn with the same
text (an edit rerunning it, say) is a new message and fails afresh. A model offered no tools calls none
and never fails, as on agno. agex's notice that a turn has made its
tool calls is read past, as agno's refusals are: the script is the
message before it.

A :class:`~agex.providers.pydanticai.PydanticAIProvider` over
pydantic-ai's ``FunctionModel``, as agex's own scripted provider is, so
requests and replies go through the mapping a real model's do.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from agex.agent import limit_reached
from agex.providers import ProviderEvent, Reply, Settings, ToolSpec
from agex.providers.pydanticai import PydanticAIProvider
from agex.record import Message
from pydantic_ai import messages as pai
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaThinkingPart,
    DeltaToolCall,
    FunctionModel,
)

from .dummy import Script, read_script

MODEL_NAME = "dummy"

_head, _, _tail = limit_reached(-1).partition("-1")
_CAP_NOTICE = re.compile(rf"\[{re.escape(_head)}\d+{re.escape(_tail)}\]")
"""agex's own message when a turn has made its tool calls: the loop
speaking, not a message a script was written in."""

_MESSAGE: ContextVar[str] = ContextVar("dummy_message", default="")
"""The id of the user message the request being answered replies to.
pydantic-ai's messages carry no ids, so :meth:`DummyProvider.stream`
reads it off agex's and the reply reads it here: per turn, as each
turn is a task of its own."""


@dataclass
class Plan:
    """What the dummy does with a request."""

    text: str
    """The script's message."""
    script: Script
    tools_ran: bool
    """Whether a tool has run since the message."""
    calls: bool
    """Whether this reply calls the script's tools."""

    @property
    def reply(self) -> str:
        return "\n".join(self.script.reply) or f"dummy: {self.text[:200]}"


def plan(messages: Sequence[pai.ModelMessage], offered: bool = True) -> Plan:
    """The reply ``messages`` get, ``offered`` saying whether the
    request offered tools."""
    at, text = _last_user(messages)
    tools_ran = any(
        isinstance(part, (pai.ToolReturnPart, pai.RetryPromptPart))
        for message in messages[at + 1 :]
        if isinstance(message, pai.ModelRequest)
        for part in message.parts
    )
    script = read_script(text)
    calls = bool(script.tool_calls) and offered and not tools_ran
    return Plan(text=text, script=script, tools_ran=tools_ran, calls=calls)


class DummyProvider(PydanticAIProvider):
    def __init__(self) -> None:
        # ``!fail`` lines already spent, per user message id
        self._failed: dict[str, int] = {}
        self._calls = 0
        super().__init__(
            FunctionModel(stream_function=self._stream, model_name=MODEL_NAME)
        )

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        settings: Settings = Settings(),
    ) -> AsyncIterator[ProviderEvent]:
        # No usage, as the agno dummy reports none. FunctionModel's is a
        # flat guess, and agex would take it for the conversation's
        # size, so compaction never measured what was sent.
        token = _MESSAGE.set(_last_user_id(messages))
        try:
            async for event in super().stream(messages, tools, settings):
                if isinstance(event, Reply):
                    event = Reply(message=replace(event.message, usage=None))
                yield event
        finally:
            _MESSAGE.reset(token)

    def _stream(
        self, messages: list[pai.ModelMessage], info: AgentInfo
    ) -> AsyncIterator[Any]:
        # ``_reply`` looked up per call, as the agno dummy's methods
        # are, so a test that patches it reaches a session already open
        return self._reply(messages, info)

    async def _reply(
        self, messages: list[pai.ModelMessage], info: AgentInfo
    ) -> AsyncIterator[Any]:
        offered = bool(info.function_tools)
        step = plan(messages, offered)
        fails = step.script.fails
        if fails and offered and not step.calls:
            message = _MESSAGE.get()
            spent = self._failed.get(message, 0)
            if spent < len(fails):
                self._failed[message] = spent + 1
                raise ModelHTTPError(
                    status_code=529, model_name=MODEL_NAME, body=fails[spent]
                )
        if step.script.thinking and not step.tools_ran:
            yield {0: DeltaThinkingPart(content="\n".join(step.script.thinking))}
        if step.calls:
            for index, (name, args) in enumerate(step.script.tool_calls, start=1):
                self._calls += 1
                yield {
                    index: DeltaToolCall(
                        name=name, json_args=args, tool_call_id=f"call_{self._calls}"
                    )
                }
            return
        reply = step.reply
        mid = len(reply) // 2
        for part in (reply[:mid], reply[mid:]):
            if part:
                yield part


def _last_user(messages: Sequence[pai.ModelMessage]) -> tuple[int, str]:
    """Where the last user message is, and its text."""
    for at in range(len(messages) - 1, -1, -1):
        message = messages[at]
        if not isinstance(message, pai.ModelRequest):
            continue
        texts = [
            part.content if isinstance(part.content, str) else _text(part.content)
            for part in message.parts
            if isinstance(part, pai.UserPromptPart)
        ]
        text = "\n".join(texts)
        if texts and not _CAP_NOTICE.fullmatch(text):
            return at, text
    return -1, ""


def _last_user_id(messages: Sequence[Message]) -> str:
    """The id of the last user message a script could be written in."""
    for message in reversed(messages):
        if message.role == "user" and not _CAP_NOTICE.fullmatch(message.text):
            return message.id
    return ""


def _text(content: Sequence[Any]) -> str:
    return "\n".join(item for item in content if isinstance(item, str))
