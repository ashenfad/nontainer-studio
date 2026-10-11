"""DummyModel: a scripted agno Model for E2E tests (the agex-ts
``Dummy`` LLM pattern, ported to agno).

Fake the MODEL, keep everything below it real: the agno run loop,
WorkspaceTools, the workspace, the sandbox all execute for real — no
tokens, no key, deterministic. The pytest FakeAgent fakes the whole
agent (fine for server plumbing); this fakes only the LLM, so browser
E2E tests exercise the true stack.

The script rides IN the user message — no side channel between the
test process and the server. Directive lines:

    !think Hmm, let me consider this.
    !tool file_write {"path": "/notes.md", "content": "hi"}
    !tool run_python {"code": "print(1)"}
    !text Here is your reply.
    !fail provider overloaded

One model "turn": if the message has ``!tool`` directives and they
haven't run yet, emit the tool calls (the real loop executes them and
reinvokes); otherwise emit the ``!text`` reply (streamed in two deltas
to exercise the streaming path). A message with no directives echoes
back — handy for smoke.

``!fail`` makes a model call raise agno's ``ModelProviderError`` with
the rest of the line as its message, which is how a provider failure
reaches the run. Failures stand in for the reply call — the first call
after the tools ran, or the first call at all in a script without
tools — and each ``!fail`` line is spent on one call, in order; the
call after the last one proceeds with the ``!text`` reply. So a script
that writes a file, fails once, and then answers reads::

    !tool file_write {"path": "/notes.md", "content": "hi"}
    !fail provider overloaded
    !text Wrote the notes.

A failure is counted per user message, on the model instance, so a
resumed run (which keeps the message) sees the failure as spent while a
new turn with the same text fails afresh. Model-level retries count as
calls: under the server's model, which retries a failed call twice,
three ``!fail`` lines are what reach the run; the tests build the model
without retries, where one does. A model offered no tools never fails,
for the reason it never calls one: the naming pass hands a tool-less
agent the whole transcript, directives and all.

Select it with ``NONTAINER_STUDIO_MODEL=dummy``. On the agex loop the
same script runs through ``agex_dummy.DummyProvider``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator, List

from agno.exceptions import ModelProviderError
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse


@dataclass
class Script:
    """A message's directives, in the order they were written."""

    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    """``(name, arguments as JSON)``, one per ``!tool`` line."""
    reply: list[str] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)
    fails: list[str] = field(default_factory=list)


def read_script(text: str) -> Script:
    """The directives in ``text``; a bad ``!tool`` argument raises, so
    a mistyped script fails loudly rather than calling nothing."""
    script = Script()
    for line in text.splitlines():
        if line.startswith("!tool "):
            name, _, args = line[len("!tool ") :].partition(" ")
            json.loads(args or "{}")
            script.tool_calls.append((name, args or "{}"))
        elif line.startswith("!text "):
            script.reply.append(line[len("!text ") :])
        elif line.startswith("!think "):
            script.thinking.append(line[len("!think ") :])
        elif line.startswith("!fail "):
            script.fails.append(line[len("!fail ") :])
    return script


class DummyModel(Model):
    def __init__(self) -> None:
        super().__init__(id="dummy", name="Dummy", provider="dummy")
        # ``!fail`` lines already spent, per user message id
        self._failed: dict[str, int] = {}

    # -- script interpretation ---------------------------------------------

    @staticmethod
    def _plan(messages: List[Message], offered: bool = True) -> ModelResponse:
        """``offered`` is whether the caller put any tools on the table.
        A model offered none calls none, whatever the script says: the
        studio's naming pass hands a tool-less agent the transcript of
        a session, directives and all, and a reply that asked for
        ``file_write`` there would be answered with an error about a
        function that was never offered."""
        last_user = next((m for m in reversed(messages) if m.role == "user"), None)
        text = str(getattr(last_user, "content", "") or "")
        tools_ran = (
            any(
                m.role == "tool"
                for m in messages[messages.index(last_user) + 1 :]  # type: ignore[arg-type]
            )
            if last_user is not None
            else False
        )

        script = read_script(text)
        tool_calls = [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
            for i, (name, args) in enumerate(script.tool_calls)
        ]
        reply, thinking = script.reply, script.thinking

        response = ModelResponse(role="assistant")
        if thinking and not tools_ran:
            # thinking precedes the first action, like real reasoners
            response.reasoning_content = "\n".join(thinking)
        if tool_calls and offered and not tools_ran:
            response.tool_calls = tool_calls
        else:
            response.content = "\n".join(reply) or f"dummy: {text[:200]}"
        return response

    def _maybe_fail(self, messages: List[Message], offered: bool) -> None:
        """Raise the next unspent ``!fail`` of the script, when this
        call is the reply call; do nothing otherwise."""
        if not offered:
            return
        last_user = next((m for m in reversed(messages) if m.role == "user"), None)
        if last_user is None:
            return
        text = str(getattr(last_user, "content", "") or "")
        fails = read_script(text).fails
        if not fails or self._plan(messages, offered=offered).tool_calls:
            return
        spent = self._failed.get(last_user.id, 0)
        if spent >= len(fails):
            return
        self._failed[last_user.id] = spent + 1
        raise ModelProviderError(fails[spent], model_name=self.name, model_id=self.id)

    # -- Model surface -------------------------------------------------------

    def invoke(self, messages: List[Message], **kwargs: Any) -> ModelResponse:
        offered = bool(kwargs.get("tools"))
        self._maybe_fail(messages, offered)
        return self._plan(messages, offered=offered)

    async def ainvoke(self, messages: List[Message], **kwargs: Any) -> ModelResponse:
        offered = bool(kwargs.get("tools"))
        self._maybe_fail(messages, offered)
        return self._plan(messages, offered=offered)

    def invoke_stream(
        self, messages: List[Message], **kwargs: Any
    ) -> Iterator[ModelResponse]:
        offered = bool(kwargs.get("tools"))
        self._maybe_fail(messages, offered)
        yield from self._stream_chunks(self._plan(messages, offered=offered))

    async def ainvoke_stream(
        self, messages: List[Message], **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        offered = bool(kwargs.get("tools"))
        self._maybe_fail(messages, offered)
        for chunk in self._stream_chunks(self._plan(messages, offered=offered)):
            yield chunk

    @staticmethod
    def _stream_chunks(response: ModelResponse) -> Iterator[ModelResponse]:
        """Split a text reply into two deltas so streaming assembly is
        actually exercised; tool calls ride one delta (as providers do);
        thinking streams first, as its own delta (as reasoners do)."""
        if response.reasoning_content:
            yield ModelResponse(
                role="assistant", reasoning_content=response.reasoning_content
            )
        if response.tool_calls:
            response.reasoning_content = None
            yield response
            return
        text = response.content or ""
        mid = len(text) // 2
        for part in (text[:mid], text[mid:]):
            if part:
                yield ModelResponse(role="assistant", content=part)

    # -- unused abstract hooks (we build ModelResponse directly) -------------

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response  # already a ModelResponse

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response  # already a ModelResponse
