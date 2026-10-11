"""What a session's agent was given, read the same way whichever loop
it runs on."""

from collections.abc import AsyncIterator, Awaitable
from types import SimpleNamespace
from typing import Any, Callable


def instructions(session: Any) -> str:
    if session.loop == "agex":
        return session.driver.session.agent.primer
    return session.driver.agent.instructions


def tool(session: Any, name: str) -> Callable[..., str]:
    """The tool ``name`` the agent holds, as a function of its
    arguments returning what the model reads."""
    if session.loop == "agex":
        held = session.driver.session._tools[name]
        return lambda **arguments: held.call(**arguments).text
    return next(
        t for t in session.driver.agent.tools if getattr(t, "__name__", "") == name
    )


def tool_names(session: Any) -> list[str]:
    """The names of the tools the agent is offered."""
    if session.loop == "agex":
        return [spec.name for spec in session.driver.session._specs]
    names = []
    for t in session.driver.agent.tools:
        functions = getattr(t, "functions", None)
        names.extend(functions or [getattr(t, "__name__", "")])
    return names


def tool_call_cap(session: Any) -> int | None:
    if session.loop == "agex":
        return session.driver.session.agent.max_tool_calls
    return session.driver.agent.tool_call_limit


def sent(session: Any) -> str:
    """What the last turn sent the model as its message."""
    if session.loop == "agex":
        run = session.driver.session.runs[-1]
        return next(m.text for m in run.messages if m.role == "user")
    stored = session.driver.agent.db.get_session(session.name)
    return next(
        str(m.content) for m in stored.runs[-1].messages or [] if m.role == "user"
    )


def patch_reply(monkeypatch: Any, before: Callable[[Any], Awaitable[None]]) -> None:
    """Await ``before(plan)`` ahead of each reply the dummy model gives,
    on either loop. ``plan`` says what the reply will be: ``text`` (the
    script's message), ``calls`` (whether it calls the script's tools)
    and ``reply`` (its text when it does not); and what the request
    was: ``users`` (the text of each user message it carried) and
    ``offered`` (whether it offered tools, which the agent's own turn
    does and a summary or title call does not)."""
    from nontainer_studio.dummy import DummyModel

    agno = DummyModel.ainvoke_stream

    async def agno_reply(self, messages, **kwargs):
        last = next((m for m in reversed(messages) if m.role == "user"), None)
        offered = bool(kwargs.get("tools")) and kwargs.get("tool_choice") != "none"
        plan = DummyModel._plan(messages, offered=offered)
        await before(
            SimpleNamespace(
                text=str(getattr(last, "content", "") or ""),
                calls=bool(plan.tool_calls),
                reply=str(plan.content or ""),
                users=[str(m.content) for m in messages if m.role == "user"],
                offered=offered,
            )
        )
        async for chunk in agno(self, messages, **kwargs):
            yield chunk

    monkeypatch.setattr(DummyModel, "ainvoke_stream", agno_reply)
    try:
        from nontainer_studio.agex_dummy import DummyProvider, plan
    except ImportError:
        return
    agex = DummyProvider._reply

    async def agex_reply(self, messages, info):
        offered = bool(info.function_tools)
        step = plan(messages, offered)
        await before(
            SimpleNamespace(
                text=step.text,
                calls=step.calls,
                reply=step.reply,
                users=[
                    part.content
                    for m in messages
                    for part in getattr(m, "parts", ())
                    if type(part).__name__ == "UserPromptPart"
                    and isinstance(part.content, str)
                ],
                offered=offered,
            )
        )
        async for chunk in agex(self, messages, info):
            yield chunk

    monkeypatch.setattr(DummyProvider, "_reply", agex_reply)


def replying(reply: Callable[[], AsyncIterator[str]]) -> dict[str, Any]:
    """A ``Registry``'s model hooks for a model whose every reply is
    ``reply()``: the text it yields, streamed, and an exception it
    raises is the provider failing there. Spread into the registry
    (``Registry(**replying(...), ...)``) and either loop gets it."""
    from agno.models.response import ModelResponse

    from nontainer_studio.dummy import DummyModel

    class AgnoModel(DummyModel):
        async def ainvoke_stream(self, messages, **kwargs):
            async for text in reply():
                yield ModelResponse(role="assistant", content=text)

    hooks: dict[str, Any] = {"model_factory": lambda *a, **k: AgnoModel()}
    try:
        from nontainer_studio.agex_dummy import DummyProvider
    except ImportError:
        return hooks

    class AgexProvider(DummyProvider):
        async def _reply(self, messages, info):
            async for text in reply():
                yield text

    hooks["agex_model"] = lambda spec: AgexProvider()
    return hooks


def tool_results(session: Any) -> list[str]:
    """Every tool result the session's conversation holds, as the model
    read it."""
    if session.loop == "agex":
        return [
            part.content
            for run in session.driver.session.runs
            for m in run.messages
            if m.role == "tool"
            for part in m.parts
        ]
    stored = session.driver.agent.db.get_session(session.name)
    return [
        str(m.content)
        for run in (stored.runs or [])
        for m in (run.messages or [])
        if m.role == "tool"
    ]


def fake_provider() -> Any:
    """agex's stand-in for test_server's ``FakeAgent``: each turn lists
    the workspace with ``terminal``, then says "hello world" in two
    deltas. ``seen`` holds each turn's message, as ``FakeAgent``'s
    does."""
    from pydantic_ai.models.function import DeltaToolCall

    from nontainer_studio.agex_dummy import DummyProvider, plan

    class FakeProvider(DummyProvider):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[str] = []

        async def _reply(self, messages, info):
            step = plan(messages, bool(info.function_tools))
            if info.function_tools and not step.tools_ran:
                self.seen.append(step.text)
                self._calls += 1
                yield {
                    1: DeltaToolCall(
                        name="terminal",
                        json_args='{"command": "ls"}',
                        tool_call_id=f"call_{self._calls}",
                    )
                }
                return
            yield "hello "
            yield "world"

    return FakeProvider()


def stored_runs(session: Any) -> list[SimpleNamespace]:
    """The runs the session's conversation holds, the same shape on
    either loop: ``run_id``, ``status`` (lowercase) and ``messages``,
    each a ``role`` and its ``content`` as text."""
    if session.loop == "agex":
        return [
            SimpleNamespace(
                run_id=run.run_id,
                status=run.status,
                messages=[
                    SimpleNamespace(role=m.role, content=_agex_text(m))
                    for m in run.messages
                ],
            )
            for run in session.driver.session.runs
        ]
    stored = session.driver.agent.db.get_session(session.name)
    return [
        SimpleNamespace(
            run_id=run.run_id,
            status=str(getattr(run.status, "value", run.status)).lower(),
            messages=[
                SimpleNamespace(role=m.role, content=str(m.content))
                for m in (run.messages or [])
            ],
        )
        for run in ((stored.runs or []) if stored is not None else [])
    ]


def _agex_text(message: Any) -> str:
    texts = []
    for part in message.parts:
        text = getattr(part, "text", None)
        if text is None:
            text = getattr(part, "content", None)
        if isinstance(text, str):
            texts.append(text)
    return "\n".join(texts)
