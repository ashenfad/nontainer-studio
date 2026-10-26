"""Sessions on the agex loop (``NONTAINER_STUDIO_LOOP=agex``): turns
through ``AgexDriver``, the tools the studio's agent has on agno too,
and which loop a session runs on.

The model is agex's scripted provider, through the registry's
``agex_model`` hook; everything below it is real."""

import json
import time

import pytest
from nontainer.conformance.corpus import ModelStep, ToolCall, calls, says, writes
from starlette.testclient import TestClient

pytest.importorskip("agex")

from agex.providers.scripted import ScriptedProvider  # noqa: E402

from nontainer_studio import server, turns  # noqa: E402
from nontainer_studio import sessions as sessions_mod  # noqa: E402
from nontainer_studio.drivers import AgexDriver  # noqa: E402

MODEL = "anthropic:claude-test"  # a spec whose model takes images


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setenv("NONTAINER_STUDIO_LOOP", "agex")
    monkeypatch.delenv("NONTAINER_STUDIO_WSGIT", raising=False)
    monkeypatch.delenv("NONTAINER_STUDIO_SESSIONS", raising=False)


class Scripts:
    """The steps each session's model takes, by session name; a model
    built for a session reads its own list."""

    def __init__(self) -> None:
        self.steps: dict[str, list[ModelStep]] = {}

    def provider(self, spec: str) -> ScriptedProvider:
        def next_step(sent: list[str]) -> ModelStep:
            for name, queue in self.steps.items():
                if queue and any(name in text for text in sent[:2]):
                    return queue.pop(0)
            return says("(no script)")

        return ScriptedProvider(next_step)


@pytest.fixture
def delegate_tool_calls():
    return None


@pytest.fixture
def studio(tmp_path, delegate_tool_calls):
    scripts = Scripts()
    registry = sessions_mod.Registry(
        model_factory=lambda *a: None,
        store=tmp_path,
        default_model=MODEL,
        agex_model=scripts.provider,
        delegate_tool_calls=delegate_tool_calls,
    )
    # a title is a model call of its own, on the session's model spec;
    # none here, where the spec names no model anybody serves
    registry.retitle = lambda session: None
    with TestClient(server.build_app(registry)) as client:
        yield client, registry, scripts
    registry.close()


def _until_done(client, session: str, since: int = 0) -> list[dict]:
    deadline = time.monotonic() + 10
    events: list[dict] = []
    while time.monotonic() < deadline:
        data = client.get(f"/api/sessions/{session}/events?since={since}&wait=0")
        events = data.json()["events"]
        if any(e["type"] == "done" for e in events):
            return events
        time.sleep(0.02)
    raise AssertionError(f"no done event within 10s: {events}")


def _chat(client, session: str, message: str) -> list[dict]:
    started = client.post(f"/api/sessions/{session}/chat", json={"message": message})
    assert started.status_code == 200, started.text
    return _until_done(client, session, since=started.json()["since"])


def test_a_turn_runs_on_agex_and_lands_in_the_transcript(studio):
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    assert session.loop == "agex" and isinstance(session.driver, AgexDriver)
    scripts.steps["s1"] = [
        writes("/workspace/a.txt", "A"),
        says("wrote it"),
    ]
    events = _chat(client, "s1", "s1: write a")
    assert [e["type"] for e in events if e["type"] != "usage"] == [
        "user",
        "tool_start",
        "tool_end",
        "text",
        "done",
    ]
    start = next(e for e in events if e["type"] == "tool_start")
    end = next(e for e in events if e["type"] == "tool_end")
    assert start["call_id"] and start["call_id"] == end["call_id"]
    assert end["is_error"] is False
    assert session.ws.files.read("/workspace/a.txt") == b"A"
    done = events[-1]
    assert done["run_id"] and done["head"] == session.ws.head


def test_a_provider_error_resumes_the_run_in_place(studio):
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    scripts.steps["s1"] = [
        writes("/workspace/a.txt", "A"),
        ModelStep(fail="provider"),
        says("done after all"),
    ]
    events = _chat(client, "s1", "s1: write a")
    notices = [e["text"] for e in events if e["type"] == "notice"]
    assert notices == ["provider error — resuming the turn where it stopped"]
    assert not any(e["type"] == "error" for e in events)
    text = "".join(e["delta"] for e in events if e["type"] == "text")
    assert text == "done after all"


def test_an_agex_session_has_the_studios_tools(studio, monkeypatch):
    """What the agno agent has: test_app, view_image where the model
    takes images, and the studio's own sessions tool, which answers
    `published` from the studio's app registry."""
    monkeypatch.setenv("NONTAINER_STUDIO_SESSIONS", "1")
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    offered = {spec.name for spec in session.driver.session._specs}
    assert {"test_app", "view_image", "sessions"} <= offered
    scripts.steps["s1"] = [calls("sessions", action="published"), says("none yet")]
    _chat(client, "s1", "s1: what is published?")
    run = session.driver.session.runs[-1]
    (result,) = run.messages[2].parts
    assert result.content == "The human has published nothing yet."


def test_a_text_only_model_is_offered_no_view_image(studio):
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    registry.set_model(session, "ollama:llama-test")
    assert session.loop == "agex"
    offered = {spec.name for spec in session.driver.session._specs}
    assert "view_image" not in offered and "test_app" in offered


def test_an_agex_session_sends_its_models_settings(studio):
    """What the studio sends a model on agno beyond its thinking reaches
    the agex session too: caching for Claude, a pin for OpenRouter."""
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    registry.set_model(session, "anthropic:claude-sonnet-5-5")
    assert session.driver.session.agent.settings.extra == {"cache": True}
    registry.set_model(session, "openrouter:qwen/qwen3.6-35b-a3b@deepinfra")
    assert session.driver.session.agent.settings.extra == {
        "openrouter_provider": {"order": ["deepinfra"], "allow_fallbacks": False}
    }


def test_a_session_stays_on_the_loop_that_wrote_its_conversation(studio, monkeypatch):
    """The knob picks a new session's loop; one holding a conversation
    stays on the loop that wrote it, whatever the knob says."""
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    scripts.steps["s1"] = [says("hi")]
    _chat(client, "s1", "s1: hello")
    monkeypatch.setenv("NONTAINER_STUDIO_LOOP", "agno")
    assert registry._loop_of("s1", registry.get("s1").ws) == "agex"
    client.post("/api/sessions", json={"name": "s2"})
    assert registry.get("s2").loop == "agno"


def test_a_note_queued_mid_turn_lands_before_the_result_it_rode_on(studio):
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    note = session.inbox.put("use a log scale")
    scripts.steps["s1"] = [writes("/workspace/a.txt", "A"), says("ok")]
    events = _chat(client, "s1", "s1: write a")
    kinds = [e["type"] for e in events]
    interject = next(e for e in events if e["type"] == "interject")
    assert (interject["id"], interject["text"]) == (note.id, "use a log scale")
    assert (
        kinds.index("tool_start") < kinds.index("interject") < kinds.index("tool_end")
    )
    end = next(e for e in events if e["type"] == "tool_end")
    assert "use a log scale" not in end["result"]


def test_a_stop_pressed_while_waiting_to_resume_is_honored(studio, monkeypatch):
    monkeypatch.setattr(turns, "RESUME_BACKOFFS", (5.0,))
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    scripts.steps["s1"] = [ModelStep(fail="provider"), says("never said")]
    started = client.post("/api/sessions/s1/chat", json={"message": "s1: go"})
    since = started.json()["since"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        events = client.get(f"/api/sessions/s1/events?since={since}&wait=0").json()
        if any(e["type"] == "notice" for e in events["events"]):
            break
        time.sleep(0.02)
    assert client.post("/api/sessions/s1/cancel").status_code == 200
    events = _until_done(client, "s1", since=since)
    notices = [e["text"] for e in events if e["type"] == "notice"]
    assert notices[-1] == "turn stopped"
    assert not any(e["type"] == "text" for e in events)


def test_a_delegate_runs_on_its_parents_loop(studio, monkeypatch):
    monkeypatch.setenv("NONTAINER_STUDIO_SESSIONS", "1")
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    # the delegate's brief names its parent, so its script is looked up
    # first
    scripts.steps["SCOUT"] = [writes("/workspace/s.txt", "S"), says("wrote s.txt")]
    scripts.steps["s1"] = [
        ModelStep(
            tool_calls=(
                ToolCall(
                    name="sessions",
                    args={
                        "action": "ask",
                        "name": "scout",
                        "task": "SCOUT write s.txt",
                        "wait": True,
                    },
                ),
            )
        ),
        says("the scout wrote it"),
    ]
    scripts.steps = {"SCOUT": scripts.steps["SCOUT"], "s1": scripts.steps["s1"]}
    events = _chat(client, "s1", "s1: get it written")
    text = "".join(e["delta"] for e in events if e["type"] == "text")
    assert text == "the scout wrote it"
    # the answer came back as the tool's result, which delivered it: no
    # turn is woken to deliver it again
    assert [e["type"] for e in events].count("delegate") == 1
    assert registry.get("s1").answered_delegates() == []
    (child,) = [n for n in registry._manifest()["delegates"] if n.startswith("s1.")]
    # its conversation is one agex wrote: reopened, it stays on agex
    assert registry.open(child).loop == "agex"


@pytest.mark.parametrize("delegate_tool_calls", [2])
def test_a_delegates_tool_calls_are_capped_per_call(studio, monkeypatch):
    """The cap counts calls, as agno's does: three in one reply under a
    cap of two, and the third is not run."""
    monkeypatch.setenv("NONTAINER_STUDIO_SESSIONS", "1")
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    scripts.steps["SCOUT"] = [
        ModelStep(
            tool_calls=tuple(
                ToolCall(
                    name="file_write",
                    args={"path": f"/workspace/{n}.txt", "content": n},
                )
                for n in "abc"
            )
        ),
        says("wrote two"),
    ]
    scripts.steps["s1"] = [
        ModelStep(
            tool_calls=(
                ToolCall(
                    name="sessions",
                    args={
                        "action": "ask",
                        "name": "scout",
                        "task": "SCOUT write a, b and c",
                        "wait": True,
                    },
                ),
            )
        ),
        says("the scout wrote two"),
    ]
    scripts.steps = {"SCOUT": scripts.steps["SCOUT"], "s1": scripts.steps["s1"]}
    _chat(client, "s1", "s1: get them written")
    (child,) = [n for n in registry._manifest()["delegates"] if n.startswith("s1.")]
    files = registry.open(child).ws.files
    assert files.exists("/workspace/a.txt") and files.exists("/workspace/b.txt")
    assert not files.exists("/workspace/c.txt")


def test_the_dummy_reads_the_same_script_on_agex():
    """The scripted test model as agex's provider: the request that
    would call the tools does, each ``!fail`` costs the reply one
    attempt, as an overloaded provider (agex resumes it), the reply
    comes next, and a request offered no tools never fails. agex's
    notice that a turn has made its tool calls is read past."""
    import asyncio

    from agex.agent import _notice
    from agex.providers import Reply, ToolSpec
    from agex.record import Message, Text, ToolResult
    from agex.record import ToolCall as Call
    from pydantic_ai.exceptions import ModelHTTPError

    from nontainer_studio.agex_dummy import DummyProvider

    def said(text):
        return Message(id=text[:8], role="user", parts=(Text(text=text),))

    async def reply(provider, messages, tools):
        async for event in provider.stream(messages, tools):
            if isinstance(event, Reply):
                return event.message

    provider = DummyProvider()
    tools = [ToolSpec(name="file_write", description="", parameters={})]
    script = said(
        '!tool file_write {"path": "/a", "content": "A"}\n'
        "!fail one\n!fail two\n!text done"
    )
    (call,) = asyncio.run(reply(provider, [script], tools)).parts
    assert isinstance(call, Call) and call.name == "file_write"
    ran = [
        script,
        Message(id="m2", role="assistant", parts=(call,)),
        Message(
            id="m3",
            role="tool",
            parts=(ToolResult(call_id=call.call_id, name=call.name, content="ok"),),
        ),
    ]
    for expected in ("one", "two"):
        with pytest.raises(ModelHTTPError, match=expected):
            asyncio.run(reply(provider, ran, tools))
    done = asyncio.run(reply(provider, [*ran, said(_notice(1))], tools))
    assert [p.text for p in done.parts] == ["done"]
    assert done.usage is None
    # a request offered no tools: a summary, a title
    fresh = said("!fail one\n!text done")
    quiet = asyncio.run(reply(DummyProvider(), [fresh], []))
    assert [p.text for p in quiet.parts] == ["done"]


# -- agent-defined tasks (@agex.task), on the agex loop -------------------------------

SHOUT = '''
@agex.task
def shout(word: str) -> str:
    """HELPER Say the word in capitals."""

print("loud:", shout("hi"))
'''

SHOUT_HANDLER = b'''
from host import agex

@agex.task(objects=())
def shout(word: str) -> str:
    """HELPER Say the word in capitals."""

def post(req):
    return {"loud": shout(req.json["word"])}
'''


def _runs(code: str) -> ModelStep:
    return calls("run_python", code=code)


def test_an_agex_session_holds_tasks_and_their_skill(studio):
    """``agex``, with ``agex_stand_in`` for tests, and the skill that
    teaches them, which building-apps points to."""
    client, registry, _ = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws
    assert {"agex", "agex_stand_in"} <= set(ws.runtime.python_config.host_objects)
    assert ws.files.fs.exists("/workspace/skills/agent-tasks/SKILL.md")
    apps = ws.files.fs.read("/workspace/skills/building-apps/SKILL.md").decode()
    assert "`agent-tasks`" in apps


def test_a_turns_code_calls_a_task_a_helper_answers(studio):
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    scripts.steps["s1"] = [_runs(SHOUT), says("done")]
    scripts.steps["HELPER"] = [_runs("task.success(word.upper())")]
    events = _chat(client, "s1", "s1: shout")
    end = next(e for e in events if e["type"] == "tool_end")
    assert "loud: HI" in end["result"], end["result"]


def test_a_published_app_calls_a_task(studio):
    """An app built with a task calls it when served, its helper on the
    studio's default model, here holding none of the app's objects."""
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    fs = session.ws.files.fs
    fs.makedirs("/workspace/app/api", exist_ok=True)
    fs.write("/workspace/app/index.html", b"<h1>shout</h1>")
    fs.write("/workspace/app/api/shout.py", SHOUT_HANDLER)
    session.ws.commit()
    pub = client.post("/api/sessions/s1/publish").json()

    scripts.steps["HELPER"] = [_runs("task.success(word.upper())")]
    served = client.post(f"{pub['url']}api/shout", json={"word": "hi"})
    assert served.status_code == 200, served.text
    assert served.json() == {"loud": "HI"}


def test_a_stop_stops_the_task_the_turns_code_waits_on(studio):
    """The turn's code waits in a task call while its helper works for a
    minute; the stop reaches the helper too, so the turn ends."""
    client, registry, scripts = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    scripts.steps["s1"] = [_runs(SHOUT), says("never said")]
    scripts.steps["HELPER"] = [
        _runs(
            "import time\nfor i in range(6000):\n    time.sleep(0.01)\n"
            "task.success('late')"
        )
    ]
    started = client.post("/api/sessions/s1/chat", json={"message": "s1: shout"})
    deadline = time.monotonic() + 10
    while session.running_call is None and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(1.0)  # the helper is at work
    t0 = time.monotonic()
    assert client.post("/api/sessions/s1/cancel", json={}).status_code == 200
    events = _until_done(client, "s1", since=started.json()["since"])
    assert time.monotonic() - t0 < 8  # not the helper's minute
    assert "turn stopped" in [e.get("text") for e in events if e["type"] == "notice"]


def test_tasks_follow_the_setting_and_a_stop_reaches_them_on_agno(
    tmp_path, monkeypatch
):
    """``NONTAINER_STUDIO_LOOP=agex`` decides, not a session's own loop:
    a session whose conversation agno wrote holds ``agex`` too, and the
    studio's stop cancels its task's helper, as agex's cancel does on the
    agex loop."""
    from nontainer_studio.dummy import DummyModel

    scripts = Scripts()

    def studio_on(loop):
        monkeypatch.setenv("NONTAINER_STUDIO_LOOP", loop)
        registry = sessions_mod.Registry(
            model_factory=lambda *a: DummyModel(),
            store=tmp_path,
            default_model="dummy",
            agex_model=scripts.provider,
        )
        registry.retitle = lambda session: None
        return registry

    first = studio_on("agno")
    with TestClient(server.build_app(first)) as client:
        client.post("/api/sessions", json={"name": "s1"})
        _chat(client, "s1", "!text hello")  # a conversation agno wrote
    first.close()

    registry = studio_on("agex")
    try:
        with TestClient(server.build_app(registry)) as client:
            session = registry.open("s1")
            assert session.loop == "agno"
            assert "agex" in session.ws.runtime.python_config.host_objects

            scripts.steps["HELPER"] = [
                _runs(
                    "import time\nfor i in range(6000):\n    time.sleep(0.01)\n"
                    "task.success('late')"
                )
            ]
            message = (
                "!tool run_python " + json.dumps({"code": SHOUT}) + "\n!text never"
            )
            started = client.post("/api/sessions/s1/chat", json={"message": message})
            deadline = time.monotonic() + 10
            while session.running_call is None and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(1.0)  # the helper is at work
            t0 = time.monotonic()
            assert client.post("/api/sessions/s1/cancel", json={}).status_code == 200
            events = _until_done(client, "s1", since=started.json()["since"])
            assert time.monotonic() - t0 < 8  # not the helper's minute
            notices = [e.get("text") for e in events if e["type"] == "notice"]
            assert "turn stopped" in notices
    finally:
        registry.close()


def test_a_session_reads_either_loops_conversation_as_a_trace(tmp_path, monkeypatch):
    """The store reads agno's runs and agex's, so ``ws-git worktree add
    --trace`` mounts a session's conversation whichever loop wrote it."""
    from nontainer_studio.dummy import DummyModel

    scripts = Scripts()
    registry = sessions_mod.Registry(
        model_factory=lambda *a: DummyModel(),
        store=tmp_path,
        default_model="dummy",
        agex_model=scripts.provider,
    )
    registry.retitle = lambda session: None
    try:
        with TestClient(server.build_app(registry)) as client:
            monkeypatch.setenv("NONTAINER_STUDIO_LOOP", "agno")
            client.post("/api/sessions", json={"name": "by-agno"})
            _chat(client, "by-agno", "!text hello from agno")

            monkeypatch.setenv("NONTAINER_STUDIO_LOOP", "agex")
            monkeypatch.setenv("NONTAINER_STUDIO_WSGIT", "1")
            client.post("/api/sessions", json={"name": "by-agex"})
            client.post("/api/sessions/by-agex/upload?name=data.csv", content=b"a\n")
            scripts.steps["by-agex"] = [says("hello from agex")]
            _chat(client, "by-agex", "by-agex: hi")

            client.post("/api/sessions", json={"name": "reader"})
            command = (
                "ws-git worktree add a by-agno --trace"
                " && ws-git worktree add x by-agex --trace"
                " && cat a/.trace/index.md x/.trace/index.md"
                " x/.trace/runs/*/changed.md"
            )
            scripts.steps["reader"] = [calls("terminal", command=command), says("ok")]
            _chat(client, "reader", "reader: read them")
            run = registry.get("reader").driver.session.runs[-1]
            (result,) = run.messages[2].parts
            assert not result.is_error, result.content
            assert "written by agno" in result.content
            assert "hello from agno" in result.content
            assert "written by agex" in result.content
            # the human's upload is the human's in the run's changes
            assert " upload\n\n- A uploads/data.csv" in result.content
    finally:
        registry.close()
