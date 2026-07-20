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

One model "turn": if the message has ``!tool`` directives and they
haven't run yet, emit the tool calls (the real loop executes them and
reinvokes); otherwise emit the ``!text`` reply (streamed in two deltas
to exercise the streaming path). A message with no directives echoes
back — handy for smoke.

Select it with ``NONTAINER_STUDIO_MODEL=dummy``.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Iterator, List

from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse


class DummyModel(Model):
    def __init__(self) -> None:
        super().__init__(id="dummy", name="Dummy", provider="dummy")

    # -- script interpretation ---------------------------------------------

    @staticmethod
    def _plan(messages: List[Message]) -> ModelResponse:
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

        tool_calls: list[dict] = []
        reply: list[str] = []
        thinking: list[str] = []
        for line in text.splitlines():
            if line.startswith("!tool "):
                name, _, args = line[len("!tool ") :].partition(" ")
                json.loads(args or "{}")  # fail loudly on a bad script
                tool_calls.append(
                    {
                        "id": f"call_{len(tool_calls)}",
                        "type": "function",
                        "function": {"name": name, "arguments": args or "{}"},
                    }
                )
            elif line.startswith("!text "):
                reply.append(line[len("!text ") :])
            elif line.startswith("!think "):
                thinking.append(line[len("!think ") :])

        response = ModelResponse(role="assistant")
        if thinking and not tools_ran:
            # thinking precedes the first action, like real reasoners
            response.reasoning_content = "\n".join(thinking)
        if tool_calls and not tools_ran:
            response.tool_calls = tool_calls
        else:
            response.content = "\n".join(reply) or f"dummy: {text[:200]}"
        return response

    # -- Model surface -------------------------------------------------------

    def invoke(self, messages: List[Message], **kwargs: Any) -> ModelResponse:
        return self._plan(messages)

    async def ainvoke(self, messages: List[Message], **kwargs: Any) -> ModelResponse:
        return self._plan(messages)

    def invoke_stream(
        self, messages: List[Message], **kwargs: Any
    ) -> Iterator[ModelResponse]:
        yield from self._stream_chunks(self._plan(messages))

    async def ainvoke_stream(
        self, messages: List[Message], **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        for chunk in self._stream_chunks(self._plan(messages)):
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
