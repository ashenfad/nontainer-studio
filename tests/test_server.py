"""Server plumbing — background turns + event-log transcript, session
lifecycle, preview/publish, time travel — exercised with a fake agent
(no LLM, no key)."""

import asyncio
import json
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from nontainer.apps import render_test_app
from nontainer.apps import request as nt_request
from starlette.testclient import TestClient

from nontainer_studio import server
from nontainer_studio import sessions as sessions_mod


class FakeAgent:
    """Yields a canned agno-shaped event stream."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def arun(self, message: str, stream: bool = True, stream_events: bool = True):
        self.seen.append(message)
        run_id = f"run-{len(self.seen)}"
        tool = SimpleNamespace(
            tool_name="terminal", tool_args={"command": "ls"}, run_id=run_id
        )
        yield SimpleNamespace(event="ToolCallStarted", tool=tool)
        yield SimpleNamespace(
            event="ToolCallCompleted",
            tool=SimpleNamespace(tool_name="terminal", result="a.txt"),
        )
        yield SimpleNamespace(event="RunContent", content="hello ", run_id=run_id)
        yield SimpleNamespace(event="RunContent", content="world")
        yield SimpleNamespace(event="RunCompleted")


class GatedAgent(FakeAgent):
    """A FakeAgent that holds its turn open until a gate is set —
    a delegate that is still running while the test looks at it."""

    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self._gate = gate

    async def arun(self, message: str, stream: bool = True, stream_events: bool = True):
        while not self._gate.is_set():
            await asyncio.sleep(0.01)
        async for event in super().arun(
            message, stream=stream, stream_events=stream_events
        ):
            yield event


@pytest.fixture
def studio(tmp_path):
    """A real Registry over a tmp store, with the agent faked out —
    everything else (workspaces, dbs, forks, publish) is real."""
    registry = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    registry._build_agent = lambda *a, **k: FakeAgent()
    with TestClient(server.build_app(registry)) as client:
        yield client, registry
    registry.close()


def _collect_until_done(client, session: str, since: int = 0) -> list[dict]:
    """Poll the transcript snapshot until a `done` event lands. (The
    SSE follow mode never ends, and TestClient drains responses — so
    tests use the ?wait=0 snapshot; the live follow path is exercised
    against a real uvicorn in the manual smoke.)"""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        data = client.get(f"/api/sessions/{session}/events?since={since}&wait=0")
        events = data.json()["events"]
        if any(e["type"] == "done" for e in events):
            return events
        time.sleep(0.02)
    raise AssertionError(f"no done event within 10s: {events}")


HANDLER = """
def get(req):
    return {"n": cache.get("n", 0)}

def post(req):
    cache["n"] = cache.get("n", 0) + 1
    return {"n": cache["n"]}
"""


def _seed_app(ws):
    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.files.fs.write(
        "/workspace/app/index.html", b"<html><body><h1>counter</h1></body></html>"
    )
    ws.files.fs.write("/workspace/app/api/count.py", HANDLER.encode())
    ws.commit()


# -- chat: background turns + transcript --------------------------------------


def test_turn_runs_in_background_and_transcript_replays(studio):
    client, registry = studio
    assert client.post("/api/sessions", json={"name": "s1"}).status_code == 200

    r = client.post("/api/sessions/s1/chat", json={"message": "build me a thing"})
    assert r.status_code == 200 and r.json()["ok"]

    events = _collect_until_done(client, "s1")
    kinds = [e["type"] for e in events]
    # the poll snapshot sees the COMPACTED transcript: contiguous
    # delta runs merge at turn boundaries (live SSE streams granular)
    assert kinds == ["user", "tool_start", "tool_end", "text", "done"]
    assert events[0]["text"] == "build me a thing"
    # args ride STRUCTURED (the client renders tool calls per-type)
    started = next(e for e in events if e["type"] == "tool_start")
    assert started["args"] == {"command": "ls"}
    assert "".join(e["delta"] for e in events if e["type"] == "text") == "hello world"

    # a SECOND subscriber replays the identical transcript from 0 —
    # this is what makes session switching / reload safe
    replay = _collect_until_done(client, "s1")
    assert [e["type"] for e in replay] == kinds
    # and cursors let a client resume where it left off
    tail = _collect_until_done(client, "s1", since=events[-1]["seq"])
    assert [e["type"] for e in tail] == ["done"]


def test_native_thinking_streams_as_thinking_events(studio):
    """reasoning_content deltas on RunContent (and ReasoningContentDelta
    events) surface as `thinking` transcript events; mixed chunks split
    into thinking + text."""
    client, registry = studio

    class ThinkingAgent(FakeAgent):
        async def arun(self, message, stream=True, stream_events=True):
            self.seen.append(message)
            yield SimpleNamespace(
                event="RunContent", reasoning_content="hmm, ", run_id="run-1"
            )
            yield SimpleNamespace(
                event="ReasoningContentDelta", reasoning_content="let me see"
            )
            yield SimpleNamespace(
                event="RunContent", reasoning_content="… ok", content="the answer"
            )

    registry._build_agent = lambda *a, **k: ThinkingAgent()
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/chat", json={"message": "ponder"})
    events = _collect_until_done(client, "s1")
    kinds = [e["type"] for e in events]
    assert kinds == ["user", "thinking", "text", "done"]  # deltas compacted
    thought = "".join(e["delta"] for e in events if e["type"] == "thinking")
    assert thought == "hmm, let me see… ok"
    assert [e["delta"] for e in events if e["type"] == "text"] == ["the answer"]


def test_artifact_events_harvest_from_tool_result_note(studio):
    """A tool result carrying a `[ui artifacts: ...]` note yields
    first-class `artifact` events, one per pair, positioned right after
    the `tool_end` — the note stays in the result text (model-facing),
    the events are additive and carry the render kind."""
    client, registry = studio

    class ArtifactAgent(FakeAgent):
        async def arun(self, message, stream=True, stream_events=True):
            self.seen.append(message)
            result = (
                "plotted 2 series\n"
                "[ui artifacts: trend -> /workspace/ui/trend.plotly.json, "
                "raw -> /workspace/ui/raw.table.json]"
            )
            yield SimpleNamespace(
                event="ToolCallCompleted",
                tool=SimpleNamespace(tool_name="run_python", result=result),
            )
            yield SimpleNamespace(event="RunContent", content="here you go")

    registry._build_agent = lambda *a, **k: ArtifactAgent()
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/chat", json={"message": "plot it"})
    events = _collect_until_done(client, "s1")

    kinds = [e["type"] for e in events]
    assert kinds == ["user", "tool_end", "artifact", "artifact", "text", "done"]
    arts = [e for e in events if e["type"] == "artifact"]
    assert arts[0] == {
        **arts[0],
        "name": "trend",
        "path": "/workspace/ui/trend.plotly.json",
        "kind": "plotly",
    }
    assert (
        arts[1]["path"] == "/workspace/ui/raw.table.json" and arts[1]["kind"] == "table"
    )
    # the note survives in the model-facing result text
    tool_end = next(e for e in events if e["type"] == "tool_end")
    assert "[ui artifacts:" in tool_end["result"]


def test_artifact_event_survives_a_long_tool_result(studio):
    """The note rides at the tail of the result; parsing must use the
    RAW result, not the 2000-char-capped tool_end text, or a long
    result truncates the note (and the artifact) away."""
    client, registry = studio

    class LongResultAgent(FakeAgent):
        async def arun(self, message, stream=True, stream_events=True):
            self.seen.append(message)
            result = (
                "x" * 5_000 + "\n[ui artifacts: big -> /workspace/ui/big.plotly.json]"
            )
            yield SimpleNamespace(
                event="ToolCallCompleted",
                tool=SimpleNamespace(tool_name="run_python", result=result),
            )

    registry._build_agent = lambda *a, **k: LongResultAgent()
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/chat", json={"message": "go"})
    events = _collect_until_done(client, "s1")

    # the capped tool_end lost the note...
    tool_end = next(e for e in events if e["type"] == "tool_end")
    assert "[ui artifacts:" not in tool_end["result"]
    # ...but the artifact event survived (parsed from the raw result)
    art = next(e for e in events if e["type"] == "artifact")
    assert art["path"] == "/workspace/ui/big.plotly.json" and art["kind"] == "plotly"


def test_tool_result_without_note_emits_no_artifact_events(studio):
    """The common case: an ordinary tool result yields only tool_end."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/chat", json={"message": "list files"})
    events = _collect_until_done(client, "s1")
    assert not any(e["type"] == "artifact" for e in events)


def test_new_sessions_seed_skills(studio):
    """Session creation installs the repo's starter skills into
    /workspace/skills as ordinary versioned files; existing sessions keep their
    own (possibly agent-edited) copies."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    files = client.get("/api/sessions/s1/files").json()["files"]
    assert "/workspace/skills/building-apps/SKILL.md" in files
    assert "/workspace/skills/building-apps/references/app.jsx" in files

    # creation-only: a reseed must not clobber the session's copies
    session = registry.get("s1")
    session.ws.files.write("/workspace/skills/building-apps/SKILL.md", "agent-edited")
    registry.close()
    registry._sessions.clear()
    session2 = registry.open("s1")
    assert (
        session2.ws.files.fs.read("/workspace/skills/building-apps/SKILL.md")
        == b"agent-edited"
    )


def test_the_seeded_skill_teaches_the_verbs_the_session_has(studio):
    """The debugging ladder names terminal verbs, and an agent sent to
    type one that is not there loses the step. `ws-curl` is the verb on
    every rung — plain `curl` is the real one, which reaches the network
    instead of the app — so the ladder teaches that spelling and no
    other, and the same holds for the two unit-test verbs it ends on.
    """
    client, registry = studio
    client.post("/api/sessions", json={"name": "sk"})
    session = registry.get("sk")
    text = session.ws.files.fs.read("/workspace/skills/building-apps/SKILL.md").decode()

    for verb in ("ws-curl", "ws-pytest", "ws-vitest"):
        assert verb in text
        assert verb in session.ws.runtime.commands
    # never the bare spelling as an instruction: `curl api/x` reaches
    # the network on a rung whose shell is real
    assert "`curl api/x`" not in text
    # markers are resolved away, never seeded raw
    assert "<!--if:" not in text and "<!--endif-->" not in text


def test_skill_conditionals_resolve_for_a_command_less_executor():
    """The other side of the gate, on the resolver directly: a dud-like
    executor gets the no-commands variant and is told WHY curl is
    absent (its presence on PATH is the trap)."""
    src = (
        "1. read the log\n"
        "<!--if:commands-->\n"
        "2. `curl api/x` in the terminal\n"
        "<!--endif-->\n"
        "<!--if:no-commands-->\n"
        "2. test_app — no `curl` builtin here\n"
        "<!--endif-->\n"
        "tail line\n"
    )
    with_cmds = sessions_mod.Registry._resolve_skill_text(src, commands=True)
    assert "curl api/x" in with_cmds
    assert "test_app" not in with_cmds

    without = sessions_mod.Registry._resolve_skill_text(src, commands=False)
    assert "curl api/x" not in without
    assert "test_app" in without

    for out in (with_cmds, without):
        assert "<!--if:" not in out and "<!--endif-->" not in out
        assert out.startswith("1. read the log\n")
        assert out.endswith("tail line\n")  # surrounding text survives


def test_compression_and_usage_events_reach_the_transcript(studio):
    """Compaction waves surface as notices (the slow turn explains
    itself); per-call token usage rides a `usage` event for the UI."""
    client, registry = studio

    class CompressingAgent(FakeAgent):
        async def arun(self, message, stream=True, stream_events=True):
            self.seen.append(message)
            yield SimpleNamespace(
                event="ModelRequestCompleted",
                input_tokens=123_456,
                cache_read_tokens=100_000,
                run_id="run-1",
            )
            yield SimpleNamespace(event="CompressionStarted")
            yield SimpleNamespace(
                event="CompressionCompleted",
                tool_results_compressed=7,
                original_size=90_000,
                compressed_size=4_000,
            )
            yield SimpleNamespace(event="RunContent", content="done")

    registry._build_agent = lambda *a, **k: CompressingAgent()
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/chat", json={"message": "go"})
    events = _collect_until_done(client, "s1")
    kinds = [e["type"] for e in events]
    assert kinds == ["user", "usage", "notice", "notice", "text", "done"]
    usage = next(e for e in events if e["type"] == "usage")
    assert usage["input_tokens"] == 123_456 and usage["cached_tokens"] == 100_000
    notices = [e["text"] for e in events if e["type"] == "notice"]
    assert "compressing older tool results" in notices[0]
    assert "7 tool results (90,000 → 4,000 chars)" in notices[1]


def test_chat_missing_session_and_empty_message(studio):
    client, _ = studio
    assert (
        client.post("/api/sessions/nope/chat", json={"message": "x"}).status_code == 404
    )
    client.post("/api/sessions", json={"name": "s1"})
    assert (
        client.post("/api/sessions/s1/chat", json={"message": "  "}).status_code == 400
    )


def test_busy_session_409s_chat(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.turn_lock.acquire()  # simulate a running turn
    try:
        assert (
            client.post("/api/sessions/s1/chat", json={"message": "x"}).status_code
            == 409
        )
        assert client.get("/api/sessions").json()["sessions"] == [
            {
                "name": "s1",
                "title": "New session",
                "busy": True,
                "model": None,
                "delegates": 0,
                "delegate_count": 0,
            }
        ]
    finally:
        session.turn_lock.release()


def test_bad_session_name_400(studio):
    client, _ = studio
    assert client.post("/api/sessions", json={"name": "../evil"}).status_code == 400


def test_index_serves_shell(studio):
    client, _ = studio
    r = client.get("/")
    assert r.status_code == 200 and "nontainer-studio" in r.text


# -- preview + publish ---------------------------------------------------------


def test_preview_dispatches_into_live_runtime(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)

    r = client.get("/preview/s1/")
    assert r.status_code == 200 and "counter" in r.text
    assert client.post("/preview/s1/api/count").json() == {"n": 1}
    assert client.get("/preview/s1/api/count").json() == {"n": 1}
    assert client.get("/preview/nope/").status_code == 404


def test_publish_freezes_a_snapshot(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _seed_app(session.ws)
    client.post("/preview/s1/api/count")  # live state: n=1

    pub = client.post("/api/sessions/s1/publish").json()
    assert pub["url"].startswith("/apps/") and pub["commit"]

    # the snapshot serves, read-only: GET works, VFS/cache mutation 500s
    assert "counter" in client.get(pub["url"]).text
    assert client.post(f"{pub['url']}api/count").status_code == 500
    # and it carries the app tree, not the session: the live `cache` the
    # preview has been writing is not in the published commit, so the
    # handler reads its own default rather than the conversation's state
    assert client.get(f"{pub['url']}api/count").json() == {"n": 0}
    assert client.get("/preview/s1/api/count").json() == {"n": 1}

    # the live session keeps moving; the published version doesn't
    session.ws.files.fs.write("/workspace/app/index.html", b"<h1>moved on</h1>")
    session.ws.commit()
    assert "moved on" in client.get("/preview/s1/").text
    assert "counter" in client.get(pub["url"]).text


NAMES_HANDLER = (
    b"def get(req):\n"
    b'    db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")\n'
    b"    return {'names': [r[0] for r in db.query('SELECT v FROM t')]}\n"
)


def _db_of(registry, name: str) -> str:
    """Where a session's db is: the manifest row says so, and nothing
    else may — a name never decides a path."""
    return registry._manifest()["sessions"][name]["db"]


def _app_db_of(registry, token: str) -> str:
    return registry._manifest()["apps"][token]["db"]


def _seed_db_app(session):
    """An app whose only content is a handler reading the app db."""
    session.ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    session.ws.files.fs.write("/workspace/app/api/names.py", NAMES_HANDLER)
    session.ws.commit()


def test_a_published_app_serves_over_the_sessions_live_db(studio):
    """`db` is a handle to an external store, and publishing is a
    deployment against that store rather than a snapshot of it: the
    app's row names the session's db, so the session's later writes
    are there for its users and its users' writes are there for the
    session."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _seed_db_app(session)
    session.db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    session.db.execute("INSERT INTO t VALUES ('at-publish')")

    pub = client.post("/api/sessions/s1/publish").json()
    assert pub["version"] == "v1" and pub["url"] == f"/apps/{pub['token']}/"
    assert client.get(f"{pub['url']}api/names").json() == {"names": ["at-publish"]}

    session.db.execute("INSERT INTO t VALUES ('after-publish')")
    assert client.get(f"{pub['url']}api/names").json() == {
        "names": ["at-publish", "after-publish"]
    }
    # the app names the session's db; nothing was copied anywhere
    assert _app_db_of(registry, pub["token"]) == _db_of(registry, "s1")
    assert not (registry._store.path / "dbs" / "apps").exists()


def test_a_second_version_serves_over_the_same_db(studio):
    """Versions are code; the store is one. A row written through the
    app between publishes is still there under v2 — and it is in the
    session's db, because that is the file the app serves over."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _seed_db_app(session)
    pub = client.post("/api/sessions/s1/publish").json()
    assert client.get(f"{pub['url']}api/names").json() == {"names": []}

    session.db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    session.db.execute("INSERT INTO t VALUES ('a user typed this')")

    session.ws.files.fs.write("/workspace/app/index.html", b"<h1>v2</h1>")
    session.ws.commit()
    v2 = client.post("/api/sessions/s1/publish", json={}).json()
    assert v2["token"] == pub["token"] and v2["version"] == "v2"

    # same URL, new code, the SAME db
    assert client.get(v2["url"]).text == "<h1>v2</h1>"
    assert client.get(f"{v2['url']}api/names").json() == {
        "names": ["a user typed this"]
    }


# -- apps: versions, the app's db, the registry ---------------------------------


def _store_tags(store) -> dict:
    """Every tag in the kvgit store, as kvgit stores it (scope prefixes
    included) — the only way to see what a publication really left
    behind in the scope that outlives its session."""
    import kvgit

    handle = kvgit.store(kind="disk", path=str(Path(store) / "kvgit"), branch="probe")
    try:
        return handle.tags()
    finally:
        handle.versioned.store.close()


def _registry(store) -> dict:
    """nontainer's own publication registry, read off disk — the table
    the studio's app entries are keyed against."""
    path = Path(store) / "publications.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def _publish(client, session: str, **body) -> dict:
    r = client.post(f"/api/sessions/{session}/publish", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_publish_makes_a_publication_and_marks_the_transcript(scripted, tmp_path):
    """A version is a version of a nontainer publication named for the
    app's token, the app's row names the session's db, and the
    conversation gets a landmark: the marker carries the SESSION commit
    and tree it was made at, so it can be come back to."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    _run(client, "s1", _script("/workspace/app/index.html", "<h1>one</h1>", "built it"))

    pub = _publish(client, "s1")
    token = pub["token"]
    record = _registry(tmp_path)[token]
    assert record["current"] == "v1"
    assert f"@store/{token}/v1" in _store_tags(tmp_path)
    # the studio's half of the table: token -> publication name, route, db
    entry = registry._manifest()["apps"][token]
    assert entry["pub"] == token
    assert entry["db"] == _db_of(registry, "s1")
    assert entry["versions"]["v1"]["ref"] == record["versions"]["v1"]["ref"]

    events = client.get("/api/sessions/s1/events?wait=0").json()["events"]
    marker = next(e for e in events if e["type"] == "publish")
    assert marker["token"] == token
    assert marker["version"] == "v1"
    assert marker["url"] == f"/apps/{token}/"
    assert marker["head"] == pub["commit"] == registry.get("s1").ws.head
    assert marker["tree"] == pub["tree"]


def test_a_version_carries_the_app_and_not_the_session(studio):
    """What a capability URL hands out is the `app/` tree and the rows
    that describe it — not the notes, the uploads, the skills or the
    conversation record sitting elsewhere in the session. A handler
    under a published version can read its whole tree, and an export
    hands over that whole tree, so "the whole tree" had better be the
    app."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.ws.files.write("/workspace/private-notes.md", "do not ship this")
    _seed_app(session.ws)
    pub = _publish(client, "s1")

    served = registry.resolve(pub["token"])
    seen = served.files.list("/workspace", recursive=True)
    assert "/workspace/app/index.html" in seen
    assert not any("private-notes" in p or "/skills/" in p for p in seen)
    # the session still holds all of it — the version is a derived
    # commit, not a narrowing of the branch
    assert "/workspace/private-notes.md" in session.ws.files.list(
        "/workspace", recursive=True
    )


def test_shared_backend_code_lives_under_app(studio, tmp_path):
    """Everything a published app RUNS from lives under `app/`. A
    version is that tree, so a module at the workspace root imports
    fine in the live preview and raises on every request once
    published — the trap this asserts nobody falls into.

    `_`-prefixed files under `app/api/` are where it goes: the api/
    prefix routes only to a bare handler name (no slash, no leading
    underscore) and static serving refuses everything under api/, so
    the module is importable and reachable by nobody, exactly as
    handler source is. Asserted through a published app after a cold
    restart, and asserted of the SKILL too, because what the agent is
    told is the half that decides where the module ends up.
    """
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    fs = session.ws.files.fs
    fs.makedirs("/workspace/app/api", exist_ok=True)
    fs.makedirs("/workspace/helpers", exist_ok=True)
    fs.write("/workspace/helpers/outside.py", b"def fn():\n    return 'outside'\n")
    fs.write("/workspace/app/api/_shared.py", b"def fn():\n    return 'shared'\n")
    fs.write(
        "/workspace/app/api/under.py",
        b"def get(req):\n    from app.api._shared import fn\n    return {'v': fn()}\n",
    )
    fs.write(
        "/workspace/app/api/outside.py",
        b"def get(req):\n    from helpers.outside import fn\n    return {'v': fn()}\n",
    )
    fs.write("/workspace/app/index.html", b"<h1>hi</h1>")
    session.ws.commit()
    # against the live workspace both import, which is why the wrong
    # one is so easy to write
    assert client.get("/preview/s1/api/under").json() == {"v": "shared"}
    assert client.get("/preview/s1/api/outside").json() == {"v": "outside"}
    pub = _publish(client, "s1")

    # cold: a fresh registry over the same store, nothing cached
    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    try:
        with TestClient(server.build_app(reborn)) as client2:
            assert client2.get(f"{pub['url']}api/under").json() == {"v": "shared"}
            assert client2.get(f"{pub['url']}api/outside").status_code == 500
            # ...and the module is not a download
            assert client2.get(f"{pub['url']}api/_shared.py").status_code == 404
            assert client2.get(f"{pub['url']}api/_shared").status_code == 404
    finally:
        reborn.close()

    skill = (
        Path(__file__).parent.parent / "skills" / "building-apps" / "SKILL.md"
    ).read_text()
    assert "app/api/_shared.py" in skill
    assert "from app.api._shared import fn" in skill
    assert "/helpers/<mod>.py" not in skill


def test_a_served_version_is_frozen_code_over_a_live_db(studio):
    """The split the whole model rests on. A handler's write to the
    workspace never lands and is refused loudly — the cache and the
    filesystem alike, under process isolation too, where the worker
    refuses a write to a read-only filesystem at open rather than
    losing it at a garbage-collected close. The app db is the other
    half: writes there work, which is the point of the app owning one.
    """
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    session.ws.files.fs.write(
        "/workspace/app/api/note.py",
        b"def post(req):\n"
        b"    open('/workspace/app/scribble.txt', 'w').write('nope')\n"
        b"    return {'ok': True}\n",
    )
    session.ws.files.fs.write(
        "/workspace/app/api/counter.py",
        b"def post(req):\n    cache['n'] = 1\n    return {'ok': True}\n",
    )
    session.ws.files.fs.write(
        "/workspace/app/api/tally.py",
        NAMES_HANDLER + b"\n\n"
        b"def post(req):\n"
        b'    db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")\n'
        b"    db.execute(\"INSERT INTO t VALUES ('from the app')\")\n"
        b"    return {'ok': True}\n",
    )
    session.ws.commit()
    pub = _publish(client, "s1")

    assert client.post(f"{pub['url']}api/counter").status_code == 500
    assert client.post(f"{pub['url']}api/note").status_code == 500
    assert client.get(f"{pub['url']}scribble.txt").status_code == 404
    snapshot = registry.resolve(pub["token"])
    assert snapshot.frozen
    assert not snapshot.files.fs.exists("/workspace/app/scribble.txt")

    assert client.post(f"{pub['url']}api/tally").json() == {"ok": True}
    assert client.get(f"{pub['url']}api/tally").json() == {"names": ["from the app"]}
    # and it landed in the store the session holds: the app's write is
    # a write to the live db, which is the point of one
    assert session.db.query("SELECT v FROM t") == [("from the app",)]


def test_a_published_app_is_readable_from_a_sandboxed_iframe(studio):
    """The preview iframe has no allow-same-origin, so it is an opaque
    origin and everything a published app fetches from it — the jsx
    loader's app.jsx, the app's own api calls — is cross-origin. Static
    files, handler responses and preflights all have to say so, or the
    app works in a tab and fails in the pane."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _seed_app(session.ws)
    session.ws.files.fs.write("/workspace/app/app.jsx", b"export default 1\n")
    session.ws.commit()
    pub = _publish(client, "s1")

    for path in ("", "app.jsx", "api/count"):
        r = client.get(f"{pub['url']}{path}")
        assert r.status_code == 200, path
        assert r.headers["access-control-allow-origin"] == "*", path

    # a JSON POST from app code preflights first
    pre = client.options(
        f"{pub['url']}api/count",
        headers={
            "origin": "null",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == "*"
    assert "POST" in pre.headers["access-control-allow-methods"]
    assert pre.headers["access-control-allow-headers"] == "content-type"
    # and the answer to the real request carries the header too
    assert (
        client.post(f"{pub['url']}api/count").headers["access-control-allow-origin"]
        == "*"
    )
    # an unknown token is still a 404, with the header (the iframe has
    # to be able to READ that answer to report it)
    missing = client.get("/apps/not-a-real-token/")
    assert missing.status_code == 404
    assert missing.headers["access-control-allow-origin"] == "*"


def test_make_current_repoints_the_url(studio, tmp_path):
    """The URL belongs to the app. Rolling back is a pointer move — no
    republish, no new token, the same link in someone's inbox.

    Two pointers have to agree on where it lands: the manifest row the
    studio serves from, and nontainer's own registry, which decides
    which version `unpublish` refuses to drop."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.ws.files.fs.makedirs("/workspace/app", exist_ok=True)
    session.ws.files.fs.write("/workspace/app/index.html", b"<h1>one</h1>")
    session.ws.commit()
    pub = _publish(client, "s1")
    session.ws.files.fs.write("/workspace/app/index.html", b"<h1>two</h1>")
    session.ws.commit()
    v2 = _publish(client, "s1")
    assert client.get(pub["url"]).text == "<h1>two</h1>"
    assert _registry(tmp_path)[pub["token"]]["current"] == "v2"

    app = client.post(
        f"/api/apps/{pub['token']}/current", json={"version": "v1"}
    ).json()
    assert app["current"] == "v1"
    assert client.get(pub["url"]).text == "<h1>one</h1>"
    assert _registry(tmp_path)[pub["token"]]["current"] == "v1"
    # and forward again
    client.post(f"/api/apps/{pub['token']}/current", json={"version": "v2"})
    assert client.get(v2["url"]).text == "<h1>two</h1>"
    assert (
        client.post(
            f"/api/apps/{pub['token']}/current", json={"version": "v9"}
        ).status_code
        == 400
    )


def test_unpublish_removes_the_publication_and_manifest(studio, tmp_path):
    """Taking the app down drops its row, and with it the app's claim
    on the db. The FILE is not unpublish's to remove — the session
    that made it is still writing there — and no verb but the sweep
    ever removes one."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _seed_app(session.ws)
    session.db.execute("CREATE TABLE t (v TEXT)")
    pub = _publish(client, "s1")
    _publish(client, "s1", name="release")
    token = pub["token"]
    assert client.get(pub["url"]).status_code == 200

    assert client.delete(f"/api/apps/{token}").json() == {"ok": True}

    assert client.get(pub["url"]).status_code == 404
    # both versions go, the current one last, and the record with them
    assert token not in _registry(tmp_path)
    assert not any(t.startswith(f"@store/{token}/") for t in _store_tags(tmp_path))
    assert client.get("/api/apps").json()["apps"] == []
    assert client.delete(f"/api/apps/{token}").status_code == 404
    # the session's store is untouched, and still named by its row
    assert (tmp_path / _db_of(registry, "s1")).exists()
    session.db.execute("INSERT INTO t VALUES ('still here')")
    assert session.db.query("SELECT v FROM t") == [("still here",)]


def test_delete_version_refuses_the_current_and_the_last(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)
    pub = _publish(client, "s1")
    token = pub["token"]
    # the only version, and the current one
    assert client.delete(f"/api/apps/{token}/versions/v1").status_code == 400

    _publish(client, "s1")  # v2, now current
    assert client.delete(f"/api/apps/{token}/versions/v2").status_code == 400
    app = client.delete(f"/api/apps/{token}/versions/v1").json()
    assert [v["name"] for v in app["versions"]] == ["v2"]
    assert client.get(pub["url"]).status_code == 200
    assert client.delete(f"/api/apps/{token}/versions/v1").status_code == 400


def _origin_of(client, token: str, version: str) -> str | None:
    """The origin tag the app's row records for one version."""
    app = next(a for a in client.get("/api/apps").json()["apps"] if a["token"] == token)
    return next(v["origin"] for v in app["versions"] if v["name"] == version)


def _drop_origin(registry, token: str, version: str) -> None:
    """Rewrite a version's row without its origin field — the shape of
    a row written before publishing named the commit."""
    manifest = registry._manifest()
    manifest["apps"][token]["versions"][version].pop("origin", None)
    registry._save_manifest(manifest)


def test_publishing_names_the_origin_commit(studio, tmp_path):
    """A version's `app/` tree is the app; the ORIGIN is the whole
    session at that publish. The studio names that commit store-scoped
    so it has a token an agent can spell, and so the history behind it
    is pinned for as long as the version is."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)

    pub = _publish(client, "s1")
    token = pub["token"]
    assert pub["origin"] == f"{token}/v1/origin"
    assert _origin_of(client, token, "v1") == pub["origin"]
    # the name is on the commit the row records, not on the derived one
    assert _store_tags(tmp_path)[f"@store/{pub['origin']}"] == pub["commit"]

    # and a second version gets one of its own
    registry.get("s1").ws.files.fs.write("/workspace/app/index.html", b"<h1>two</h1>")
    registry.get("s1").ws.commit()
    v2 = _publish(client, "s1")
    assert v2["origin"] == f"{token}/v2/origin" and v2["commit"] != pub["commit"]
    assert _store_tags(tmp_path)[f"@store/{v2['origin']}"] == v2["commit"]


def test_deleting_a_version_releases_its_origin_tag(studio, tmp_path):
    """The tag is a GC root over the origin session's history. The
    version it was kept for going is what releases it — nontainer's
    unpublish removes only what its own publish wrote."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)
    pub = _publish(client, "s1")
    v2 = _publish(client, "s1")
    token = pub["token"]

    assert client.delete(f"/api/apps/{token}/versions/v1").status_code == 200
    tags = _store_tags(tmp_path)
    assert f"@store/{pub['origin']}" not in tags
    assert f"@store/{v2['origin']}" in tags  # the version that stayed kept its


def test_unpublishing_releases_every_origin_tag(studio, tmp_path):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)
    pub = _publish(client, "s1")
    v2 = _publish(client, "s1")

    assert client.delete(f"/api/apps/{pub['token']}").json() == {"ok": True}
    tags = _store_tags(tmp_path)
    assert f"@store/{pub['origin']}" not in tags
    assert f"@store/{v2['origin']}" not in tags


def test_a_version_row_with_no_origin_is_read_and_deleted_as_it_is(studio, tmp_path):
    """A version whose commit was never named has no origin, and every
    reader says so rather than reconstructing one: the app lists with
    none, and removing it drops the row and looks for no tag."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)
    pub = _publish(client, "s1")
    v2 = _publish(client, "s1")
    token = pub["token"]
    _drop_origin(registry, token, "v1")

    assert _origin_of(client, token, "v1") is None
    assert client.delete(f"/api/apps/{token}/versions/v1").status_code == 200
    assert [
        v["name"] for v in client.get("/api/apps").json()["apps"][0]["versions"]
    ] == ["v2"]
    # the row went; the name nothing recorded is still on the store,
    # which is what makes it safe to leave rows like this alone
    assert f"@store/{v2['origin']}" in _store_tags(tmp_path)


def test_deleting_the_origin_session_leaves_the_app_served(studio, tmp_path):
    """The publication promise: a store-scoped tag outlives the branch
    that made it, and the app's row still names the db — so the URL
    keeps working, and keeps WRITING, after the conversation behind it
    is gone."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _seed_db_app(session)
    session.ws.files.fs.write(
        "/workspace/app/api/add.py",
        b"def post(req):\n"
        b'    db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")\n'
        b"    db.execute(\"INSERT INTO t VALUES ('after the session went')\")\n"
        b"    return {'ok': True}\n",
    )
    session.ws.commit()
    session.db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    session.db.execute("INSERT INTO t VALUES ('published')")
    pub = _publish(client, "s1")

    assert client.delete("/api/sessions/s1").json() == {"ok": True}
    assert client.get("/api/sessions").json()["sessions"] == []

    # cold path too: nothing cached, no session to reopen
    registry._published.clear()
    assert client.post(f"{pub['url']}api/add").json() == {"ok": True}
    assert client.get(f"{pub['url']}api/names").json() == {
        "names": ["published", "after the session went"]
    }
    assert client.get("/api/apps").json()["apps"][0]["session"] == "s1"


def test_app_selection_and_version_names(studio):
    """Publishing again extends the session's most recent app; `app:
    "new"` starts another; a name may be given, and must be a name a
    tag can carry and one this app doesn't already hold.

    The name rules are the store's — it raises the kind of error a
    caller's mistake gets, so the refusal reaches the client as a 400
    and not as a fault."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)

    first = _publish(client, "s1")
    again = _publish(client, "s1")
    assert again["token"] == first["token"] and again["version"] == "v2"

    named = _publish(client, "s1", name="release-1")
    assert named["token"] == first["token"] and named["version"] == "release-1"

    other = _publish(client, "s1", app="new")
    assert other["token"] != first["token"] and other["version"] == "v1"
    # "most recent" is the app last published to, not the oldest lineage
    assert _publish(client, "s1")["token"] == other["token"]
    # ...and naming one keeps working. The default name counts the
    # app's versions rather than its vN's, so a named version pushes
    # the next number along rather than being overwritten by it.
    assert _publish(client, "s1", app=first["token"])["version"] == "v4"

    for bad in ("", "a/b", "a%b", "release-1"):
        r = client.post(
            "/api/sessions/s1/publish", json={"name": bad, "app": first["token"]}
        )
        assert r.status_code == 400, bad
    assert (
        client.post("/api/sessions/s1/publish", json={"app": "not-a-token"}).status_code
        == 404
    )


def test_a_version_whose_record_fails_is_taken_back_down(studio, tmp_path):
    """A published version with no manifest row is a URL nothing can
    reach and a branch nothing will ever collect. Taking it back down
    is a plain removal and never a pointer move: a version after the
    first lands with `current=False` and the URL moves to it only once
    the row naming it is on disk, so the version being removed is one
    nothing was pointing at."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _seed_app(session.ws)
    pub = _publish(client, "s1")
    token = pub["token"]

    def boom(*a, **k):
        raise RuntimeError("the record could not be written")

    moves = []
    original = sessions_mod._head_tree
    set_current = registry._store.set_current
    registry._store.set_current = lambda *a: moves.append(a) or set_current(*a)
    sessions_mod._head_tree = boom
    try:
        session.ws.files.write("/workspace/app/index.html", "<h1>v2</h1>")
        with pytest.raises(RuntimeError):
            registry.publish("s1")
    finally:
        sessions_mod._head_tree = original
        registry._store.set_current = set_current

    assert moves == []  # nothing to move back, so nothing moved
    record = _registry(tmp_path)[token]
    assert sorted(record["versions"]) == ["v1"] and record["current"] == "v1"
    app = client.get("/api/apps").json()["apps"][0]
    assert [v["name"] for v in app["versions"]] == ["v1"]
    assert client.get(pub["url"]).status_code == 200
    # and the name is free again, because nothing of it was left behind
    assert _publish(client, "s1")["version"] == "v2"


def _publish_v2_leaving_the_pointer_behind(client, registry, session) -> dict:
    """Two versions, with the store's pointer stuck on the first: what
    a `set_current` that raised after the manifest row landed leaves
    behind. Returns the app, as the publish route says it."""
    session.ws.files.fs.makedirs("/workspace/app", exist_ok=True)
    session.ws.files.fs.write("/workspace/app/index.html", b"<h1>one</h1>")
    session.ws.commit()
    pub = _publish(client, session.name)
    session.ws.files.fs.write("/workspace/app/index.html", b"<h1>two</h1>")
    session.ws.commit()

    def refuse(*a):
        raise RuntimeError("the pointer would not move")

    original = registry._store.set_current
    registry._store.set_current = refuse
    try:
        assert _publish(client, session.name)["version"] == "v2"
    finally:
        registry._store.set_current = original
    return pub


def test_a_publish_outlives_a_pointer_the_store_would_not_move(studio, tmp_path):
    """Which version an app serves is the MANIFEST's answer, and the
    store's pointer follows it.

    The row lands first and the store is pointed at it after, so a
    `set_current` that raises — or a machine that goes away in the
    window — leaves a version that is published, recorded and served
    while the store still names the one before it. Failing the request
    there would report a publish that happened as one that did not,
    and a retry would collide with the version already recorded. So it
    is logged, and the next registry to open the store reconciles the
    pointer with the manifest."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    pub = _publish_v2_leaving_the_pointer_behind(client, registry, session)
    token = pub["token"]

    # the publish stands: recorded, and serving off the new version
    app = client.get("/api/apps").json()["apps"][0]
    assert app["current"] == "v2"
    assert [v["name"] for v in app["versions"]] == ["v1", "v2"]
    assert client.get(pub["url"]).text == "<h1>two</h1>"
    assert _registry(tmp_path)[token]["current"] == "v1"  # the store is behind

    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    try:
        assert _registry(tmp_path)[token]["current"] == "v2"
        # ...which is what lets v1 go: the store refuses to drop the
        # version it points at while others remain
        assert [v["name"] for v in reborn.delete_version(token, "v1")["versions"]] == [
            "v2"
        ]
        assert sorted(_registry(tmp_path)[token]["versions"]) == ["v2"]
    finally:
        reborn.close()


def test_taking_an_app_down_reconciles_the_pointer_first(studio, tmp_path):
    """`unpublish` removes an app's versions in an order the MANIFEST
    decides — the served one last, since the store refuses to drop the
    version it points at while others remain. A pointer left behind an
    earlier publish would make that order the wrong one and strand the
    version it names, so the pointer is reconciled before the first
    removal rather than at the next restart."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    token = _publish_v2_leaving_the_pointer_behind(client, registry, session)["token"]
    assert _registry(tmp_path)[token]["current"] == "v1"

    assert client.delete(f"/api/apps/{token}").json() == {"ok": True}
    # nothing of the app is left on the store, in either version
    assert token not in _registry(tmp_path)
    assert not any(t.startswith(f"@store/{token}/") for t in _store_tags(tmp_path))


def test_a_manifest_naming_a_version_the_store_lost_is_left_alone(
    studio, tmp_path, caplog
):
    """Reconciling moves the store's pointer to the version the
    manifest names. When the store does not hold that version at all,
    the manifest is the half that is wrong and no pointer move fixes
    it, so it is logged and left: a row a human can still read beats a
    guess written over it."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)
    pub = _publish(client, "s1")
    token = pub["token"]
    manifest = registry._manifest()
    manifest["apps"][token]["current"] = "v9"
    registry._save_manifest(manifest)

    with caplog.at_level("WARNING", logger="nontainer_studio.sessions"):
        reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
        reborn._build_agent = lambda *a, **k: FakeAgent()
        try:
            assert reborn._manifest()["apps"][token]["current"] == "v9"
            assert _registry(tmp_path)[token]["current"] == "v1"
        finally:
            reborn.close()
    assert any("v9" in m for m in caplog.messages)


def test_publishing_a_session_with_no_app_is_refused(studio):
    """A version IS the `app/` tree, so a session that has not built one
    has no version to make — and the refusal is the caller's to fix,
    not a server fault."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    registry.get("s1").ws.files.write("/workspace/notes.md", "thinking about it")

    r = client.post("/api/sessions/s1/publish")
    assert r.status_code == 400
    assert "no files under 'app/'" in r.json()["error"]
    assert client.get("/api/apps").json()["apps"] == []


def test_publish_refuses_a_turn_in_flight(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _seed_app(session.ws)
    session.turn_lock.acquire()
    try:
        assert client.post("/api/sessions/s1/publish").status_code == 409
    finally:
        session.turn_lock.release()


def test_the_marker_lands_under_the_same_reservation_as_the_tag(scripted):
    """The publish route holds ONE reservation across the tag and the
    marker. A chat request that won the turn lock in between would put
    its `user` event above a landmark whose commit predates it — and
    restoring to that marker would then rewind the files out from under
    a prompt still on screen. So a chat racing a publish is refused,
    and the marker is always the last event of the two."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    _run(client, "s1", _script("/workspace/app/index.html", "one", "built it"))
    session = registry.get("s1")

    # the reservation is real: a chat arriving mid-publish is refused
    assert session.turn_lock.acquire(blocking=False)
    try:
        assert (
            client.post(
                "/api/sessions/s1/chat", json={"message": "!text hi"}
            ).status_code
            == 409
        )
    finally:
        session.turn_lock.release()

    _publish(client, "s1")
    _run(client, "s1", "!text and now this")
    events = client.get("/api/sessions/s1/events?wait=0").json()["events"]
    marker = next(e for e in events if e["type"] == "publish")
    users = [e["seq"] for e in events if e["type"] == "user"]
    # the marker sits between the turn it names and the one after it
    assert [u < marker["seq"] for u in users] == [True, False]
    assert marker["head"] == next(
        e["head"] for e in events if e["type"] == "done" and e["seq"] < marker["seq"]
    )


def test_publishing_into_another_sessions_app_is_refused(studio):
    """An app belongs to the session that created it. Extending someone
    else's would tag a version from this branch while the entry keeps
    naming theirs — a version nothing downstream could reason about."""
    client, registry = studio
    for name in ("s1", "s2"):
        client.post("/api/sessions", json={"name": name})
        _seed_app(registry.get(name).ws)
    theirs = _publish(client, "s1")

    r = client.post("/api/sessions/s2/publish", json={"app": theirs["token"]})
    assert r.status_code == 403
    assert "belongs to session 's1'" in r.json()["error"]
    # ...and nothing was tagged or recorded on the way to refusing
    app = next(
        a
        for a in client.get("/api/apps").json()["apps"]
        if a["token"] == theirs["token"]
    )
    assert [v["name"] for v in app["versions"]] == ["v1"]
    assert app["session"] == "s1"


def test_changed_since_answers_content_and_writes_apart(studio):
    """Two questions, two answers. Editing an app file changes the
    content; editing a note outside <root>/app leaves the app identical
    but still moves the tree — kvgit stamps every write with its own
    time, so `up_to_date` is the write question and `count` the content
    one."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _seed_app(session.ws)
    pub = _publish(client, "s1")

    def status() -> dict:
        apps = client.get("/api/sessions/s1/apps").json()["apps"]
        assert [a["token"] for a in apps] == [pub["token"]]
        return apps[0]["changed_since"]

    assert status() == {"count": 0, "paths": [], "up_to_date": True}

    session.ws.files.write("/workspace/notes.md", "not an app file")
    fresh = status()
    assert fresh["count"] == 0 and fresh["paths"] == []
    assert fresh["up_to_date"] is False  # written to, same app

    session.ws.files.write("/workspace/app/index.html", "<h1>changed</h1>")
    changed = status()
    assert changed["count"] == 1
    assert changed["paths"] == ["/workspace/app/index.html"]
    assert changed["up_to_date"] is False

    # publishing again closes both gaps
    _publish(client, "s1")
    assert status() == {"count": 0, "paths": [], "up_to_date": True}


def test_apps_registry_lists_every_app(studio):
    client, registry = studio
    for name in ("s1", "s2"):
        client.post("/api/sessions", json={"name": name})
        _seed_app(registry.get(name).ws)
    client.post("/api/sessions/s1/title", json={"title": "Dashboard"})
    a = _publish(client, "s1")
    b = _publish(client, "s2")

    apps = client.get("/api/apps").json()["apps"]
    assert {row["token"] for row in apps} == {a["token"], b["token"]}
    mine = next(row for row in apps if row["token"] == a["token"])
    assert mine["session"] == "s1"
    assert mine["title"] == "Dashboard"
    assert mine["current"] == "v1"
    assert mine["url"] == a["url"]
    assert [v["name"] for v in mine["versions"]] == ["v1"]
    # the per-session view filters, and adds the live comparison
    rows = client.get("/api/sessions/s2/apps").json()["apps"]
    assert [row["token"] for row in rows] == [b["token"]]
    assert "changed_since" in rows[0]


def test_old_shape_publications_migrate_on_load(studio, tmp_path, caplog):
    """The anchor-branch shape: a token naming a forked branch, served
    over the session's live db. On load each becomes an app with a v1
    naming that same db, and the anchor branch goes; an entry whose
    branch is gone is dropped rather than left 404ing."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _seed_db_app(session)
    session.db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    session.db.execute("INSERT INTO t VALUES ('shared')")
    branch = "s1-pub-deadbeef"
    snapshot = session.ws.fork(branch)
    checkpoint = snapshot.head
    snapshot.close()
    manifest = registry._manifest()
    manifest["published"] = {
        "live-token": {"branch": branch, "session": "s1", "checkpoint": checkpoint},
        "dead-token": {"branch": "s1-pub-gone", "session": "s1", "checkpoint": "beef"},
    }
    registry._save_manifest(manifest)

    with caplog.at_level("INFO", logger="nontainer_studio.sessions"):
        reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    try:
        with TestClient(server.build_app(reborn)) as client2:
            apps = client2.get("/api/apps").json()["apps"]
            assert [row["token"] for row in apps] == ["live-token"]
            assert apps[0]["current"] == "v1"
            # the same live db it was served over before, named
            # rather than copied
            r = client2.get("/apps/live-token/api/names")
            assert r.json() == {"names": ["shared"]}
            assert _app_db_of(reborn, "live-token") == _db_of(reborn, "s1")
        assert reborn._manifest()["published"] == {}
        assert _registry(tmp_path)["live-token"]["current"] == "v1"
    finally:
        reborn.close()
    text = "\n".join(caplog.messages)
    assert "migrated live-token" in text and "dropped dead-token" in text


def test_a_manifest_in_the_old_shape_is_filled_in_and_serves(studio, tmp_path):
    """Where a db is used to be its session's NAME, so an old install
    holds `dbs/<name>.sqlite` for every session and a copy at
    `dbs/apps/<token>.sqlite` for every app. Opening the registry
    writes those paths down as rows — nothing moves, no copy is
    merged — and after that one fill there is a single shape: every
    row names its file, and a name never decides a path again."""
    client, registry = studio
    for name in ("s1", "s1.helper"):
        client.post("/api/sessions", json={"name": name})
    session = registry.get("s1")
    _seed_db_app(session)
    session.db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    session.db.execute("INSERT INTO t VALUES ('in the copy')")
    token = _publish(client, "s1")["token"]

    # rewrite the install into the shape that predates db ids: files
    # named for their sessions, an app holding a copy of its own, and
    # a manifest that records none of it
    manifest = registry._manifest()
    for name in ("s1", "s1.helper"):
        shutil.copyfile(
            tmp_path / _db_of(registry, name), tmp_path / "dbs" / f"{name}.sqlite"
        )
    copy = tmp_path / "dbs" / "apps" / f"{token}.sqlite"
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tmp_path / _db_of(registry, "s1"), copy)
    for name in ("s1", "s1.helper"):
        (tmp_path / _db_of(registry, name)).unlink()
    manifest["sessions"] = ["s1", "s1.helper"]  # the bare-list shape
    manifest["apps"][token]["db"] = f"dbs/apps/{token}.sqlite"
    manifest["delegates"]["s1.helper"] = "s1"
    registry._save_manifest(manifest)
    registry.close()

    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    try:
        rows = reborn._manifest()["sessions"]
        assert rows["s1"]["db"] == "dbs/s1.sqlite"
        assert rows["s1.helper"]["db"] == "dbs/s1.helper.sqlite"
        assert _app_db_of(reborn, token) == f"dbs/apps/{token}.sqlite"

        with TestClient(server.build_app(reborn)) as client2:
            url = f"/apps/{token}/"
            assert client2.get(f"{url}api/names").json() == {"names": ["in the copy"]}
            # the copy is the app's own db, and the session's is its
            # own: a copy already made is a fact, and merging its rows
            # into anything is not the studio's to do
            live = reborn.open("s1")
            live.db.execute("INSERT INTO t VALUES ('after the fill')")
            assert client2.get(f"{url}api/names").json() == {"names": ["in the copy"]}
            assert live.db.query("SELECT v FROM t") == [
                ("in the copy",),
                ("after the fill",),
            ]

            # deletes as before: the rows go, the files stay for the
            # sweep, and the app's copy is nobody's to take
            assert client2.delete("/api/sessions/s1").json() == {"ok": True}
            assert client2.get("/api/sessions").json()["sessions"] == []
            assert client2.get(f"{url}api/names").json() == {"names": ["in the copy"]}
    finally:
        reborn.close()


def _tagged_app(registry, session, token, versions, current=None) -> dict:
    """The layout a version had before publications, built by hand: one
    store-scoped `pub/<token>/<version>` tag per version, a manifest row
    naming it, and the app's db copied beside the session's."""
    rows = {}
    for i, (version, html) in enumerate(versions):
        session.ws.files.write("/workspace/app/index.html", html)
        session.ws.commit()
        tag = f"pub/{token}/{version}"
        commit = registry._store.tags.add(
            session.ws,
            tag,
            info={
                "token": token,
                "session": session.name,
                "version": version,
                "title": "Old app",
            },
        )
        rows[version] = {
            "tag": tag,
            "commit": commit,
            "tree": sessions_mod._head_tree(session.ws),
            "created": 100.0 + i,
        }
    db = registry._store.path / "dbs" / "apps" / f"{token}.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(registry._store.path / _db_of(registry, session.name), db)
    manifest = registry._manifest()
    entry = {
        "token": token,
        "session": session.name,
        "title": "Old app",
        "created": 100.0,
        "db": f"dbs/apps/{token}.sqlite",
        "current": current or versions[-1][0],
        "versions": rows,
    }
    manifest["apps"][token] = entry
    registry._save_manifest(manifest)
    return entry


def test_tagged_versions_migrate_to_publications(studio, tmp_path, caplog):
    """The one-way migration. Each recorded tag is published again as a
    version of a publication named for the token — same names, same
    order, same pointer — and the tag goes. A version whose origin
    session was deleted comes across too: the tag is still there, and
    that is what the state is re-derived from."""
    client, registry = studio
    for name in ("s1", "s2"):
        client.post("/api/sessions", json={"name": name})
    live = _tagged_app(
        registry,
        registry.get("s1"),
        "live-app",
        [("v1", "<h1>one</h1>"), ("v2", "<h1>two</h1>")],
        current="v1",  # the URL was rolled back
    )
    _tagged_app(registry, registry.get("s2"), "orphan-app", [("v1", "<h1>alone</h1>")])
    assert client.delete("/api/sessions/s2").json() == {"ok": True}

    with caplog.at_level("INFO", logger="nontainer_studio.sessions"):
        reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    try:
        record = _registry(tmp_path)
        assert sorted(record["live-app"]["versions"]) == ["v1", "v2"]
        assert record["live-app"]["current"] == "v1"  # the pointer came across
        assert sorted(record["orphan-app"]["versions"]) == ["v1"]
        # the old tags are gone, and the rows name publications now
        tags = _store_tags(tmp_path)
        assert not any(t.startswith("@store/pub/live-app/") for t in tags)
        assert not any(t.startswith("@store/pub/orphan-app/") for t in tags)
        entry = reborn._manifest()["apps"]["live-app"]
        assert entry["pub"] == "live-app"
        assert all("tag" not in v and v["ref"] for v in entry["versions"].values())
        # what the SESSION commit was is kept: it is what a marker
        # restores to and what changed_since measures against
        assert {v: r["commit"] for v, r in entry["versions"].items()} == {
            v: r["commit"] for v, r in live["versions"].items()
        }

        with TestClient(server.build_app(reborn)) as client2:
            assert "<h1>one</h1>" in client2.get("/apps/live-app/").text
            assert "<h1>alone</h1>" in client2.get("/apps/orphan-app/").text
            # and the pointer still moves
            client2.post("/api/apps/live-app/current", json={"version": "v2"})
            assert "<h1>two</h1>" in client2.get("/apps/live-app/").text
    finally:
        reborn.close()
    assert "migrated live-app" in "\n".join(caplog.messages)


def test_the_migration_resumes_after_a_crash(studio, tmp_path):
    """The window between a version landing on the store and the row
    naming it landing in the manifest. A crash in there must be
    recoverable, so the legacy tag — the only thing that could
    re-derive the version — outlives the manifest write rather than
    going with the publish, and the retry ADOPTS the version already
    published under that name instead of colliding with it."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _tagged_app(
        registry,
        registry.get("s1"),
        "crash-app",
        [("v1", "<h1>one</h1>"), ("v2", "<h1>two</h1>")],
        current="v1",
    )

    original = sessions_mod.Registry._save_manifest

    def die_once(self, manifest):
        app = manifest["apps"].get("crash-app") or {}
        if app.get("pub"):  # the row that names the publication
            raise RuntimeError("the machine went away mid-migration")
        return original(self, manifest)

    sessions_mod.Registry._save_manifest = die_once
    try:
        with pytest.raises(RuntimeError):
            sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    finally:
        sessions_mod.Registry._save_manifest = original

    # the versions are on the store and the manifest never heard: the
    # tags have to still be there, or nothing can finish the job
    assert sorted(_registry(tmp_path)["crash-app"]["versions"]) == ["v1", "v2"]
    crashed = _store_tags(tmp_path)
    assert "@store/pub/crash-app/v1" in crashed
    assert "@store/pub/crash-app/v2" in crashed

    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    try:
        entry = reborn._manifest()["apps"]["crash-app"]
        record = _registry(tmp_path)["crash-app"]
        # once each, not twice, and pointing where it pointed
        assert sorted(entry["versions"]) == sorted(record["versions"]) == ["v1", "v2"]
        assert entry["current"] == record["current"] == "v1"
        # and only NOW do the tags go
        assert not any(
            t.startswith("@store/pub/crash-app/") for t in _store_tags(tmp_path)
        )
        with TestClient(server.build_app(reborn)) as client2:
            assert "<h1>one</h1>" in client2.get("/apps/crash-app/").text
    finally:
        reborn.close()


def test_the_migration_runs_once(studio, tmp_path):
    """Versions never move, so a second open has nothing to do: the
    refs it finds are the refs the first one wrote, and no version is
    published twice."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _tagged_app(registry, registry.get("s1"), "once-app", [("v1", "<h1>one</h1>")])

    first = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    refs = first._manifest()["apps"]["once-app"]["versions"]
    first.close()
    second = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    try:
        assert second._manifest()["apps"]["once-app"]["versions"] == refs
        assert sorted(_registry(tmp_path)["once-app"]["versions"]) == ["v1"]
    finally:
        second.close()


def test_restore_to_a_publish_rewinds_files_and_conversation(scripted):
    """A publish marker is an anchor like a user message is: restoring
    to one puts the files AND the agent's memory back where that version
    was tagged, and cuts the transcript after the marker — the marker
    itself survives, because the version it names still does."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    _run(client, "s1", _script("/workspace/app/index.html", "one", "made one"))
    pub = _publish(client, "s1")
    events = client.get("/api/sessions/s1/events?wait=0").json()["events"]
    marker = next(e for e in events if e["type"] == "publish")
    _run(client, "s1", _script("/workspace/app/index.html", "two", "made two"))
    session = registry.get("s1")
    assert session.ws.files.fs.read("/workspace/app/index.html") == b"two"
    assert len(_run_ids(registry, "s1")) == 2

    r = client.post("/api/sessions/s1/restore", json={"seq": marker["seq"]})
    assert r.status_code == 200, r.text

    assert session.ws.files.fs.read("/workspace/app/index.html") == b"one"
    assert len(_run_ids(registry, "s1")) == 1  # the second turn was unsaid
    after = client.get("/api/sessions/s1/events?wait=0").json()["events"]
    cut = next(e for e in after if e["type"] == "truncate")
    assert cut["to"] == marker["seq"] + 1
    visible = [e for _, e in sessions_mod.Registry._visible(session.events)]
    assert visible[-1]["type"] == "publish"  # the marker outlives its own rewind
    # the publication itself is untouched: a rewind unsays turns, not tags
    assert client.get(pub["url"]).status_code == 200


def test_restore_validations(scripted):
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    _run(client, "s1", "!text hi")
    events = client.get("/api/sessions/s1/events?wait=0").json()["events"]
    user_seq = next(e["seq"] for e in events if e["type"] == "user")
    assert client.post("/api/sessions/s1/restore", json={}).status_code == 400
    assert (
        client.post("/api/sessions/s1/restore", json={"seq": user_seq}).status_code
        == 400
    )


def test_branch_from_a_version_opens_where_it_was_published(scripted):
    """Fork the origin session and rewind the child to the version's
    commit: the child opens with the files and the conversation as they
    stood at that publish, with its own universe from there."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    _run(client, "s1", _script("/workspace/app/index.html", "one", "made one"))
    pub = _publish(client, "s1")
    _run(client, "s1", _script("/workspace/app/index.html", "two", "made two"))

    # the origin is READ, not moved — staged work and all
    origin = registry.get("s1")
    head = origin.ws.head
    origin.ws.files.fs.write("/workspace/scratch.txt", b"mid-thought")
    assert origin.ws.uncommitted

    r = client.post(f"/api/apps/{pub['token']}/versions/v1/branch")
    assert r.status_code == 200, r.text
    child = registry.get(r.json()["name"])
    assert child.ws.files.fs.read("/workspace/app/index.html") == b"one"
    assert len(_run_ids(registry, child.name)) == 1
    assert not child.ws.files.fs.exists("/workspace/scratch.txt")
    # nothing about the origin moved: not its head, not its staged work
    assert origin.ws.head == head
    assert origin.ws.uncommitted
    assert origin.ws.files.fs.read("/workspace/app/index.html") == b"two"
    origin.ws.discard()  # so the delete below isn't testing a dirty branch

    client.delete("/api/sessions/s1")
    gone = client.post(f"/api/apps/{pub['token']}/versions/v1/branch")
    assert gone.status_code == 404
    assert "still served" in gone.json()["error"]


def test_branching_survives_an_edit_that_hid_the_marker(scripted):
    """The child's transcript is PROJECTED at the publish, not cut after
    it. Editing back past a publish leaves a truncate in the log whose
    cut precedes the marker; appending another cut cannot undo it, so a
    copied log would leave the child showing a transcript that stops
    before the state it actually holds."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    _run(client, "s1", _script("/workspace/a.txt", "one", "made one"))
    _run(client, "s1", _script("/workspace/app/index.html", "app", "made the app"))
    pub = _publish(client, "s1")
    _run(client, "s1", _script("/workspace/c.txt", "three", "made three"))
    # the edit rewinds to the SECOND prompt, cutting away the publish
    session = registry.get("s1")
    second = _user_seqs(session)[1]
    _edit(client, "s1", second, _script("/workspace/b.txt", "two", "made two instead"))
    visible = [e for _, e in sessions_mod.Registry._visible(session.events)]
    assert not any(e["type"] == "publish" for e in visible)  # hidden in the origin

    r = client.post(f"/api/apps/{pub['token']}/versions/v1/branch")
    assert r.status_code == 200, r.text
    child = registry.get(r.json()["name"])
    shown = [e for _, e in sessions_mod.Registry._visible(child.events)]
    assert shown[-1]["type"] == "publish"  # ends AT the publish
    # the two turns before it survived; the one after it never came, and
    # neither did the edit's replacement — both happened after the fork
    assert sum(1 for e in shown if e["type"] == "user") == 2
    assert not any("instead" in (e.get("text") or "") for e in shown)
    assert child.ws.files.fs.read("/workspace/a.txt") == b"one"
    assert child.ws.files.fs.read("/workspace/app/index.html") == b"app"
    assert not child.ws.files.fs.exists("/workspace/b.txt")
    assert not child.ws.files.fs.exists("/workspace/c.txt")


# -- files ----------------------------------------------------------------------


def test_files_tree_and_raw(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws
    ws.files.fs.makedirs("/workspace/app/screenshots", exist_ok=True)
    ws.files.fs.write("/workspace/notes.md", b"# hi")
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
        "7753de0000000c49444154089963f8cfc000000301010018dd8db0000000"
        "0049454e44ae426082"
    )
    ws.files.fs.write("/workspace/app/screenshots/shot-1.png", png)

    files = client.get("/api/sessions/s1/files").json()["files"]
    assert (
        "/workspace/notes.md" in files
        and "/workspace/app/screenshots/shot-1.png" in files
    )

    r = client.get("/api/sessions/s1/file", params={"path": "/workspace/notes.md"})
    assert r.status_code == 200 and r.text == "# hi"

    r = client.get(
        "/api/sessions/s1/file",
        params={"path": "/workspace/app/screenshots/shot-1.png"},
    )
    assert r.status_code == 200 and r.content == png
    assert r.headers["content-type"] == "image/png"

    assert (
        client.get("/api/sessions/s1/file", params={"path": "/nope"}).status_code == 404
    )


# -- upload ---------------------------------------------------------------------


def test_upload_lands_committed_with_notice(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")

    r = client.post("/api/sessions/s1/upload?name=data.csv", content=b"a,b\n1,2\n")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "path": "/workspace/uploads/data.csv", "size": 8}
    assert session.ws.files.fs.read("/workspace/uploads/data.csv") == b"a,b\n1,2\n"
    # committed: an edit's rewind extends to uploads
    assert any(c.info.get("tool") == "file_write" for c in session.ws.log(limit=3))
    # transcript notice
    assert any(
        e["type"] == "notice" and "/workspace/uploads/data.csv" in e["text"]
        for e in session.events
    )

    # basename-only: traversal-ish names collapse to a safe filename
    r = client.post("/api/sessions/s1/upload?name=../../etc/passwd", content=b"x")
    assert r.json()["path"] == "/workspace/uploads/passwd"

    assert client.post("/api/sessions/s1/upload", content=b"x").status_code == 400
    assert (
        client.post("/api/sessions/nope/upload?name=x", content=b"x").status_code == 404
    )


def test_upload_multi_file_parallel(studio):
    """N parallel uploads — the workspace lock serializes the writes;
    every file lands and every write minted its own commit."""
    from concurrent.futures import ThreadPoolExecutor

    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    before = len(list(session.ws.log()))

    def up(i: int):
        return client.post(
            f"/api/sessions/s1/upload?name=f{i}.txt", content=f"file {i}".encode()
        ).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(up, range(6)))
    assert codes == [200] * 6
    for i in range(6):
        assert (
            session.ws.files.fs.read(f"/workspace/uploads/f{i}.txt")
            == f"file {i}".encode()
        )
    assert len(list(session.ws.log())) == before + 6  # one commit per file


def test_upload_size_cap(studio):
    client, _ = studio
    client.post("/api/sessions", json={"name": "s1"})
    r = client.post(
        "/api/sessions/s1/upload?name=big.bin",
        content=b"x",
        headers={"content-length": "999999999"},
    )
    assert r.status_code == 413


# -- data stack -------------------------------------------------------------------


def test_data_stack_granted_when_installed(studio):
    pytest.importorskip("pandas")
    pytest.importorskip("matplotlib")
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    cfg = registry.get("s1").ws.runtime.python_config
    names = {
        getattr(g, "module", None).__name__ for group in cfg.modules for g in group
    }
    assert {"numpy", "pandas", "matplotlib"} <= names


# -- turn lifecycle ---------------------------------------------------------------


def test_turn_task_is_referenced_and_done_precedes_next_user(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/chat", json={"message": "one"})
    session = registry.get("s1")
    assert session.turn_task is not None  # strong ref: no GC'd turns
    _collect_until_done(client, "s1")
    client.post("/api/sessions/s1/chat", json={"message": "two"})
    events = _collect_until_done(client, "s1", since=0)
    kinds = [e["type"] for e in events]
    # every `user` is preceded by a completed turn: done before release
    first_done = kinds.index("done")
    second_user = kinds.index("user", 1)
    assert first_done < second_user


def test_event_cap_never_swallows_done(studio):
    import asyncio

    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.events.extend({"type": "text", "delta": "x"} for _ in range(20_000))

    asyncio.run(session.emit({"type": "text", "delta": "dropped"}))
    asyncio.run(session.emit({"type": "done"}))
    kinds = [e["type"] for e in session.events[-2:]]
    assert "done" in kinds  # control events bypass the cap
    assert not any(e.get("delta") == "dropped" for e in session.events)


def test_preview_answers_cors_preflight(studio):
    """A JSON POST from app code is a non-simple request: the opaque-
    origin iframe preflights with OPTIONS first. No preflight answer =
    the browser blocks the real request regardless of its headers."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)
    r = client.options(
        "/preview/s1/api/count",
        headers={
            "origin": "null",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "POST" in r.headers["access-control-allow-methods"]
    assert r.headers["access-control-allow-headers"] == "content-type"
    # and the preflighted request itself still dispatches
    r = client.post(
        "/preview/s1/api/count", headers={"content-type": "application/json"}
    )
    assert r.status_code == 200


def test_preview_sends_cors_for_sandboxed_iframe(studio):
    """The preview iframe is an opaque origin (sandbox without
    allow-same-origin), so the app's own fetches need CORS — and the
    iframe must NOT be able to reach the studio API (no such header
    there)."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)
    r = client.get("/preview/s1/api/count")
    assert r.headers["access-control-allow-origin"] == "*"
    r = client.get("/api/sessions")
    assert "access-control-allow-origin" not in r.headers


def test_session_manifest_survives_restart(studio, tmp_path):
    """The rail should list sessions from prior server runs — the
    workspaces persist, so the listing must too (lazily openable)."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})

    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    assert reborn.list() == [
        {
            "name": "s1",
            "title": "New session",
            "busy": False,
            "model": None,
            "delegates": 0,
            "delegate_count": 0,
        }
    ]
    # and it opens lazily with its files intact
    registry.get("s1").ws.files.write("keep.txt", "here")
    session = reborn.open("s1")
    assert session.ws.files.fs.read("keep.txt") == b"here"
    reborn.close()


def test_events_since_must_be_int(studio):
    client, _ = studio
    client.post("/api/sessions", json={"name": "s1"})
    r = client.get("/api/sessions/s1/events?since=banana&wait=0")
    assert r.status_code == 400


def test_transcript_survives_restart(studio, tmp_path):
    """The event log is durable jsonl: a reborn registry reloads the
    transcript, and the same cursor feed serves history and live —
    no special casing for either."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/chat", json={"message": "hello"})
    events = _collect_until_done(client, "s1")

    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    session = reborn.open("s1")
    assert [e["type"] for e in session.events] == [e["type"] for e in events]
    assert session.events[0]["type"] == "user"
    assert session.events[0]["text"] == "hello"
    assert session.events[0]["head"]  # the undo anchor rides the user event
    reborn.close()


def test_transcript_compacts_deltas_and_survives_reload(studio):
    """Delta granularity is a wire concern: at each non-delta boundary
    contiguous text/thinking runs merge into single events (seq of the
    run's first chunk), the jsonl carries only the compacted form, and
    a reload reconstructs the identical transcript. Reasoning turns
    used to burn thousands of log entries; undone timelines then hit
    the old lifetime cap ('event log full') — gone with the window."""
    import asyncio
    import json as _json

    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")

    async def turn():
        await session.emit({"type": "user", "text": "go", "head": "h1"})
        for chunk in ("thi", "nk ", "hard"):
            await session.emit({"type": "thinking", "delta": chunk})
        for chunk in ("hello ", "world"):
            await session.emit({"type": "text", "delta": chunk})
        await session.emit({"type": "done", "run_id": "r1", "head": "h1"})

    asyncio.run(turn())

    kinds = [(e["type"], e["seq"]) for e in session.events]
    # 7 emitted -> 4 stored; merged events keep their run's FIRST seq
    assert kinds == [("user", 0), ("thinking", 1), ("text", 4), ("done", 6)]
    assert session.events[1]["delta"] == "think hard"
    assert session.events[2]["delta"] == "hello world"
    assert session.next_seq == 7

    # the jsonl holds exactly the compacted form
    lines = [_json.loads(x) for x in session.log_path.read_text().splitlines()]
    assert [e["seq"] for e in lines] == [0, 1, 4, 6]

    # legacy (pre-seq, granular) logs collapse on load with positional seqs
    legacy = session.log_path.with_name("legacy.jsonl")
    legacy.write_text(
        "\n".join(
            _json.dumps(e)
            for e in [
                {"type": "user", "text": "hi"},
                {"type": "text", "delta": "a"},
                {"type": "text", "delta": "b"},
                {"type": "done"},
            ]
        )
        + "\n"
    )
    loaded = sessions_mod.Registry._load_events(legacy)
    assert [(e["type"], e["seq"]) for e in loaded] == [
        ("user", 0),
        ("text", 1),
        ("done", 3),
    ]
    assert loaded[1]["delta"] == "ab"


def test_memory_window_drops_head_but_seqs_stay_monotonic(studio, monkeypatch):
    """The in-memory list is a tail WINDOW, not a lifetime cap: old
    (flushed) events fall off, seqs keep counting, and a poller asking
    from an ancient seq just gets the surviving tail."""
    import asyncio

    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    monkeypatch.setattr(sessions_mod, "MAX_EVENTS", 4)

    async def spam():
        for i in range(10):
            await session.emit({"type": "notice", "text": f"n{i}"})

    asyncio.run(spam())
    assert len(session.events) == 4
    assert [e["seq"] for e in session.events] == [6, 7, 8, 9]
    assert session.next_seq == 10
    data = client.get("/api/sessions/s1/events?since=0&wait=0").json()
    assert [e["seq"] for e in data["events"]] == [6, 7, 8, 9]
    assert data["next"] == 10


def test_event_log_tolerates_torn_lines(studio, tmp_path):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    log = registry.get("s1").log_path
    log.write_text('{"type": "user", "text": "ok"}\n{"type": "trunc')  # crash mid-write
    assert sessions_mod.Registry._load_events(log) == [
        {"type": "user", "text": "ok", "seq": 0}
    ]


# -- synchronized restore ---------------------------------------------------------


class FakeChatDb:
    """The two agno db methods the rewind touches."""

    def __init__(self) -> None:
        self.record = None

    def get_session(self, session_id, session_type=None, **kw):
        return self.record

    def upsert_session(self, record, **kw):
        self.record = record


def _turn(client, session: str, message: str) -> None:
    # From the cursor the POST hands back, not from 0: a second turn
    # polled from 0 sees the FIRST turn's `done` and returns while the
    # new one is still running.
    started = client.post(f"/api/sessions/{session}/chat", json={"message": message})
    _collect_until_done(client, session, since=started.json()["since"])


# -- edit: rewind + retry as one verb ---------------------------------------------


def _user_seqs(session) -> list[int]:
    return [e["seq"] for e in session.events if e["type"] == "user"]


@pytest.fixture
def scripted(tmp_path):
    """A registry over the REAL stack: agno's run loop, WorkspaceTools,
    the workspace and the store db all execute — only the LLM is faked,
    by directives riding in the user message (see dummy.py). The chat
    lives in the branch now, so nothing short of a real agent can show
    that a rewind takes the files and the memory together."""
    from nontainer_studio.dummy import DummyModel

    registry = sessions_mod.Registry(
        model_factory=lambda spec=None: DummyModel(), store=tmp_path
    )
    with TestClient(server.build_app(registry)) as client:
        yield client, registry
    registry.close()


def _script(path: str, content: str, reply: str) -> str:
    """One scripted turn: write a file, then say so."""
    return f"!tool file_write {json.dumps({'path': path, 'content': content})}\n!text {reply}"


def _run(client, session: str, message: str) -> list[dict]:
    """Run a turn and wait for ITS done, not a previous turn's."""
    r = client.post(f"/api/sessions/{session}/chat", json={"message": message})
    assert r.status_code == 200, r.text
    return _collect_until_done(client, session, since=r.json()["since"])


def _edit(client, session: str, seq: int, message: str):
    r = client.post(
        f"/api/sessions/{session}/edit", json={"seq": seq, "message": message}
    )
    if r.status_code == 200:
        _collect_until_done(client, session, since=r.json()["since"])
    return r


def _run_ids(registry, name: str) -> list[str]:
    """The agent's memory, as the branch holds it."""
    record = registry.db.get_session(name)
    return [run.run_id for run in (record.runs or [])] if record is not None else []


def test_edit_rewinds_files_and_memory_in_one_restore(scripted):
    """Editing an earlier prompt restores the workspace to that turn's
    pre-turn head — and the conversation lives in the same branch, so
    that ONE call unwrites the turn's file and unsays its run. The
    transcript cut is an appended `truncate` event; the log stays
    append-only."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")

    _run(client, "s1", _script("/workspace/a.txt", "A", "wrote a"))
    _run(client, "s1", _script("/workspace/b.txt", "B", "wrote b"))
    before = _run_ids(registry, "s1")
    assert len(before) == 2

    seq = _user_seqs(session)[1]
    assert (
        _edit(
            client, "s1", seq, _script("/workspace/c.txt", "C", "wrote c")
        ).status_code
        == 200
    )

    assert session.ws.files.fs.read("/workspace/a.txt") == b"A"
    assert not session.ws.files.fs.exists("/workspace/b.txt")
    assert session.ws.files.fs.read("/workspace/c.txt") == b"C"

    after = _run_ids(registry, "s1")
    assert after[0] == before[0]  # the kept turn is the same run
    assert before[1] not in after  # the rewound one is gone
    assert len(after) == 2  # and the rerun landed in its place

    kinds = [e["type"] for e in session.events]
    cut = kinds.index("truncate")
    assert session.events[cut]["to"] == seq
    assert kinds[cut + 1] == "user"


def test_edit_the_first_message_leaves_nothing_behind(scripted):
    """Nothing precedes the first turn, so its pre-turn head is the
    empty conversation over the seeded workspace."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _run(client, "s1", _script("/workspace/a.txt", "A", "wrote a"))
    before = _run_ids(registry, "s1")

    seq = _user_seqs(session)[0]
    assert (
        _edit(
            client, "s1", seq, _script("/workspace/z.txt", "Z", "wrote z")
        ).status_code
        == 200
    )

    assert not session.ws.files.fs.exists("/workspace/a.txt")
    assert session.ws.files.fs.read("/workspace/z.txt") == b"Z"
    after = _run_ids(registry, "s1")
    assert len(after) == 1 and after != before


def test_a_second_edit_rewinds_to_its_own_anchor(scripted):
    """Every user event carries the head its own turn began from, so
    editing a REPLACEMENT prompt rewinds to where that turn started —
    not to the state of the turn it replaced."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")

    _run(client, "s1", _script("/workspace/a.txt", "A", "wrote a"))
    _run(client, "s1", _script("/workspace/b.txt", "B", "wrote b"))
    kept = _run_ids(registry, "s1")[0]

    first_cut = _user_seqs(session)[1]
    assert (
        _edit(
            client, "s1", first_cut, _script("/workspace/v2.txt", "2", "wrote v2")
        ).status_code
        == 200
    )
    second_cut = _user_seqs(session)[-1]
    assert second_cut > first_cut
    assert (
        _edit(
            client, "s1", second_cut, _script("/workspace/v3.txt", "3", "wrote v3")
        ).status_code
        == 200
    )

    assert session.ws.files.fs.read("/workspace/a.txt") == b"A"
    for gone in ("b.txt", "v2.txt"):
        assert not session.ws.files.fs.exists(f"/workspace/{gone}")
    assert session.ws.files.fs.read("/workspace/v3.txt") == b"3"
    after = _run_ids(registry, "s1")
    assert len(after) == 2 and after[0] == kept


def test_a_repaired_run_persists_through_the_store_db(scripted):
    """``repair_aborted_run`` rewrites a stored run in place (agno's
    history builder skips error/cancelled runs, so the turn's real work
    would vanish from memory). Over the branch db that is a re-upsert of
    a run the branch already holds — allowed, and committed."""
    from agno.db.base import SessionType
    from agno.run.base import RunStatus

    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _run(client, "s1", _script("/workspace/a.txt", "A", "wrote a"))

    record = registry.db.get_session(session_id="s1", session_type=SessionType.AGENT)
    run_id = record.runs[-1].run_id
    record.runs[-1].status = RunStatus.error
    registry.db.upsert_session(record)

    sessions_mod.repair_aborted_run(session, run_id, "credit balance too low")

    stored = registry.db.get_session(session_id="s1", session_type=SessionType.AGENT)
    repaired = stored.runs[-1]
    assert repaired.run_id == run_id
    assert repaired.status == RunStatus.completed
    assert "credit balance too low" in repaired.messages[-1].content


def test_the_conversation_survives_a_restart(scripted, tmp_path):
    """No handoff: the conversation is in the branch, so a registry
    rebuilt over the same store reads it back with the files."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    _run(client, "s1", _script("/workspace/a.txt", "A", "wrote a"))
    ids = _run_ids(registry, "s1")
    assert ids
    registry.close()

    from nontainer_studio.dummy import DummyModel

    reborn = sessions_mod.Registry(
        model_factory=lambda spec=None: DummyModel(), store=tmp_path
    )
    assert _run_ids(reborn, "s1") == ids
    assert reborn.open("s1").ws.files.fs.read("/workspace/a.txt") == b"A"
    reborn.close()


def test_fork_inherits_files_conversation_and_the_parents_db(scripted):
    """One kvgit operation carries files, cache, cwd and the agent's
    memory; the transcript is copied so the human reads what the agent
    remembers; the app db is NAMED, not copied, because it is a handle
    to an external store and a branch does not clone production."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    parent = registry.get("s1")
    _run(client, "s1", _script("/workspace/a.txt", "A", "wrote a"))
    parent.db.execute("CREATE TABLE IF NOT EXISTS notes (t TEXT)")
    parent.db.execute("INSERT INTO notes VALUES (?)", ("parent",))

    r = client.post("/api/sessions/s1/fork", json={"conversation": "inherit"})
    assert r.status_code == 200
    name = r.json()["name"]
    assert name != "s1"
    child = registry.get(name)

    assert child.ws.files.fs.read("/workspace/a.txt") == b"A"
    assert _run_ids(registry, name) == _run_ids(registry, "s1")
    assert registry.db.get_session(name).session_id == name
    assert [e["type"] for e in child.events] == [e["type"] for e in parent.events]

    assert child.db is parent.db  # one handle per file, so writes queue
    assert child.db.query("SELECT t FROM notes") == [("parent",)]
    child.db.execute("INSERT INTO notes VALUES (?)", ("child",))
    assert parent.db.query("SELECT t FROM notes") == [("parent",), ("child",)]
    # the child's row names the parent's file: no copy, and no path
    # invented out of either name
    assert _db_of(registry, name) == _db_of(registry, "s1")

    # the parent kept its own universe, whole
    assert parent.ws.files.fs.read("/workspace/a.txt") == b"A"
    assert registry.db.get_session("s1").session_id == "s1"
    assert {row["name"] for row in registry.list()} == {"s1", name}


def test_a_fork_of_a_fork_keeps_naming_the_root_db(scripted):
    """A db id is minted once, at the root of the lineage: a chain of
    forks names one file and not a chain of copies."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    root = registry.get("s1")
    root.db.execute("CREATE TABLE IF NOT EXISTS notes (t TEXT)")

    child = registry.get(client.post("/api/sessions/s1/fork", json={}).json()["name"])
    grandchild = registry.get(
        client.post(f"/api/sessions/{child.name}/fork", json={}).json()["name"]
    )

    grandchild.db.execute("INSERT INTO notes VALUES (?)", ("grandchild",))
    assert root.db.query("SELECT t FROM notes") == [("grandchild",)]
    rel = _db_of(registry, "s1")
    assert _db_of(registry, child.name) == rel
    assert _db_of(registry, grandchild.name) == rel


def test_a_new_session_starts_with_a_clean_db_under_an_id_of_its_own(studio, tmp_path):
    """Only a fork names somebody else's store. A session opened by
    name mints an id for its db — an id, not the name: a slug is
    reused the moment its session is deleted, and a path that is a
    slug would hand the next holder a dead session's rows."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    registry.get("s1").db.execute("CREATE TABLE t (v TEXT)")

    client.post("/api/sessions", json={"name": "s2"})
    other = registry.get("s2")
    assert other.db.query("SELECT name FROM sqlite_master WHERE name='t'") == []

    rel = _db_of(registry, "s2")
    assert re.fullmatch(r"dbs/[0-9a-f]{16}\.sqlite", rel), rel
    assert rel != _db_of(registry, "s1")
    assert (tmp_path / rel).exists()
    assert not (tmp_path / "dbs" / "s2.sqlite").exists()


def test_a_reborn_name_starts_empty_beside_the_fork_that_kept_the_store(
    scripted, tmp_path
):
    """The whole reason a db has an id. The origin is deleted while its
    fork still names the file, so the file stays and the slug goes
    free — and the next session to draw that slug mints an id of its
    own, reads nothing, and cannot write into what the fork is still
    reading."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    origin = registry.get("s1")
    origin.db.execute("CREATE TABLE notes (t TEXT)")
    origin.db.execute("INSERT INTO notes VALUES (?)", ("the fork's rows",))
    fork = registry.get(client.post("/api/sessions/s1/fork", json={}).json()["name"])
    shared = _db_of(registry, "s1")

    assert client.delete("/api/sessions/s1").json() == {"ok": True}
    assert (tmp_path / shared).exists()  # the fork still names it

    client.post("/api/sessions", json={"name": "s1"})
    reborn = registry.get("s1")
    assert _db_of(registry, "s1") != shared
    assert reborn.db is not fork.db
    assert reborn.db.query("SELECT name FROM sqlite_master WHERE name='notes'") == []

    reborn.db.execute("CREATE TABLE notes (t TEXT)")
    reborn.db.execute("INSERT INTO notes VALUES (?)", ("mine",))
    assert fork.db.query("SELECT t FROM notes") == [("the fork's rows",)]


def test_fork_fresh_keeps_the_files_and_drops_the_chat(scripted):
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    _run(client, "s1", _script("/workspace/a.txt", "A", "wrote a"))

    r = client.post("/api/sessions/s1/fork", json={"conversation": "fresh"})
    assert r.status_code == 200
    child = registry.get(r.json()["name"])
    assert child.ws.files.fs.read("/workspace/a.txt") == b"A"
    assert _run_ids(registry, child.name) == []
    assert child.events == []
    # and the fork is a working session, not a husk
    _run(client, child.name, _script("/workspace/b.txt", "B", "wrote b"))
    assert len(_run_ids(registry, child.name)) == 1


def test_fork_refuses_a_turn_in_flight_a_dirty_workspace_and_a_bad_mode(scripted):
    """kvgit refuses to fork a branch with staged changes, and a fork of
    half a turn would be a state no commit ever held."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")

    session.ws.files.fs.write("/workspace/staged.txt", b"mid-turn")
    assert session.ws.uncommitted
    assert client.post("/api/sessions/s1/fork", json={}).status_code == 409
    session.ws.commit()

    session.turn_lock.acquire()
    try:
        assert client.post("/api/sessions/s1/fork", json={}).status_code == 409
    finally:
        session.turn_lock.release()

    r = client.post("/api/sessions/s1/fork", json={"conversation": "sideways"})
    assert r.status_code == 400
    # nothing was minted by either refusal
    assert [row["name"] for row in registry.list()] == ["s1"]


def test_edit_validations(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _turn(client, "s1", "one")
    seq = _user_seqs(session)[0]

    post = lambda body: client.post("/api/sessions/s1/edit", json=body)  # noqa: E731
    assert post({"seq": seq, "message": "  "}).status_code == 400
    assert post({"seq": "0", "message": "x"}).status_code == 400
    assert post({"seq": True, "message": "x"}).status_code == 400
    assert post({"seq": len(session.events) + 5, "message": "x"}).status_code == 400
    assert post({"seq": seq + 1, "message": "x"}).status_code == 400  # not a user event

    session.turn_lock.acquire()  # simulate a running turn
    try:
        assert post({"seq": seq, "message": "x"}).status_code == 409
    finally:
        session.turn_lock.release()
    # failed edits never leak the lock
    assert not session.busy


# -- a2ui egress: turn-level A2UI v0.9 projection ---------------------------------


class PlotlyArtifactAgent(FakeAgent):
    """A turn with prose plus a tool result naming a plotly artifact."""

    async def arun(self, message, stream=True, stream_events=True):
        self.seen.append(message)
        result = "plotted it\n[ui artifacts: fig -> /workspace/ui/fig.plotly.json]"
        yield SimpleNamespace(
            event="ToolCallCompleted",
            tool=SimpleNamespace(tool_name="run_python", result=result),
        )
        yield SimpleNamespace(event="RunContent", content="here you go")


class SilentAgent(FakeAgent):
    """A turn that produces no prose and no artifacts — just a tool call."""

    async def arun(self, message, stream=True, stream_events=True):
        self.seen.append(message)
        yield SimpleNamespace(
            event="ToolCallCompleted",
            tool=SimpleNamespace(tool_name="terminal", result="ok"),
        )


def test_a2ui_projects_a_turn_into_a_v0_9_surface(studio):
    """One turn (prose + a plotly artifact) → createSurface,
    updateComponents, updateDataModel — surface id from the done seq, prose
    a Text component, the chart bound into the data model, cursor on every
    message."""
    import json as _json

    client, registry = studio
    registry._build_agent = lambda *a, **k: PlotlyArtifactAgent()
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    # real bytes in the workspace so read_bytes finds the spec (the
    # projection reads the file to build the Chart + data model)
    spec = {"data": [{"x": [1], "y": [2]}], "layout": {"title": "hi"}}
    session.ws.files.fs.write(
        "/workspace/ui/fig.plotly.json", _json.dumps(spec).encode()
    )

    client.post("/api/sessions/s1/chat", json={"message": "plot it"})
    events = _collect_until_done(client, "s1")
    done_seq = next(e["seq"] for e in events if e["type"] == "done")

    data = client.get("/api/sessions/s1/a2ui?wait=0").json()
    messages = data["messages"]
    verbs = [next(k for k in m if k not in ("version", "cursor")) for m in messages]
    assert verbs == ["createSurface", "updateComponents", "updateDataModel"]

    surface_id = f"s1-turn-{done_seq}"
    assert messages[0]["createSurface"]["surfaceId"] == surface_id
    # the driving event's cursor rides every message (snapshot too)
    assert all(m["cursor"] == done_seq for m in messages)
    assert all(m["version"] == "v0.9" for m in messages)

    comps = messages[1]["updateComponents"]["components"]
    text = next(c for c in comps if c.get("component") == "Text")
    assert text["text"] == "here you go"
    chart = next(c for c in comps if c.get("component") == "Chart")
    assert chart["spec"] == {"path": "/artifacts/fig/spec"}

    dm = messages[2]["updateDataModel"]
    assert dm["surfaceId"] == surface_id and dm["value"] == spec
    assert data["next"] == session.next_seq


def test_a2ui_empty_turn_emits_nothing(studio):
    """A turn with no prose and no artifacts renders no surface."""
    client, registry = studio
    registry._build_agent = lambda *a, **k: SilentAgent()
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/chat", json={"message": "quiet"})
    _collect_until_done(client, "s1")
    data = client.get("/api/sessions/s1/a2ui?wait=0").json()
    assert data["messages"] == []


def test_a2ui_edit_voids_the_rewound_surface(studio):
    """An edit's `truncate` deletes every surface at-or-after the cut via
    the v0.9-native deleteSurface (cursor = the truncate seq), while the
    replacement turn gets a fresh surface."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")

    _turn(client, "s1", "one")  # default FakeAgent: prose-only surface
    before = client.get("/api/sessions/s1/a2ui?wait=0").json()["messages"]
    created = [m for m in before if "createSurface" in m]
    assert len(created) == 1
    first_surface = created[0]["createSurface"]["surfaceId"]

    seq = _user_seqs(session)[0]
    r = client.post("/api/sessions/s1/edit", json={"seq": seq, "message": "one v2"})
    assert r.status_code == 200
    _collect_until_done(client, "s1", since=r.json()["since"] - 1)

    after = client.get("/api/sessions/s1/a2ui?wait=0").json()["messages"]
    # the rewound surface is deleted, cursor = the truncate event's seq
    trunc_seq = next(e["seq"] for e in session.events if e["type"] == "truncate")
    deletes = [m for m in after if "deleteSurface" in m]
    assert any(
        d["deleteSurface"]["surfaceId"] == first_surface and d["cursor"] == trunc_seq
        for d in deletes
    )
    # both the original and the replacement surface show in the projection
    surfaces = [m["createSurface"]["surfaceId"] for m in after if "createSurface" in m]
    assert first_surface in surfaces and len(surfaces) == 2


def test_a2ui_since_resumes_like_the_native_feed(studio):
    """?since= filters the snapshot to messages at-or-after the cursor, so a
    consumer resumes without re-receiving turns it already has."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _turn(client, "s1", "one")
    first = client.get("/api/sessions/s1/a2ui?wait=0").json()
    first_cursor = first["messages"][0]["cursor"]
    _turn(client, "s1", "two")

    # resume just past the first turn: only the second turn's messages
    resumed = client.get(
        f"/api/sessions/s1/a2ui?wait=0&since={first_cursor + 1}"
    ).json()["messages"]
    assert resumed and all(m["cursor"] > first_cursor for m in resumed)
    assert client.get("/api/sessions/s1/a2ui?since=banana&wait=0").status_code == 400


def test_unmatched_api_gets_cors_teaching_404(studio):
    """An app in the preview iframe using absolute urls escapes its
    /preview/{name}/ prefix and lands on the studio origin — without
    CORS headers the sandboxed (opaque-origin) iframe sees only an
    unexplained CORS block. The fallback answers readably."""
    client, _ = studio
    r = client.get("/api/explorer")
    assert r.status_code == 404
    assert r.headers["access-control-allow-origin"] == "*"
    assert "RELATIVE urls" in r.text and "/preview/" in r.text

    # preflight for a JSON POST from the iframe
    r = client.options(
        "/api/explorer", headers={"access-control-request-headers": "content-type"}
    )
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"

    # real API routes are untouched (registered before the fallback)
    assert client.get("/api/sessions").status_code == 200


# -- stop: graceful mid-turn cancel ------------------------------------------------


class CancellableAgent(FakeAgent):
    """Streams forever until acancel_run flips the flag, then ends the
    stream with RunCancelled (the agno contract)."""

    def __init__(self) -> None:
        super().__init__()
        import asyncio

        self.cancelled = asyncio.Event()
        self.cancel_requests: list[str] = []

    async def acancel_run(self, run_id: str) -> bool:
        self.cancel_requests.append(run_id)
        self.cancelled.set()
        return True

    async def arun(self, message, stream=True, stream_events=True):
        self.seen.append(message)
        yield SimpleNamespace(event="RunContent", content="working…", run_id="run-9")
        await self.cancelled.wait()
        yield SimpleNamespace(event="RunCancelled", run_id="run-9")


def test_cancel_stops_the_turn_and_repairs_memory(studio):
    client, registry = studio
    agent = CancellableAgent()
    registry._build_agent = lambda *a, **k: agent
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    chat_db = FakeChatDb()
    session.agent.db = chat_db
    from agno.run.base import RunStatus

    chat_db.record = SimpleNamespace(
        runs=[SimpleNamespace(run_id="run-9", status=RunStatus.cancelled, messages=[])]
    )

    client.post("/api/sessions/s1/chat", json={"message": "long job"})
    deadline = time.monotonic() + 5  # wait for the stream to reveal run_id
    while session.run_id is None and time.monotonic() < deadline:
        time.sleep(0.02)
    r = client.post("/api/sessions/s1/cancel", json={})
    assert r.status_code == 200 and agent.cancel_requests == ["run-9"]

    events = _collect_until_done(client, "s1")
    assert any(e["type"] == "notice" and e["text"] == "turn stopped" for e in events)
    assert not session.busy and session.run_id is None
    # the cancelled run was repaired: memory keeps the partial work
    run = chat_db.record.runs[0]
    assert run.status == RunStatus.completed
    assert "stopped by the user" in run.messages[-1].content


def test_cancel_when_idle_409s(studio):
    client, _ = studio
    client.post("/api/sessions", json={"name": "s1"})
    assert client.post("/api/sessions/s1/cancel", json={}).status_code == 409


# -- aborted-run repair -----------------------------------------------------------


class ExplodingAgent(FakeAgent):
    """Streams some real work, then dies (billing/transport error)."""

    async def arun(self, message, stream=True, stream_events=True):
        self.seen.append(message)
        run_id = f"run-{len(self.seen)}"
        yield SimpleNamespace(event="RunContent", content="working…", run_id=run_id)
        raise RuntimeError("credit balance too low")


def test_aborted_run_is_repaired_into_memory(studio):
    """A turn killed mid-flight must not vanish from the agent's
    memory: the stored run flips error -> completed with a closing
    note (agno's history builder skips error runs)."""
    from agno.run.base import RunStatus

    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    exploding = ExplodingAgent()
    chat_db = FakeChatDb()
    exploding.db = chat_db
    session.agent = exploding
    # simulate agno having stored the errored run (as it really does)
    chat_db.record = SimpleNamespace(
        runs=[SimpleNamespace(run_id="run-1", status=RunStatus.error, messages=[])]
    )

    client.post("/api/sessions/s1/chat", json={"message": "build it"})
    events = _collect_until_done(client, "s1")
    assert any(e["type"] == "error" for e in events)  # failure surfaced

    run = chat_db.record.runs[0]
    assert run.status == RunStatus.completed  # memory retained
    assert "turn aborted early" in run.messages[-1].content
    assert "credit balance" in run.messages[-1].content


class RunErrorAgent(FakeAgent):
    """Streams some real work, then reports a provider failure as a
    RunError EVENT and ends cleanly — agno's post-retry behavior. No
    exception ever raises, so only the event flags the death."""

    async def arun(self, message, stream=True, stream_events=True):
        self.seen.append(message)
        run_id = f"run-{len(self.seen)}"
        yield SimpleNamespace(event="RunContent", content="working…", run_id=run_id)
        yield SimpleNamespace(
            event="RunError", content="Provider returned error", run_id=run_id
        )


def test_provider_error_event_is_repaired_into_memory(studio):
    """The equal-grouse amnesia: a provider error arrives as a RunError
    STREAM EVENT (agno's retries exhausted), the stream ends cleanly,
    and without repair the stored status=error run vanishes from the
    agent's memory — 'please continue' then replans from scratch while
    the workspace holds all the work."""
    from agno.run.base import RunStatus

    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    erroring = RunErrorAgent()
    chat_db = FakeChatDb()
    erroring.db = chat_db
    session.agent = erroring
    chat_db.record = SimpleNamespace(
        runs=[SimpleNamespace(run_id="run-1", status=RunStatus.error, messages=[])]
    )

    client.post("/api/sessions/s1/chat", json={"message": "build it"})
    events = _collect_until_done(client, "s1")
    assert any(e["type"] == "error" for e in events)  # failure surfaced

    run = chat_db.record.runs[0]
    assert run.status == RunStatus.completed  # memory retained
    assert "turn aborted early" in run.messages[-1].content
    assert "Provider returned error" in run.messages[-1].content


class RestartingAgent:
    """agno's whole-run retry, as the stream shows it: RunStarted is
    yielded once PER ATTEMPT, so a restarted run replays the opening
    event with the turn's earlier tool calls already dropped from the
    model's memory."""

    async def arun(self, message: str, stream: bool = True, stream_events: bool = True):
        yield SimpleNamespace(event="RunStarted", run_id="run-1")
        yield SimpleNamespace(
            event="ToolCallStarted",
            tool=SimpleNamespace(tool_name="file_write", tool_args={"path": "/a"}),
        )
        # provider drops the stream here; agno sleeps and re-enters the
        # attempt loop, rebuilding messages from history + the prompt
        yield SimpleNamespace(event="RunStarted", run_id="run-1")
        yield SimpleNamespace(
            event="RunContent", content="starting over", run_id="run-1"
        )
        yield SimpleNamespace(event="RunCompleted")


def test_retry_rewind_hook_keeps_files_in_step_with_memory(studio):
    """agno's whole-run retry rebuilds the agent's memory from history +
    the prompt, dropping the failed attempt's tool calls. The files those
    calls wrote must go with them, or the model builds a second version
    beside work it can't remember doing. pre_hooks run per ATTEMPT under a
    stable run_id, which is what makes the rewind placeable at all.

    A rewind is a checkout, and a checkout APPENDS: the head moves
    forward onto a commit holding the pre-turn content rather than back
    onto the pre-turn commit itself. So the assertion is about content
    — the file is gone and nothing differs from the anchor — and the
    anchor is still in the log, which is what keeps it a usable anchor
    for the next retry and for the human's own undo."""
    import asyncio

    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws
    hook = sessions_mod.Registry._retry_rewind_hook(ws)
    ctx = SimpleNamespace(run_id="run-1")

    asyncio.run(hook(ctx))  # attempt 1: records the pre-turn head
    start = ws.head
    ws.files.write("/workspace/app/index.html", "half an app")
    assert ws.head != start, "the write should have moved the head"

    asyncio.run(hook(ctx))  # attempt 2 under the same run: a retry
    assert not ws.files.fs.isfile("/workspace/app/index.html")
    assert not ws.changed_since(start).paths, "the content is back at the anchor"
    assert ws.head != start, "the rewind landed a commit rather than dropping one"
    assert start in {c.id for c in ws.log()}, "the anchor is still reachable"
    rewound = ws.head

    # a NEW run is a new turn, not a retry — it re-anchors and rewinds
    # nothing, or the next turn would undo the previous one's work
    ws.files.write("/workspace/keep.txt", "second turn")
    after_write = ws.head
    asyncio.run(hook(SimpleNamespace(run_id="run-2")))
    assert ws.head == after_write
    assert ws.files.fs.isfile("/workspace/keep.txt")
    assert rewound != after_write


def test_run_restart_is_surfaced_as_a_notice(studio):
    """A silent restart reads as the model losing the plot: the human
    watches the turn redo itself with no explanation. The second
    RunStarted is the only signal agno gives, so the turn names it."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    registry.get("s1").agent = RestartingAgent()

    client.post("/api/sessions/s1/chat", json={"message": "build it"})
    events = _collect_until_done(client, "s1")

    notices = [e["text"] for e in events if e["type"] == "notice"]
    assert any("restarted" in n for n in notices), notices
    assert any("attempt 2" in n for n in notices), notices
    # the FIRST RunStarted must stay quiet — every turn has one
    assert len([n for n in notices if "restarted" in n]) == 1


def test_arrow_pool_is_fork_safe_from_first_import():
    """Arrow's default mimalloc pool segfaults in forked children
    (observed: SIGSEGV in libarrow's mi_thread_init, 'multi-threaded
    process forked'). Sandbox workers fork from sandtrap's forkserver
    broker rather than from the server process now, but the broker
    inherits this process's environment — and preload_grants imports
    pyarrow into it — so the pin still has to be set here. pyarrow
    reads ARROW_DEFAULT_MEMORY_POOL at import, and importing pandas
    imports pyarrow, so the package __init__ must win the race. A
    subprocess proves the end state, immune to whatever this test
    process already imported."""
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import nontainer_studio, pyarrow;"
            "print(pyarrow.default_memory_pool().backend_name)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "system"


def test_repair_leaves_healthy_runs_alone(studio):
    from agno.run.base import RunStatus

    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    chat_db = FakeChatDb()
    chat_db.record = SimpleNamespace(
        runs=[SimpleNamespace(run_id="run-1", status=RunStatus.completed, messages=[])]
    )
    session.agent.db = chat_db

    sessions_mod.repair_aborted_run(session, "run-1", "whatever")
    assert chat_db.record.runs[0].messages == []  # untouched


def test_published_urls_survive_restart(studio, tmp_path):
    """A version is a store-scoped tag and the db is the app's own file,
    so a restart reopens the app from the manifest alone — no session
    involved, nothing to reconstruct."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _seed_db_app(session)
    session.db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    session.db.execute("INSERT INTO t VALUES ('before-restart')")
    pub = client.post("/api/sessions/s1/publish").json()
    assert client.get(f"{pub['url']}api/names").json() == {"names": ["before-restart"]}

    # "restart": a fresh registry over the same store, sessions unopened
    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    with TestClient(server.build_app(reborn)) as client2:
        r = client2.get(f"{pub['url']}api/names")
        assert r.status_code == 200
        assert r.json() == {"names": ["before-restart"]}  # the app's own db
        assert client2.get("/apps/not-a-real-token/").status_code == 404
    reborn.close()


def test_known_sessions_open_lazily_on_get(studio, tmp_path):
    """After a restart, a manifest-known session must serve GETs
    (events, files, preview probe) without waiting for a POST — a
    reloaded browser tab points at yesterday's session immediately.
    Unknown names still 404 (GETs never create sessions)."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})

    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    with TestClient(server.build_app(reborn)) as client2:
        assert client2.get("/api/sessions/s1/events?wait=0").status_code == 200
        assert client2.get("/api/sessions/s1/app").json() == {"exists": False}
        assert client2.get("/api/sessions/ghost/events?wait=0").status_code == 404
    reborn.close()


def test_app_probe_flips_when_app_lands(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    assert client.get("/api/sessions/s1/app").json() == {"exists": False}
    _seed_app(registry.get("s1").ws)
    assert client.get("/api/sessions/s1/app").json() == {"exists": True}


def test_ui_dir_exists_from_the_start(studio):
    """Agents predictably savefig straight into /workspace/ui instead of
    assigning objects to `ui` — the near-miss should work, not
    FileNotFoundError (VFS open doesn't create parents)."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws
    assert ws.files.fs.isdir("/workspace/ui")
    result = ws.run_python("open('/workspace/ui/x.png', 'wb').write(b'png-ish')")
    assert not result.error
    assert ws.files.fs.read("/workspace/ui/x.png") == b"png-ish"


def test_error_truncation_keeps_the_exception_line(studio):
    """Tracebacks cap by cutting the MIDDLE: the final line (the
    exception) is the whole point of the message."""
    from nontainer_studio.server import _short_middle

    trace = (
        "Traceback (most recent call last):\n"
        + "\n".join(f'  File "<x>", line {i}, in frame_{i}' for i in range(200))
        + "\nFileNotFoundError: No such file or directory: '/workspace/ui/plot.png'"
    )
    capped = _short_middle(trace)
    assert len(capped) <= 2_100
    assert capped.startswith("Traceback")
    assert capped.endswith(
        "FileNotFoundError: No such file or directory: '/workspace/ui/plot.png'"
    )
    assert "…[truncated]…" in capped
    # short messages pass through untouched
    assert _short_middle("boom") == "boom"


class LongExplodingAgent(FakeAgent):
    async def arun(self, message, stream=True, stream_events=True):
        self.seen.append(message)
        yield SimpleNamespace(event="RunContent", content="working…", run_id="run-1")
        raise RuntimeError("x" * 5_000 + " THE ACTUAL ERROR")


def test_error_event_tail_survives_capping(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    registry.get("s1").agent = LongExplodingAgent()
    client.post("/api/sessions/s1/chat", json={"message": "go"})
    events = _collect_until_done(client, "s1")
    error = next(e for e in events if e["type"] == "error")
    assert error["message"].endswith("THE ACTUAL ERROR")
    assert len(error["message"]) < 2_200


# -- delete -----------------------------------------------------------------------


def test_delete_removes_the_whole_universe(studio, tmp_path):
    """Delete takes the workspace branch, the transcript and the chat
    record. Not the db: it is an external store this session may share
    with a fork or a published app, so no deletion removes one. Its
    published apps are NOT part of that universe either — see the next
    test."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.ws.files.write("keep.txt", "data")
    _seed_app(session.ws)
    client.post("/api/sessions/s1/upload?name=u.txt", content=b"x")
    db = tmp_path / _db_of(registry, "s1")
    assert db.exists()
    assert (tmp_path / "events" / "s1.jsonl").exists()

    assert client.delete("/api/sessions/s1").json() == {"ok": True}

    assert client.get("/api/sessions").json()["sessions"] == []
    assert client.get("/api/sessions/s1/events?wait=0").status_code == 404
    assert db.exists()  # nobody names it now; the sweep is what collects it
    assert not (tmp_path / "events" / "s1.jsonl").exists()

    # recreating the name is a FRESH universe — the branch really died
    # (an orphaned branch would resurrect the old files here)
    client.post("/api/sessions", json={"name": "s1"})
    reborn = registry.get("s1")
    assert not reborn.ws.files.fs.exists("keep.txt")
    assert not reborn.ws.files.fs.exists("/workspace/app/index.html")


def test_the_sweep_collects_the_db_files_no_row_names(studio, tmp_path):
    """Deleting a session drops its row, not its file — the file may be
    a fork's store or the one an app is serving over, and counting
    referrers at every delete is a rule that has to be got right in
    four places. The sweep is the one place instead: it reads the
    manifest, keeps what it names, and removes the rest."""
    client, registry = studio
    for name in ("keeper", "doomed"):
        client.post("/api/sessions", json={"name": name})
    _seed_app(registry.get("keeper").ws)
    token = _publish(client, "keeper")["token"]
    kept = _db_of(registry, "keeper")
    gone = _db_of(registry, "doomed")

    assert client.delete("/api/sessions/doomed").json() == {"ok": True}
    assert (tmp_path / gone).exists()  # a deletion never removes a db

    assert registry.sweep_dbs() == [gone]

    assert not (tmp_path / gone).exists()
    # the live session's file stays, and so does the one the app names
    assert (tmp_path / kept).exists()
    assert _app_db_of(registry, token) == kept
    assert registry.sweep_dbs() == []


def test_the_sweep_keeps_a_db_a_fork_still_names(studio, tmp_path):
    """The rule the sweep exists to make simple: the origin is deleted
    while its fork's row still names the file, so the file is not an
    orphan and the fork keeps reading it."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    origin = registry.get("s1")
    origin.db.execute("CREATE TABLE notes (t TEXT)")
    origin.db.execute("INSERT INTO notes VALUES (?)", ("the fork's rows",))
    fork = registry.get(client.post("/api/sessions/s1/fork", json={}).json()["name"])
    shared = _db_of(registry, "s1")

    assert client.delete("/api/sessions/s1").json() == {"ok": True}
    assert registry.sweep_dbs() == []

    assert (tmp_path / shared).exists()
    assert fork.db.query("SELECT t FROM notes") == [("the fork's rows",)]


def test_the_sweep_leaves_a_file_it_still_holds_open(studio, tmp_path, caplog):
    """A handle in the map is a connection something asked for.
    Unlinking the file under it would not fail anything — the writes
    would simply go nowhere — so an unnamed file that is open is
    logged and left for the next sweep, which takes it once the
    connection is closed."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    stray = "dbs/deadbeefdeadbeef.sqlite"
    with registry._lock:
        registry._db_handle(stray).execute("CREATE TABLE t (v TEXT)")

    with caplog.at_level("INFO", logger="nontainer_studio.sessions"):
        assert registry.sweep_dbs() == []
    assert stray in "\n".join(caplog.messages)
    assert (tmp_path / stray).exists()

    with registry._lock:
        registry._dbs.pop(stray).close()
    assert registry.sweep_dbs() == [stray]
    assert not (tmp_path / stray).exists()


def test_the_sweep_runs_when_the_registry_opens(studio, tmp_path):
    """Every restart tidies up: the files a previous run's deletions
    left behind are collected before anything is served."""
    client, registry = studio
    for name in ("keeper", "doomed"):
        client.post("/api/sessions", json={"name": name})
    orphan = _db_of(registry, "doomed")
    kept = _db_of(registry, "keeper")
    assert client.delete("/api/sessions/doomed").json() == {"ok": True}
    assert (tmp_path / orphan).exists()
    registry.close()

    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    try:
        assert not (tmp_path / orphan).exists()
        assert (tmp_path / kept).exists()
    finally:
        reborn.close()


def test_an_open_that_fails_leaves_no_handle_behind(studio, tmp_path):
    """A failed open leaves nothing: not the manifest reservation, and
    not the connection it minted on the way. A handle left in the map
    holds the file open for the life of the process AND stops the
    sweep collecting it — the two together make one failed click a
    permanent leak."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "keeper"})

    made = []
    handle = registry._db_handle
    registry._db_handle = lambda rel: made.append((rel, handle(rel))) or made[-1][1]
    registry._assemble = lambda *a, **k: 1 / 0
    try:
        with pytest.raises(ZeroDivisionError):
            registry.open("doomed")
    finally:
        del registry._db_handle, registry._assemble

    rel, db = made[-1]
    assert rel not in registry._dbs
    with pytest.raises(sqlite3.ProgrammingError):  # the connection went too
        db.query("SELECT 1")
    assert "doomed" not in registry.known()
    # and the file it minted is an orphan the sweep can take
    assert registry.sweep_dbs() == [rel]


def test_an_open_that_fails_closes_the_workspace_it_opened(studio, tmp_path):
    """The other half of leaving nothing behind: an open workspace pins
    its branch, so a session that never came up would hold one for the
    life of the process — and every verb that removes a branch closes
    the session first, which is a step nothing can take for a session
    that does not exist."""
    client, registry = studio
    closed = []
    store_open = registry._store.open

    def spy(name, **kw):
        ws = store_open(name, **kw)
        ws.close = lambda close=ws.close: (closed.append(name), close())[1]
        return ws

    registry._store.open = spy
    registry._assemble = lambda *a, **k: 1 / 0
    try:
        with pytest.raises(ZeroDivisionError):
            registry.open("doomed")
    finally:
        registry._store.open = store_open
        del registry._assemble

    assert closed == ["doomed"]
    # nothing is holding the branch, so the name opens for real
    assert registry.open("doomed").ws.files.fs.isdir("/workspace/skills")


def test_an_open_whose_cleanup_fails_still_raises_the_real_error(studio):
    """A close that throws on the way out must not become the error the
    caller sees: the reason the open failed is the one worth reading,
    and a masked one sends the reader after the wrong fault."""
    client, registry = studio
    store_open = registry._store.open

    def spy(name, **kw):
        ws = store_open(name, **kw)
        ws.close = lambda: (_ for _ in ()).throw(RuntimeError("close blew up"))
        return ws

    registry._store.open = spy
    registry._assemble = lambda *a, **k: 1 / 0
    try:
        with pytest.raises(ZeroDivisionError):
            registry.open("doomed")
    finally:
        registry._store.open = store_open
        del registry._assemble


def test_the_sweep_refuses_a_manifest_it_cannot_read(studio, tmp_path, caplog):
    """Destructive cleanup never runs on a reference set it could not
    load. Every other reader answers an unparseable manifest with an
    empty one — a page that 404s beats a page that 500s — but for the
    one caller that DELETES what the manifest does not mention, an
    empty answer means every store on the install."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    db = tmp_path / _db_of(registry, "s1")
    registry.release("s1")  # no handle held: only the manifest protects it
    (tmp_path / "sessions.json").write_text("{not json at all")

    with caplog.at_level("WARNING", logger="nontainer_studio.sessions"):
        assert registry.sweep_dbs() == []
    assert "sessions.json" in "\n".join(caplog.messages)
    assert db.exists()


def test_the_sweep_refuses_a_manifest_it_cannot_open(studio, tmp_path, caplog):
    """Unreadable is the same answer as unparseable: a fault on the
    read is not a store with nothing in it."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    db = tmp_path / _db_of(registry, "s1")
    registry.release("s1")  # no handle held: only the manifest protects it
    path = tmp_path / "sessions.json"
    path.chmod(0o000)
    try:
        with caplog.at_level("WARNING", logger="nontainer_studio.sessions"):
            assert registry.sweep_dbs() == []
    finally:
        path.chmod(0o644)
    assert db.exists()


def test_the_sweep_refuses_when_the_manifest_names_nothing(studio, tmp_path, caplog):
    """A manifest that parses but names no session and no app, beside
    db files that exist, is far more likely a truncated write than a
    store that emptied itself — so the sweep says what it would have
    taken and takes none of it."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    rel = _db_of(registry, "s1")
    registry.release("s1")  # no handle held: only the manifest protects it
    (tmp_path / "sessions.json").write_text("{}")

    with caplog.at_level("WARNING", logger="nontainer_studio.sessions"):
        assert registry.sweep_dbs() == []
    assert rel in "\n".join(caplog.messages)  # what it would have taken
    assert (tmp_path / rel).exists()


def test_the_sweep_on_a_brand_new_store_takes_nothing(tmp_path):
    """No manifest and no db files is a store nobody has used yet, not
    a fault: the sweep has nothing to read and nothing to collect."""
    fresh = tmp_path / "fresh"
    registry = sessions_mod.Registry(model_factory=lambda *a: None, store=fresh)
    try:
        assert not (fresh / "sessions.json").exists()
        assert registry.sweep_dbs() == []
    finally:
        registry.close()


def test_delete_busy_409s_and_unknown_404s(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.turn_lock.acquire()
    try:
        assert client.delete("/api/sessions/s1").status_code == 409
    finally:
        session.turn_lock.release()
    assert client.delete("/api/sessions/nope").status_code == 404


def test_delete_leaves_other_sessions_alone(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions", json={"name": "s2"})
    registry.get("s2").ws.files.write("mine.txt", "s2 data")
    client.delete("/api/sessions/s1")
    assert client.get("/api/sessions").json()["sessions"] == [
        {
            "name": "s2",
            "title": "New session",
            "busy": False,
            "model": None,
            "delegates": 0,
            "delegate_count": 0,
        }
    ]
    assert registry.get("s2").ws.files.fs.read("mine.txt") == b"s2 data"


# -- models: registry, per-session switching --------------------------------------


def test_provider_spec_parsing(monkeypatch):
    from nontainer_studio import providers

    assert providers.parse_spec("dummy") == ("dummy", "dummy")
    assert providers.parse_spec("openrouter:deepseek/deepseek-v4-flash") == (
        "openrouter",
        "deepseek/deepseek-v4-flash",
    )
    # bare provider -> its default model
    provider, model = providers.parse_spec("anthropic")
    assert provider == "anthropic" and model
    # legacy bare model id rides the default provider
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert providers.parse_spec("claude-sonnet-5") == (
        "anthropic",
        "claude-sonnet-5",
    )
    with pytest.raises(ValueError):
        providers.parse_spec("nope:whatever")


def test_models_endpoint_reflects_env(studio, monkeypatch):
    client, _ = studio
    monkeypatch.setenv("NONTAINER_STUDIO_MODEL", "dummy")
    data = client.get("/api/models").json()
    assert data["default"] == "dummy"
    names = [p["name"] for p in data["providers"]]
    assert "dummy" in names  # advertised only because it's the default
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("NONTAINER_STUDIO_MODEL", "openrouter")
    data = client.get("/api/models").json()
    assert data["default"] == "openrouter:anthropic/claude-sonnet-5"
    openrouter = next(p for p in data["providers"] if p["name"] == "openrouter")
    assert openrouter["models"]  # curated picks for the picker


def test_model_switch_persists_and_notices(studio, tmp_path):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")

    r = client.post("/api/sessions/s1/model", json={"model": "dummy"})
    assert r.json() == {"ok": True, "model": "dummy"}
    assert session.model == "dummy"
    assert any(
        e["type"] == "notice" and "model → dummy" in e["text"] for e in session.events
    )
    # the rail shows it, and a restart remembers it
    listed = client.get("/api/sessions").json()["sessions"]
    assert listed == [
        {
            "name": "s1",
            "title": "New session",
            "busy": False,
            "model": "dummy",
            "delegates": 0,
            "delegate_count": 0,
        }
    ]
    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    assert reborn.open("s1").model == "dummy"
    reborn.close()

    # busy sessions can't switch; empty spec 400s
    session.turn_lock.acquire()
    try:
        assert (
            client.post("/api/sessions/s1/model", json={"model": "dummy"}).status_code
            == 409
        )
    finally:
        session.turn_lock.release()
    assert client.post("/api/sessions/s1/model", json={}).status_code == 400


# -- dummy model: the real agent loop, scripted -----------------------------------


def test_dummy_model_drives_real_agent(tmp_path):
    """The E2E test double: DummyModel fakes only the LLM — the agno
    run loop, WorkspaceTools, and the workspace all execute for real.
    Directives in the user message script the turn."""
    from nontainer_studio.dummy import DummyModel

    registry = sessions_mod.Registry(
        model_factory=lambda spec=None: DummyModel(), store=tmp_path
    )
    with TestClient(server.build_app(registry)) as client:
        client.post("/api/sessions", json={"name": "s1"})
        message = (
            '!tool file_write {"path": "/workspace/notes.md", "content": "scripted"}\n'
            "!text Wrote your note."
        )
        client.post("/api/sessions/s1/chat", json={"message": message})
        events = _collect_until_done(client, "s1")

        kinds = [e["type"] for e in events]
        assert "tool_start" in kinds and "tool_end" in kinds
        started = next(e for e in events if e["type"] == "tool_start")
        assert started["name"] == "file_write"
        assert (
            started["args"]["path"] == "/workspace/notes.md"
        )  # structured through agno
        reply = "".join(e["delta"] for e in events if e["type"] == "text")
        assert reply == "Wrote your note."
        # the tool REALLY ran: the workspace has the file, committed
        ws = registry.get("s1").ws
        assert ws.files.fs.read("/workspace/notes.md") == b"scripted"
        # and the done event carries the run mapping for undo
        done = next(e for e in events if e["type"] == "done")
        assert done["run_id"] and done["head"]
    registry.close()


# -- identity is a minted slug; the label is a title -----------------------------


def test_create_mints_a_slug_and_starts_untitled(studio):
    """No name in the body = the UI's "+ New": the server mints identity
    so nobody types it, and the session starts with no title."""
    client, registry = studio
    r = client.post("/api/sessions", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "New session"
    # a pettable slug, and a legal session id (it names a branch, a db
    # file, a jsonl and every route)
    assert re.fullmatch(r"[a-z]+(-[a-z]+)+", body["name"]), body["name"]
    assert registry.get(body["name"]) is not None


def test_minted_names_are_unique(studio):
    client, _ = studio
    names = {client.post("/api/sessions", json={}).json()["name"] for _ in range(8)}
    assert len(names) == 8


def test_explicit_name_still_creates(studio):
    """The typed-name form stays for tests/scripting — it just never
    becomes the label."""
    client, registry = studio
    r = client.post("/api/sessions", json={"name": "s1"})
    assert r.json() == {"ok": True, "name": "s1", "title": "New session"}
    assert client.post("/api/sessions", json={"name": "s1"}).json()["name"] == "s1"
    assert client.post("/api/sessions", json={"name": "bad/name"}).status_code == 400


def test_title_resolution_user_outranks_agent(studio):
    """user > agent > default, and clearing the user's REVEALS the
    agent's latest rather than falling to the default."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    assert registry.title_of("s1") == "New session"

    registry.set_agent_title("s1", "Revenue dashboard")
    assert registry.title_of("s1") == "Revenue dashboard"

    r = client.post("/api/sessions/s1/title", json={"title": "Q3 numbers"})
    assert r.json()["title"] == "Q3 numbers"
    # the agent keeps suggesting; the human's choice still wins
    registry.set_agent_title("s1", "Something else")
    assert registry.title_of("s1") == "Q3 numbers"

    # clearing falls back to the agent's LATEST, not the default
    assert client.post("/api/sessions/s1/title", json={"title": ""}).json()[
        "title"
    ] == ("Something else")


def test_agent_titles_are_clamped(studio):
    """The agent writes this free-text: a newline would break the rail
    row and a novel would blow past it."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    registry.set_agent_title("s1", "  line one\nline two   \t spaced  ")
    assert registry.title_of("s1") == "line one line two spaced"
    registry.set_agent_title("s1", "x" * 200)
    assert registry.title_of("s1") == "x" * 60
    # blank/junk reads as "no title", never as ""
    registry.set_agent_title("s1", "   ")
    assert registry.title_of("s1") == "New session"


def test_rail_lists_newest_first(studio):
    """Slugs carry no order, so alphabetical would scatter new sessions
    into random rail slots — birthdays decide."""
    client, _ = studio
    first = client.post("/api/sessions", json={"name": "aaa"}).json()["name"]
    time.sleep(0.01)
    second = client.post("/api/sessions", json={"name": "zzz"}).json()["name"]
    listed = [s["name"] for s in client.get("/api/sessions").json()["sessions"]]
    assert listed == [second, first]  # newest first, NOT alphabetical


def test_titles_and_birthday_survive_restart(studio, tmp_path):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    registry.set_agent_title("s1", "Persisted title")

    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    assert reborn.title_of("s1") == "Persisted title"
    reborn.close()


def test_recommend_title_tool_names_the_session(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    tool = registry._title_tool("s1")

    assert "Revenue dashboard" in tool("Revenue dashboard")
    assert registry.title_of("s1") == "Revenue dashboard"
    # it can rename on a topic shift
    tool("Debugging the CSV import")
    assert registry.title_of("s1") == "Debugging the CSV import"


def test_recommend_title_cannot_override_the_human(studio):
    """The agent's suggestion is stored but not shown — and the tool
    result says so, rather than claiming a title it didn't get."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/title", json={"title": "Mine"})

    said = registry._title_tool("s1")("Something the agent picked")
    assert "Mine" in said  # reports what's SHOWN, not what it asked for
    assert registry.title_of("s1") == "Mine"
    # ...but it was remembered: clearing the human's reveals it
    assert client.post("/api/sessions/s1/title", json={"title": ""}).json()[
        "title"
    ] == ("Something the agent picked")


def test_title_tool_survives_a_model_switch(studio):
    """The closure captures only (registry, name) — nothing turn-scoped
    — so the agent rebuilt by a model switch still titles the right
    session."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    tool_before = registry._title_tool("s1")
    client.post("/api/sessions/s1/model", json={"model": "dummy"})
    tool_before("Still works")
    assert registry.title_of("s1") == "Still works"


def test_agent_is_given_the_title_tool(studio):
    """The wiring the rest of stage 3 rests on: a studio tool riding
    alongside the nontainer toolkit in the same agno Agent."""
    pytest.importorskip("agno")
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    # the fixture fakes _build_agent; call the real one
    agent = sessions_mod.Registry._build_agent(
        registry, "s1", registry.get("s1").ws, registry.get("s1").runtime
    )
    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in agent.tools}
    assert "recommend_title" in names


def test_primer_teaches_when_to_title(studio):
    assert "recommend_title" in sessions_mod.STUDIO_PRIMER
    assert "New session" in sessions_mod.STUDIO_PRIMER


def test_the_primer_names_only_verbs_the_session_carries(studio):
    """The primer teaches terminal verbs, and a verb it names that the
    session does not carry costs a turn to discover. The three it names
    are wired by the same two calls every session gets, so the claim is
    checked against the wiring rather than trusted."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    commands = registry.get("s1").ws.runtime.commands
    for verb in ("ws-git", "ws-pytest", "ws-vitest"):
        assert verb in sessions_mod.VERSIONING_PRIMER
        assert verb in commands


def test_the_primer_says_where_else_a_delegate_can_start():
    """Which fork points exist is the studio's own answer: its sessions
    share one store, so any commit of any of them is one. The rest of
    what an ask takes is the `sessions` tool's own description, and
    saying it twice is how the two drift."""
    primer = sessions_mod.VERSIONING_PRIMER
    assert "fork_from=<session>@<commit>" in primer
    assert "ws-git branch" in primer  # how the agent learns the names
    assert "resume" in primer


def test_the_db_primer_says_the_store_is_shared():
    """An agent told its db is a copy would trust rows nobody else can
    see, and would not defend a handler against a concurrent writer."""
    assert "HANDLE to one external store" in sessions_mod.DB_PRIMER
    assert "a fork" in sessions_mod.DB_PRIMER
    assert "delegate" in sessions_mod.DB_PRIMER
    assert "copies it" not in sessions_mod.DB_PRIMER
    assert "the published app owns" not in sessions_mod.STUDIO_PRIMER


class TitlingAgent(FakeAgent):
    """Calls recommend_title mid-turn, like the real thing.

    The real loop EXECUTES the tool and THEN emits ToolCallCompleted —
    two separate effects (the manifest write and the transcript event).
    A fake that only yielded the event would leave the manifest unwritten
    and quietly test half the feature."""

    def __init__(self, registry, name: str, title: str = "Revenue dashboard") -> None:
        super().__init__()
        self._tool = registry._title_tool(name)
        self.title = title

    async def arun(self, message, stream=True, stream_events=True):
        self.seen.append(message)
        run_id = f"run-{len(self.seen)}"
        result = self._tool(self.title)  # the tool really runs
        yield SimpleNamespace(
            event="ToolCallCompleted",
            tool=SimpleNamespace(
                tool_name="recommend_title",
                tool_args={"title": self.title},
                result=result,
                run_id=run_id,
            ),
        )
        yield SimpleNamespace(event="RunContent", content="named it", run_id=run_id)


def test_title_event_rides_the_transcript(studio):
    """The tool writes the manifest; the EVENT is the temporal record —
    it marks when the session got its name."""
    client, registry = studio
    registry._build_agent = lambda n, *a, **k: TitlingAgent(registry, n)
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/chat", json={"message": "hi"})
    events = _collect_until_done(client, "s1")

    titled = [e for e in events if e["type"] == "title"]
    assert len(titled) == 1 and titled[0]["title"] == "Revenue dashboard"
    # the tool_end stays too — the human sees the agent named the session
    assert any(e["type"] == "tool_end" for e in events)


def test_title_event_carries_the_stored_form(studio):
    """Clamped like the manifest stores it, and junk emits nothing at
    all rather than an empty label."""
    client, registry = studio
    registry._build_agent = lambda n, *a, **k: TitlingAgent(
        registry, n, "  ragged\ntitle  "
    )
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/chat", json={"message": "hi"})
    events = _collect_until_done(client, "s1")
    assert [e["title"] for e in events if e["type"] == "title"] == ["ragged title"]

    registry._build_agent = lambda n, *a, **k: TitlingAgent(registry, n, "   ")
    client.post("/api/sessions", json={"name": "s2"})
    client.post("/api/sessions/s2/chat", json={"message": "hi"})
    events = _collect_until_done(client, "s2")
    assert not [e for e in events if e["type"] == "title"]


def test_edit_rewinds_the_agents_title(studio):
    """Rollback-follow: the agent named the session out of a conversation
    the edit is unsaying, so the title goes back to the one that was in
    force before the cut."""
    client, registry = studio
    registry._build_agent = lambda n, *a, **k: TitlingAgent(registry, n, "First topic")
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _turn(client, "s1", "one")
    assert registry.title_of("s1") == "First topic"

    session.agent.title = "Second topic"
    _turn(client, "s1", "two")
    assert registry.title_of("s1") == "Second topic"

    # Unsay turn two: the title it gave goes with it. This drives the
    # registry half directly — the /edit route then runs a FRESH turn,
    # which re-titles and would mask the rewind we're asserting.
    seq = _user_seqs(session)[1]
    registry.rewind_to_event(session, seq)
    assert registry.title_of("s1") == "First topic"


def test_edit_keeps_a_title_it_cannot_prove_was_undone(studio):
    """No title event survives the cut. That is ambiguous — never
    titled, or titled before the event window — so the manifest's value
    stands rather than being wiped."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _turn(client, "s1", "one")  # plain FakeAgent: no title event
    registry.set_agent_title("s1", "Titled long ago")

    seq = _user_seqs(session)[0]
    r = client.post("/api/sessions/s1/edit", json={"seq": seq, "message": "redo"})
    _collect_until_done(client, "s1", since=r.json()["since"] - 1)
    assert registry.title_of("s1") == "Titled long ago"


def test_edit_never_rewinds_the_humans_title(studio):
    """The human's title isn't a conversational fact — an edit must not
    touch it."""
    client, registry = studio
    registry._build_agent = lambda n, *a, **k: TitlingAgent(registry, n, "Agent's idea")
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _turn(client, "s1", "one")
    client.post("/api/sessions/s1/title", json={"title": "Mine"})

    seq = _user_seqs(session)[0]
    r = client.post("/api/sessions/s1/edit", json={"seq": seq, "message": "redo"})
    _collect_until_done(client, "s1", since=r.json()["since"] - 1)
    assert registry.title_of("s1") == "Mine"


def test_delete_forgets_the_title_and_birthday(studio):
    """A slug is free to be minted again once `sessions` forgets it —
    it must not come back wearing a dead session's name."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    registry.set_agent_title("s1", "Doomed")
    client.delete("/api/sessions/s1")

    manifest = registry._manifest()
    assert "s1" not in manifest["titles"]
    assert "s1" not in manifest["created"]
    # a session reborn under the same slug starts untitled
    client.post("/api/sessions", json={"name": "s1"})
    assert registry.title_of("s1") == "New session"


def test_v1_manifest_format_tolerated(studio, tmp_path):
    (tmp_path / "sessions.json").write_text('["old-style"]')
    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    assert {
        "name": "old-style",
        "title": "New session",
        "busy": False,
        "model": None,
        "delegates": 0,
        "delegate_count": 0,
    } in reborn.list()
    assert reborn.resolve("nope") is None
    reborn.close()


# -- process isolation --------------------------------------------------------------


def test_agent_sandbox_is_process_isolated_and_crash_proof(studio):
    """The default: agent code runs in a worker process of its own,
    forked from sandtrap's broker rather than from this server. Killing
    that worker (a stand-in for segfault/OOM) costs nothing but the
    moment — the server survives, and the next execution respawns and
    still sees the workspace."""
    import os
    import signal

    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws

    proc = ws._sandbox._process  # only exists under process isolation
    assert proc.is_alive()

    assert ws.run_python("open('/kept.txt', 'w').write('x')").error is None
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(timeout=5.0)

    r = ws.run_python("content = open('/kept.txt').read()")
    assert r.error is None
    assert r.namespace["content"] == "x"


def test_db_host_object_bridges_through_isolation(studio):
    """The studio's `db` is a live sqlite wrapper — under process
    isolation it must cross as an RPC proxy, not vanish as
    unpicklable."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    r = session.ws.run_python(
        "db.execute('CREATE TABLE IF NOT EXISTS t (v TEXT)')\n"
        "db.execute('INSERT INTO t VALUES (?)', ('from worker',))\n"
        "rows = db.query('SELECT v FROM t')"
    )
    assert r.error is None, r.error
    assert r.namespace["rows"] == [("from worker",)]
    # the PARENT's db saw the writes (it IS the store)
    assert session.db.query("SELECT v FROM t") == [("from worker",)]


def test_db_executemany_bulk_loads_through_isolation(studio):
    """Bulk insert is the first thing an agent does when building an
    app on uploaded data, and executemany is the sqlite3 API every
    model assumes. Without it (equal-grouse) they fall back to
    hand-escaped literal INSERT strings."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    r = session.ws.run_python(
        "db.execute('CREATE TABLE ev (make TEXT, n INTEGER)')\n"
        "rows = [('TESLA', 1), ('KIA', 2), ('FORD', 3)]\n"
        "db.executemany('INSERT INTO ev VALUES (?, ?)', rows)\n"
        "count = db.query('SELECT COUNT(*) FROM ev')[0][0]"
    )
    assert r.error is None, r.error
    assert r.namespace["count"] == 3
    assert session.db.query("SELECT n FROM ev ORDER BY n") == [(1,), (2,), (3,)]


def test_executor_factory_guard(monkeypatch):
    """Default and unrecognized values pick no custom executor (studio's
    historical LocalExecutor path). The dud/dud-vm branches import
    nontainer.executor_dud lazily, so they stay dormant until that lands."""
    monkeypatch.delenv("NONTAINER_STUDIO_EXECUTOR", raising=False)
    assert sessions_mod._executor_factory() is None
    monkeypatch.setenv("NONTAINER_STUDIO_EXECUTOR", "bogus")
    assert sessions_mod._executor_factory() is None


def test_vm_prewarm_noop_outside_dud_vm(monkeypatch):
    """Prewarm must never fire (or import dud) outside dud-vm mode."""
    monkeypatch.delenv("NONTAINER_STUDIO_EXECUTOR", raising=False)
    assert sessions_mod.start_vm_prewarm() is None  # would raise on dud


def test_vm_warm_zero_bakes_image_without_booting(monkeypatch):
    """VM_WARM=0 skips warm VMs but still eagerly builds the image, so
    the first session open pays boot-only, never build+boot."""
    baked = []
    monkeypatch.setattr(sessions_mod, "_bake_image", baked.append)
    monkeypatch.setenv("NONTAINER_STUDIO_EXECUTOR", "dud-vm")
    monkeypatch.setenv("NONTAINER_STUDIO_VM_WARM", "0")
    t = sessions_mod.start_vm_prewarm()
    assert t is not None
    t.join(timeout=5)
    assert len(baked) == 1 and baked[0] == sessions_mod._vm_config()


def test_vm_config_pins_host_versions():
    import importlib.metadata as md

    cfg = sessions_mod._vm_config()
    assert f"pandas=={md.version('pandas')}" in cfg["packages"]
    assert all("==" in p for p in cfg["packages"])


def test_vm_config_medium_defaults_auto(monkeypatch):
    monkeypatch.delenv("NONTAINER_STUDIO_VM_MEDIUM", raising=False)
    assert sessions_mod._vm_config()["medium"] == "auto"
    monkeypatch.setenv("NONTAINER_STUDIO_VM_MEDIUM", "initramfs")
    assert sessions_mod._vm_config()["medium"] == "initramfs"


def test_vm_config_guest_python_matches_host_minor():
    """Pickle portability spans the interpreter, not just package
    versions — and pinned versions must have wheels for the guest's
    python (a 3.13 host against a hardcoded 3.12 guest bricks the
    image build when a pin lacks a cp312 wheel)."""
    import sys

    cfg = sessions_mod._vm_config()
    assert cfg["image"] == f"python:3.{sys.version_info.minor}-slim"


def test_dud_vm_defaults_the_pool_cap(monkeypatch):
    """Studio never closes sessions, so unbounded bound-VM growth is
    the long-running failure mode; dud's pool bounds it only when
    DUD_VM_MAX_TOTAL is set. Studio defaults it; the operator wins."""
    import os

    monkeypatch.delenv("DUD_VM_MAX_TOTAL", raising=False)
    try:
        sessions_mod._ensure_vm_cap()
        assert os.environ["DUD_VM_MAX_TOTAL"] == "4"
    finally:
        os.environ.pop("DUD_VM_MAX_TOTAL", None)
    monkeypatch.setenv("DUD_VM_MAX_TOTAL", "9")
    sessions_mod._ensure_vm_cap()
    assert os.environ["DUD_VM_MAX_TOTAL"] == "9"


def test_executor_factory_plumbed_on_open_and_resolve(tmp_path, monkeypatch):
    """The regression this branch exists to prevent: BOTH workspace
    creation paths — session open and publish-snapshot resolve (the
    restart path) — must hand Store.open() the selected factory. A
    dropped **_ws_kwargs() would silently fall back to LocalExecutor
    and every other test would stay green."""
    from nontainer.executor import LocalExecutor

    class MarkedExecutor(LocalExecutor):
        pass

    monkeypatch.setattr(
        sessions_mod, "_executor_factory", lambda: lambda: MarkedExecutor()
    )
    registry = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    try:
        session = registry.create()
        assert isinstance(session.ws.runtime.executor, MarkedExecutor)
        _seed_app(session.ws)
        published = registry.publish(session.name)
        token = published["token"]
        # drop the cached snapshot so resolve takes the cold path — the
        # lazy manifest reopen a restart would take
        registry._published.pop(token, None)
        snapshot = registry.resolve(token)
        assert snapshot is not None
        assert isinstance(snapshot.runtime.executor, MarkedExecutor)
    finally:
        registry.close()


def test_dud_rung_bridges_the_db_host_object(tmp_path, monkeypatch):
    """The dud rung's version floor, enforced by exercising it.

    Every other dud test here monkeypatches the executor away, so the
    only thing that ever touched a real one was a running server — and
    the studio/nontainer/dud version triple broke there silently once
    already: nontainer 0.3 passes `allow=dud.public_methods(obj)` for
    every host object, which older duds have neither the keyword nor
    the helper for. That is a TypeError on session construction, not a
    degraded rung, and it reaches a user before it reaches CI.

    The subprocess backend, deliberately: it needs no hypervisor and no
    guest image, so it runs anywhere the extra is installed, and it
    crosses the same host-object bridge the VM rung does.
    """
    pytest.importorskip("dud", reason="the [dud] extra is optional (3.11+)")
    from nontainer import workspace

    monkeypatch.setenv("NONTAINER_STUDIO_EXECUTOR", "dud")
    db = sessions_mod.Db(tmp_path / "dbs" / "smoke.sqlite")
    ws = workspace(
        "smoke",
        store=tmp_path,
        python=sessions_mod.Registry._python_config(db),
        **sessions_mod._ws_kwargs(),
    )
    try:
        r = ws.run_python(
            "db.execute('CREATE TABLE t (v TEXT)')\n"
            "db.execute('INSERT INTO t VALUES (?)', ('from guest',))\n"
            "rows = db.query('SELECT v FROM t')"
        )
        assert r.error is None, r.error
        # Rows cross the guest boundary as JSON, so they arrive as lists
        # where the in-process rung hands back sqlite3's tuples. Compare
        # the contents, not the container.
        assert [list(row) for row in r.namespace["rows"]] == [["from guest"]]
        # The write landed in the HOST's sqlite file — the bridge is to
        # the live object, not a copy that dies with the guest.
        assert db.query("SELECT v FROM t") == [("from guest",)]
    finally:
        ws.close()


def test_failed_create_rolls_back_the_reservation(tmp_path, monkeypatch):
    """create() reserves the minted slug before open(); an open that
    fails (dud not installed, an unbuildable guest image) must not
    leave a dead rail entry that 500s on every click."""
    registry = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    try:

        def doomed_open(name):
            raise RuntimeError("image build failed")

        monkeypatch.setattr(registry, "open", doomed_open)
        with pytest.raises(RuntimeError, match="image build failed"):
            registry.create()
        assert registry.known() == set()  # reservation rolled back
    finally:
        registry.close()


# -- one AppsConfig, two lifecycles -------------------------------------------


def _custom_studio(tmp_path, apps):
    registry = sessions_mod.Registry(
        model_factory=lambda *a: None, store=tmp_path, apps=apps
    )
    registry._build_agent = lambda *a, **k: FakeAgent()
    return registry


def test_one_appsconfig_reaches_authoring_and_serving(tmp_path):
    """The declaration governs two lifecycles: authoring (test_app's
    interception, the agent's tool description) and serving (the CSP a
    published snapshot carries). Studio used to build one at each site
    and let both fall through to defaults, so they agreed by luck — and
    customizing either alone was an app that verifies green and breaks
    published."""
    from nontainer.apps import DEFAULT_SCRIPT_HOSTS, AppsConfig

    apps = AppsConfig(script_hosts=(*DEFAULT_SCRIPT_HOSTS, "esm.corp.internal"))
    registry = _custom_studio(tmp_path, apps)
    try:
        with TestClient(server.build_app(registry)) as client:
            client.post("/api/sessions", json={"name": "s1"})
            session = registry.get("s1")

            # authoring: the runtime the agent's tools drive
            assert session.runtime.config is apps

            # serving: the published snapshot's CSP derives from the
            # SAME script_hosts, not from a second, default config
            _seed_app(session.ws)
            pub = client.post("/api/sessions/s1/publish").json()
            csp = client.get(pub["url"]).headers["content-security-policy"]
            assert "https://esm.corp.internal" in csp
    finally:
        registry.close()


def test_the_default_studio_still_serves_the_library_csp(studio):
    """The shared object must not change the default policy — including
    'wasm-unsafe-eval', which a vendored library with a wasm core needs
    and which test_app cannot catch the absence of."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)
    pub = client.post("/api/sessions/s1/publish").json()

    csp = client.get(pub["url"]).headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'" in csp
    assert "https://esm.sh" in csp


# -- vendored browser libraries (the air-gap floor) ---------------------------


def test_vendored_assets_serve_to_preview_and_publish(studio):
    """plotly and tailwind come from the app's own origin, so an agent
    with no internet still gets a chart that renders."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    _seed_app(registry.get("s1").ws)

    for path in ("vendor/plotly.min.js", "vendor/tailwind.js"):
        r = client.get(f"/preview/s1/{path}")
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/javascript")
        assert len(r.content) > 100_000  # a real bundle, not a stub

    # ... and the same bytes survive publishing, since the router serves
    # under the same AppsConfig the session was built with
    pub = client.post("/api/sessions/s1/publish").json()
    assert client.get(f"{pub['url']}vendor/plotly.min.js").status_code == 200


def test_vendored_assets_stay_out_of_the_workspace(studio):
    """They are served, not stored: nothing enters the agent's
    filesystem, so they cost nothing in commits, forks, or a guest
    tree."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws
    _seed_app(ws)
    assert client.get("/preview/s1/vendor/plotly.min.js").status_code == 200

    assert not ws.files.fs.exists("/workspace/app/vendor")
    files = client.get("/api/sessions/s1/files").json()["files"]
    assert not any("vendor/" in f for f in files)


def test_the_agent_is_told_what_it_actually_has(studio):
    """static_assets puts the bytes in place; frontend_notes says they
    exist. A library the agent isn't told about may as well not be
    here — and nontainer's default block would name CDNs instead."""
    from nontainer.adapters.render import apps_notes

    client, registry = studio
    notes = apps_notes(registry.apps)
    assert "vendor/plotly.min.js" in notes
    assert "vendor/tailwind.js" in notes
    assert "esm.sh/preact" not in notes
    assert "cdn.jsdelivr.net/npm/plotly" not in notes


def test_shipped_skills_reference_no_cdn():
    """The reference files are what the agent copies. One CDN url here
    is an app that renders for us and breaks air-gapped -- the exact
    failure vendoring exists to remove."""
    import re
    from pathlib import Path

    skills = Path(__file__).parent.parent / "skills"
    offenders = []
    for path in skills.rglob("*"):
        if not path.is_file():
            continue
        for m in re.finditer(r"https?://[^\s\"'<>)]+", path.read_text()):
            offenders.append(f"{path.relative_to(skills)}: {m.group()}")
    assert not offenders, "external urls in shipped skills:\n" + "\n".join(offenders)


def test_vendored_stack_actually_runs_in_a_browser(studio):
    """Serving the bytes is not the claim -- rendering without a network
    is. Drives the real thing through test_app: plotly draws a trace and
    tailwind compiles a utility class, both from vendor/."""
    pytest.importorskip("playwright")

    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.ws.files.fs.makedirs("/workspace/app", exist_ok=True)
    session.ws.files.fs.write(
        "/workspace/app/index.html",
        b"""<!doctype html>
<html><head>
  <script src="vendor/tailwind.js"></script>
  <script src="vendor/plotly.min.js"></script>
</head><body class="bg-gray-50">
<div id="chart"></div><div id="status">init</div>
<script>
Plotly.react('chart', [{x:[1,2,3], y:[2,4,8], type:'scatter'}], {})
  .then(() => { document.getElementById('status').textContent =
      'plotly ' + Plotly.version; });
</script>
</body></html>""",
    )
    session.ws.commit()

    result = session.runtime.test_app(
        [
            {"assert": "document.querySelectorAll('#chart .trace').length > 0"},
            {"read": "#status"},
            {"eval": "getComputedStyle(document.body).backgroundColor"},
        ]
    )
    if result.load_error and "unavailable" in result.load_error:
        pytest.skip(result.load_error)  # no chromium

    assert result.ok, result
    assert result.results[1].value == "plotly 3.7.0"  # the pinned version
    assert "249, 250, 251" in result.results[2].value  # tailwind compiled it
    assert not result.rejected  # nothing tried to reach a CDN


def test_a_custom_csp_reaches_verification_not_just_serving(tmp_path, monkeypatch):
    """NONTAINER_STUDIO_CSP used to be passed to build_router only, so
    test_app verified under the DERIVED policy while the router served
    this one. An app could pass verification and be refused published --
    the divergence the single config exists to prevent."""
    monkeypatch.setenv("NONTAINER_STUDIO_CSP", "default-src 'self'; script-src 'self'")
    registry = _custom_studio(tmp_path, sessions_mod.apps_config())
    try:
        with TestClient(server.build_app(registry)) as client:
            client.post("/api/sessions", json={"name": "s1"})
            session = registry.get("s1")
            _seed_app(session.ws)

            # the authoring runtime -- what test_app enforces
            assert session.runtime.config.csp == "default-src 'self'; script-src 'self'"

            # ... and the served snapshot carries the same string
            pub = client.post("/api/sessions/s1/publish").json()
            served = client.get(pub["url"]).headers["content-security-policy"]
            assert served == "default-src 'self'; script-src 'self'"
    finally:
        registry.close()


def test_csp_none_disables_it_on_both_halves(tmp_path, monkeypatch):
    monkeypatch.setenv("NONTAINER_STUDIO_CSP", "none")
    registry = _custom_studio(tmp_path, sessions_mod.apps_config())
    try:
        with TestClient(server.build_app(registry)) as client:
            client.post("/api/sessions", json={"name": "s1"})
            _seed_app(registry.get("s1").ws)
            assert registry.apps.csp == ""
            pub = client.post("/api/sessions/s1/publish").json()
            assert "content-security-policy" not in client.get(pub["url"]).headers
    finally:
        registry.close()


REFERENCE_HANDLER = b"""
ROWS = [
    {"id": 1, "category": "a", "region": "north", "year": 2020, "value": 1.0},
    {"id": 2, "category": "b", "region": "south", "year": 2021, "value": 3.0},
]


def get(req):
    category = req.params.get("category") or ""
    kept = [r for r in ROWS if not category or r["category"] == category]
    return {
        "options": {"category": ["a", "b"], "region": ["north", "south"]},
        "total": len(kept),
        "mean_value": (sum(r["value"] for r in kept) / len(kept)) if kept else None,
        "chart": {"x": [r["year"] for r in kept], "y": [r["value"] for r in kept]},
        "rows": kept,
    }
"""


def _reference_app(studio):
    """The reference set, copied verbatim into a session the way an agent
    would, over a stand-in for api-handler.py that needs no parquet."""
    refs = Path(__file__).parent.parent / "skills" / "building-apps" / "references"
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws
    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.files.fs.write("/workspace/app/index.html", (refs / "app.html").read_bytes())
    ws.files.fs.write("/workspace/app/app.jsx", (refs / "app.jsx").read_bytes())
    ws.files.fs.write("/workspace/app/api/summary.py", REFERENCE_HANDLER)
    ws.commit()
    return registry.get("s1")


def test_the_reference_app_actually_runs(studio):
    """The reference files are what an agent COPIES, so they have to work
    verbatim: the import map resolving bare specifiers, JSX compiled in
    the browser, a fetch into a real handler, a chart drawn, a filter
    that refetches, and a dialog that opens. Every failure found while
    building this stack was in exactly these seams (CJS exports, a bare
    `react-dom` import, esbuild's require shim).

    The filter step is the one that would otherwise rot silently: MUI's
    default Select is a div plus a popover, which test_app's `select`
    action cannot drive at all. The reference passes
    `SelectProps={{ native: true }}` precisely so this works, and
    nothing but driving it would notice if that were dropped."""
    pytest.importorskip("playwright")
    session = _reference_app(studio)

    result = session.runtime.test_app(
        [
            {"assert": "document.querySelectorAll('#rows tbody tr').length === 2"},
            {"assert": "document.querySelector('#total').textContent === '2'"},
            # Plotly drew into the ref'd Box, not into a detached node.
            {"assert": "document.querySelector('#chart .plot-container') !== null"},
            # A native <select>, so this drives the real control.
            {"select": ["#f-category", "a"]},
            {"assert": "document.querySelectorAll('#rows tbody tr').length === 1"},
            {"assert": "document.querySelector('#total').textContent === '1'"},
            # ...and the dialog still binds the row it was opened from.
            {"click": "#open-1"},
            {"assert": "document.querySelector('.MuiDialog-root') !== null"},
            {"eval": "document.querySelector('#note').value"},
        ]
    )
    if result.load_error and "unavailable" in result.load_error:
        pytest.skip(result.load_error)

    assert result.ok, render_test_app(result)
    assert result.results[-1].value == "'a'"  # fetched, rendered, and bound
    assert not result.rejected  # nothing reached for a CDN


def test_the_reference_app_wears_the_shell_palette(studio):
    """The bug this closes: the reference read --app-primary and
    --app-color-scheme, and nothing in the stack set either — so the file
    an agent copies quietly rendered stock Material purple on white.
    Asserting the COMPUTED colour is the only check that would have
    caught it; the source looked correct."""
    pytest.importorskip("playwright")
    session = _reference_app(studio)

    result = session.runtime.test_app(
        [
            # The palette reached the page...
            {
                "assert": "getComputedStyle(document.documentElement)"
                ".getPropertyValue('--app-primary').trim() === '#e94560'"
            },
            # ...and the theme built from it reached MUI. rgb(), because
            # that is how a browser reports a resolved colour.
            {
                "assert": "getComputedStyle(document.querySelector('#reset'))"
                ".color === 'rgb(233, 69, 96)'"
            },
            # CssBaseline painted the shell's background, not white.
            {
                "assert": "getComputedStyle(document.body)"
                ".backgroundColor === 'rgb(26, 26, 46)'"
            },
            # And plotly is transparent rather than its default white
            # paper, which on a dark page is the most visible mismatch
            # there is.
            {
                "assert": "document.querySelector('#chart .main-svg')"
                ".style.backgroundColor === 'rgba(0, 0, 0, 0)'"
            },
        ]
    )
    if result.load_error and "unavailable" in result.load_error:
        pytest.skip(result.load_error)
    assert result.ok, render_test_app(result)


def _jsx_app(ws, html: bytes, jsx: bytes):
    ws.files.fs.makedirs("/workspace/app", exist_ok=True)
    ws.files.fs.write("/workspace/app/index.html", html)
    ws.files.fs.write("/workspace/app/app.jsx", jsx)
    ws.commit()


BARE_IMPORT_JSX = b"""import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Button } from '@mui/material';

function App() {
  const [n, setN] = useState(0);
  return <Button id="b" onClick={() => setN(n + 1)}>count {n}</Button>;
}
createRoot(document.getElementById('root')).render(<App />);
"""


def test_an_agent_written_page_needs_no_import_map(studio):
    """The map is machinery an agent would otherwise have to reproduce
    in every app, and an app whose html lacks it fails on the first
    import with an error about specifiers rather than about the thing
    the agent got wrong. The loader supplies it."""
    pytest.importorskip("playwright")
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _jsx_app(
        session.ws,
        b"""<html><body><div id="root"></div>
<script type="module" src="vendor/jsx-loader.js" data-app="app.jsx"></script>
</body></html>""",
        BARE_IMPORT_JSX,
    )
    result = session.runtime.test_app(
        [
            {"click": "#b"},
            {"assert": "document.querySelector('#b').textContent === 'count 1'"},
        ]
    )
    if result.load_error and "unavailable" in result.load_error:
        pytest.skip(result.load_error)
    assert result.ok, render_test_app(result)


def test_a_page_that_declares_its_own_map_wins(studio):
    """An agent extending the set with its own entry should win -- and
    older engines allow only one map, so injecting a second would break
    the page rather than help it."""
    pytest.importorskip("playwright")
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _jsx_app(
        session.ws,
        b"""<html><head><script type="importmap">
{"imports": {"react": "./vendor/react.min.js",
             "react/jsx-runtime": "./vendor/react.min.js",
             "react-dom/client": "./vendor/react.min.js",
             "react-dom": "./vendor/react.min.js",
             "@mui/material": "./vendor/mui.min.js",
             "house/theme": "./vendor/react.min.js"}}
</script></head><body><div id="root"></div>
<script type="module" src="vendor/jsx-loader.js" data-app="app.jsx"></script>
</body></html>""",
        b"import { Fragment as HouseFragment } from 'house/theme';\n"
        b"if (!HouseFragment) throw new Error('house entry did not resolve');\n"
        + BARE_IMPORT_JSX,
    )
    result = session.runtime.test_app(
        [{"assert": "document.querySelectorAll('script[type=importmap]').length === 1"}]
    )
    if result.load_error and "unavailable" in result.load_error:
        pytest.skip(result.load_error)
    assert result.ok, render_test_app(result)


def test_the_vendored_react_exports_its_whole_public_surface():
    """The export list is generated from the module, not written by
    hand. Hand-listing silently omitted React 19's `use`,
    `useActionState`, `useOptimistic` and `useEffectEvent` — an app
    importing one of those failed at module instantiation, even though
    the function was sitting on the bundled React object.

    Static on purpose: it needs neither a browser nor node, so it guards
    the bundle on every run and will fail the next time React adds an
    API the generator does not pick up."""
    import re

    bundle = (
        Path(__file__).parent.parent / "nontainer_studio" / "appassets" / "react.min.js"
    ).read_text()
    match = re.search(r"export\{([^}]*)\}", bundle)
    assert match, "react.min.js exports nothing — `export *` from CJS again?"
    names = {part.split(" as ")[-1].strip() for part in match.group(1).split(",")}

    expected = {
        # hooks a model reaches for, old and new
        "useState",
        "useEffect",
        "useMemo",
        "useRef",
        "useReducer",
        "use",
        "useActionState",
        "useOptimistic",
        "useTransition",
        # composition
        "Fragment",
        "StrictMode",
        "Suspense",
        "createContext",
        "memo",
        "forwardRef",
        "lazy",
        "startTransition",
        # dom + the jsx runtime the loader compiles against
        "createRoot",
        "hydrateRoot",
        "createPortal",
        "flushSync",
        "jsx",
        "jsxs",
    }
    assert not (expected - names), f"missing from the bundle: {expected - names}"
    # A hand-list drifts DOWN; this catches that shape without pinning a
    # number that a React release would have to chase.
    assert len(names) >= 50, f"only {len(names)} exports — did the generator run?"


# -- the house theme (one palette, two frontends) -----------------------------

# app-facing property -> the shell's own custom property it copies.
# frontend/src/app.css is the source; appassets/theme.css is the copy an
# agent's app sees. The names differ on purpose (the shell stays free to
# rename its internals), which is exactly why they need pairing.
_PALETTE_PAIRS = {
    "--app-background": "--bg",
    "--app-surface": "--surface",
    "--app-surface-hover": "--surface-hover",
    "--app-border": "--border",
    "--app-text": "--text",
    "--app-text-muted": "--text-muted",
    "--app-link": "--link",
    "--app-primary": "--accent",
    "--app-secondary": "--purple",
    "--app-success": "--success",
    "--app-warning": "--warning",
    "--app-error": "--error",
}


def _root_properties(path: Path) -> dict[str, str]:
    """Custom properties declared in the FIRST `:root {...}` block."""
    import re

    block = re.search(r":root\s*\{(.*?)\}", path.read_text(), re.S)
    assert block, f"no :root block in {path}"
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block.group(1))
    }


def test_the_app_palette_still_matches_the_shell():
    """A palette stated in two places and updated in one is the whole
    reason this pairing exists. theme.css is a hand-copy of the shell's
    :root block under app-facing names; nothing else would notice it
    going stale, because an app with last quarter's accent colour still
    renders perfectly."""
    root = Path(__file__).parent.parent
    shell = _root_properties(root / "frontend" / "src" / "app.css")
    app = _root_properties(root / "nontainer_studio" / "appassets" / "theme.css")

    drifted = {
        app_name: (app.get(app_name), shell.get(shell_name))
        for app_name, shell_name in _PALETTE_PAIRS.items()
        if app.get(app_name) != shell.get(shell_name)
    }
    assert not drifted, (
        "theme.css no longer matches frontend/src/app.css "
        f"(app value, shell value): {drifted}"
    )


def test_theme_assets_serve_to_preview_and_publish(studio):
    """Every file the import map points at is real on BOTH lifecycles.
    A map entry naming a file that only exists in preview would verify
    green and 404 once published — the split this config exists to
    close."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws
    ws.files.fs.makedirs("/workspace/app", exist_ok=True)
    ws.files.fs.write("/workspace/app/index.html", b"<h1>hi</h1>")
    ws.commit()

    mapped = (
        "vendor/theme.css",
        "vendor/theme.js",
        "vendor/icons.min.js",
        "vendor/mui-utils.js",
        "vendor/mui.min.js",
    )
    for path in mapped:
        assert client.get(f"/preview/s1/{path}").status_code == 200, path

    pub = client.post("/api/sessions/s1/publish").json()
    for path in mapped:
        assert client.get(f"{pub['url']}{path}").status_code == 200, path


def test_the_notes_name_both_theme_spellings(studio):
    """A vendored theme the agent is never told about is a file nobody
    imports."""
    _, registry = studio
    notes = registry.apps.frontend_notes
    assert "house/theme" in notes  # the React spelling
    assert "vendor/theme.css" in notes  # the plain-DOM one


def test_the_reference_handler_survives_nulls(studio):
    """api-handler.py is a working file too, and its own subject is data
    that breaks JSON. Nothing enforces this: a bare NaN goes out as a
    200 and only fails in the browser, so the reference shipping one
    would look fine from every angle except a rendered page.

    The frame below is the shape that catches it — a null in a STRING
    column, not just the numeric one. That is what the first draft of
    the rows block got wrong."""
    pytest.importorskip("pandas")
    refs = Path(__file__).parent.parent / "skills" / "building-apps" / "references"
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    ws = session.ws
    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.files.fs.makedirs("/workspace/app/data", exist_ok=True)
    ws.run_python(
        "import pandas as pd\n"
        'pd.DataFrame({"category": ["a", "b", None],\n'
        '              "region": ["north", None, "south"],\n'
        '              "year": [2020, 2021, 2022],\n'
        '              "value": [1.0, None, None]}\n'
        ').to_parquet("/workspace/app/data/records.parquet")\n'
    )
    ws.files.fs.write(
        "/workspace/app/api/summary.py", (refs / "api-handler.py").read_bytes()
    )

    response = session.runtime.dispatch(nt_request("GET", "/api/summary"))
    assert response.status == 200, response.text

    # parse_constant, because Python's json.loads ACCEPTS bare NaN and
    # the browser's JSON.parse does not. A plain json.loads here would
    # sail straight past the very thing this test exists to catch.
    def reject(constant):
        raise AssertionError(f"response carries a bare {constant} — not JSON")

    body = json.loads(response.text, parse_constant=reject)
    assert body["rows"][2]["category"] is None
    assert body["rows"][1]["region"] is None
    assert body["rows"][1]["value"] is None


# -- the MUI bundle's declared surface ----------------------------------------


def _module_exports(path: Path) -> set[str]:
    names: set[str] = set()
    for group in re.findall(r"export\{([^}]*)\}", path.read_text()):
        for part in group.split(","):
            names.add(part.split(" as ")[-1].strip())
    return names


def test_the_skill_lists_exactly_the_icons_that_exist():
    """Curating the icon set is what keeps it at 14 KB instead of 4.3 MB,
    and the cost is that a name outside the set fails. So the list is
    part of the contract: the skill prints it, and an agent picks from
    it rather than from memory. A list that drifts from the bundle is
    worse than no list — it would send the agent at a name that isn't
    there, with the skill's own authority behind it."""
    root = Path(__file__).parent.parent
    bundled = _module_exports(root / "nontainer_studio" / "appassets" / "icons.min.js")
    skill = (root / "skills" / "building-apps" / "SKILL.md").read_text()

    block = re.search(r"```\n(Add ArrowBack.*?)\n```", skill, re.S)
    assert block, "the icon manifest is gone from SKILL.md"
    listed = set(block.group(1).split())

    assert listed == bundled, (
        f"only in the skill: {sorted(listed - bundled)}; "
        f"only in the bundle: {sorted(bundled - listed)}"
    )


def test_the_icon_bundle_needs_only_what_the_import_map_answers():
    """icons.min.js is built with @mui/material/utils external so there
    stays ONE MUI instance. If an icons upgrade started importing a
    second subpath, the import map would not resolve it and every app
    using an icon would die on a specifier error — after verifying
    green here, because nothing else looks at this seam."""
    assets = Path(__file__).parent.parent / "nontainer_studio" / "appassets"
    needed = set(
        re.findall(r'from"([^"./][^"]*)"', (assets / "icons.min.js").read_text())
    )
    loader = (assets / "jsx-loader.js").read_text()
    answered = set(re.findall(r'"([^"]+)":\s*"\./vendor/', loader))
    # react/jsx-runtime is in the map; @mui/material/utils must be too.
    assert needed <= answered, f"unmapped specifiers: {sorted(needed - answered)}"
    assert "@mui/material/utils" in needed  # the shim is load-bearing, not vestigial


def test_the_grid_shares_one_mui_instance(studio):
    """The reason the grid is in mui.min.js rather than its own bundle.
    A second copy of MUI carries a second @mui/private-theming context,
    so <ThemeProvider> would theme everything EXCEPT the grid — which
    looks like a styling bug, not a bundling one. Asserting a themed
    colour inside the grid is what distinguishes the two."""
    pytest.importorskip("playwright")
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _jsx_app(
        session.ws,
        b"""<html><body><div id="root"></div>
<script type="module" src="vendor/jsx-loader.js" data-app="app.jsx"></script>
</body></html>""",
        b"""import { createRoot } from 'react-dom/client';
import { Box, CssBaseline, ThemeProvider } from '@mui/material';
import { DataGrid } from '@mui/x-data-grid';
import { Delete, Search } from '@mui/icons-material';
import theme from 'house/theme';

const rows = [{ id: 1, name: 'a' }, { id: 2, name: 'b' }];
const columns = [{ field: 'name', headerName: 'name', width: 120 }];

createRoot(document.getElementById('root')).render(
  <ThemeProvider theme={theme}>
    <CssBaseline />
    <Box id="icons"><Delete /><Search /></Box>
    <Box sx={{ height: 300 }}><DataGrid rows={rows} columns={columns} /></Box>
  </ThemeProvider>,
);
""",
    )
    result = session.runtime.test_app(
        [
            {"assert": "document.querySelectorAll('#icons svg').length === 2"},
            {"assert": "document.querySelectorAll('.MuiDataGrid-row').length === 2"},
            # The grid resolved OUR theme rather than a default of its
            # own. Both values are chosen because stock MUI disagrees:
            # its dark text.primary is #fff (ours is --app-text) and its
            # shape.borderRadius is 4 (ours is 8). A second MUI instance
            # would render legibly here and fail both.
            {
                "assert": "getComputedStyle(document.querySelector('.MuiDataGrid-root'))"
                ".color === 'rgb(224, 224, 224)'"
            },
            {
                "assert": "getComputedStyle(document.querySelector('.MuiDataGrid-root'))"
                ".borderRadius === '8px'"
            },
        ]
    )
    if result.load_error and "unavailable" in result.load_error:
        pytest.skip(result.load_error)
    assert result.ok, render_test_app(result)


def test_the_reference_handler_survives_a_null_year(studio):
    """`year` looks like a safe int right up until a sampled row is
    missing one, and then int(NaN) raises and the whole summary 500s on
    data that is merely incomplete.

    The aggregate never showed it: groupby drops null keys silently, so
    the chart renders fine while the table request dies. Worth its own
    test because it fails DIFFERENTLY from the string columns — those
    ship a bare NaN and blank the page; this one is a 500."""
    pytest.importorskip("pandas")
    refs = Path(__file__).parent.parent / "skills" / "building-apps" / "references"
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    ws = session.ws
    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.files.fs.makedirs("/workspace/app/data", exist_ok=True)
    ws.run_python(
        "import pandas as pd\n"
        'pd.DataFrame({"category": ["a", "b"], "region": ["north", "south"],\n'
        '              "year": [2020, None], "value": [1.0, 3.0]}\n'
        ').to_parquet("/workspace/app/data/records.parquet")\n'
    )
    ws.files.fs.write(
        "/workspace/app/api/summary.py", (refs / "api-handler.py").read_bytes()
    )

    response = session.runtime.dispatch(nt_request("GET", "/api/summary"))
    assert response.status == 200, response.text
    body = json.loads(response.text)
    assert body["rows"][1]["year"] is None
    assert body["rows"][0]["year"] == 2020  # a real year still casts to int


def test_the_loader_does_not_mistake_another_stylesheet_for_the_house_one(studio):
    """The skip-if-already-linked check matches the house stylesheet by
    URL, not by the substring "theme.css". An app that links its own
    `custom-theme.css` would otherwise never receive vendor/theme.css,
    and the failure is SILENT: theme.js reads every --app-* property as
    "" and quietly hands back a stock MUI theme."""
    pytest.importorskip("playwright")
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.ws.files.fs.makedirs("/workspace/app", exist_ok=True)
    session.ws.files.fs.write(
        "/workspace/app/custom-theme.css", b".mine { color: red }\n"
    )
    _jsx_app(
        session.ws,
        b"""<html><head>
<link rel="stylesheet" href="custom-theme.css" />
</head><body><div id="root"></div>
<script type="module" src="vendor/jsx-loader.js" data-app="app.jsx"></script>
</body></html>""",
        b"""import { createRoot } from 'react-dom/client';
import { Button, CssBaseline, ThemeProvider } from '@mui/material';
import theme from 'house/theme';
createRoot(document.getElementById('root')).render(
  <ThemeProvider theme={theme}><CssBaseline />
    <Button id="b">hi</Button>
  </ThemeProvider>);
""",
    )
    result = session.runtime.test_app(
        [
            # both stylesheets present: the app's own AND ours
            {
                "assert": """document.querySelectorAll(
                    "link[href$='theme.css']").length === 2"""
            },
            {
                "assert": "getComputedStyle(document.documentElement)"
                ".getPropertyValue('--app-primary').trim() === '#e94560'"
            },
            # stock MUI would be rgb(144, 202, 249) here
            {
                "assert": "getComputedStyle(document.querySelector('#b'))"
                ".color === 'rgb(233, 69, 96)'"
            },
        ]
    )
    if result.load_error and "unavailable" in result.load_error:
        pytest.skip(result.load_error)
    assert result.ok, render_test_app(result)


def test_the_house_theme_survives_a_missing_stylesheet(studio):
    """Missing colours should cost the house look, not the app.

    createTheme does NOT treat an explicit `undefined` as "use your
    default" — it throws "Cannot read properties of undefined (reading
    'type')" from inside library code and renders nothing. So theme.js
    omits absent properties rather than passing them, and this drives
    that path through the real module: a reader that finds nothing is
    exactly what an app gets when theme.css never arrived."""
    pytest.importorskip("playwright")
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _jsx_app(
        session.ws,
        b"""<html><body><div id="root"></div>
<script type="module" src="vendor/jsx-loader.js" data-app="app.jsx"></script>
</body></html>""",
        b"""import { createRoot } from 'react-dom/client';
import { Button, CssBaseline, ThemeProvider } from '@mui/material';
import { createHouseTheme } from 'house/theme';

const bare = createHouseTheme(() => undefined);
createRoot(document.getElementById('root')).render(
  <ThemeProvider theme={bare}><CssBaseline />
    <Button id="b">hi</Button>
  </ThemeProvider>);
""",
    )
    result = session.runtime.test_app(
        [
            {"assert": "document.querySelector('#b') !== null"},
            # Stock MUI dark primary, i.e. it really did fall back
            # rather than somehow still finding our palette.
            {
                "assert": "getComputedStyle(document.querySelector('#b'))"
                ".color === 'rgb(144, 202, 249)'"
            },
        ]
    )
    if result.load_error and "unavailable" in result.load_error:
        pytest.skip(result.load_error)
    assert result.ok, render_test_app(result)


def test_fork_holds_the_turn_lock_and_lets_go_after(scripted):
    """The busy check is a reservation, not a snapshot: the session's
    turn lock is held for the whole fork, so a chat request that arrives
    mid-fork waits rather than committing under it."""
    client, registry = scripted
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    _run(client, "s1", _script("/workspace/a.txt", "A", "wrote a"))

    seen = {}
    original = sessions_mod.fork_session

    def spying_fork(ws, name, **kw):
        seen["locked_during_fork"] = session.turn_lock.locked()
        return original(ws, name, **kw)

    sessions_mod.fork_session = spying_fork
    try:
        child = registry.fork(session)
    finally:
        sessions_mod.fork_session = original
    assert seen["locked_during_fork"] is True
    assert not session.busy
    assert not child.busy


def test_python_config_drops_the_isolation_knob_on_a_dud_rung(tmp_path, monkeypatch):
    """The knob is the in-process sandbox's; a dud rung either exceeds
    it (a VM) or refuses it (subprocess), so the studio does not ask."""
    monkeypatch.setenv("NONTAINER_STUDIO_ISOLATION", "process")
    db = sessions_mod.Db(tmp_path / "dbs" / "x.sqlite")
    assert sessions_mod.Registry._python_config(db).isolation == "process"
    monkeypatch.setenv("NONTAINER_STUDIO_EXECUTOR", "dud")
    assert sessions_mod.Registry._python_config(db).isolation == "none"
    monkeypatch.setenv("NONTAINER_STUDIO_EXECUTOR", "dud-vm")
    assert sessions_mod.Registry._python_config(db).isolation == "none"


# -- delegates: the rail's listing, and the sweep on a timer -----------------


def _delegate(registry, parent="boss", child="boss.scout"):
    """A delegate that has finished: opened, recorded, handles
    released — what the runner leaves behind when it answers."""
    registry.open(parent)
    registry.open_delegate(parent, child)
    registry.release(child)
    return child


def test_the_route_lists_what_a_session_delegated(studio):
    """The rail's ⑂ badge counts what a turn would deliver; this is the
    listing behind it, which is everything the session forked."""
    client, registry = studio
    child = _delegate(registry)

    rows = client.get("/api/sessions/boss/delegates").json()["delegates"]

    assert [r["name"] for r in rows] == [child]
    assert rows[0]["kept"] is False
    assert rows[0]["touched"] > 0
    # and the rail row says there is something to list
    row = next(r for r in client.get("/api/sessions").json()["sessions"])
    assert (row["name"], row["delegate_count"]) == ("boss", 1)


def test_keeping_a_delegate_through_the_route_is_written_down(studio):
    """A keep has to outlive the job table it is flagged in, so the
    record is written whether or not a live job took the flag — and
    un-keeping is the record alone, since nontainer offers no un-keep."""
    client, registry = studio
    child = _delegate(registry)

    kept = client.post(f"/api/sessions/boss/delegates/{child}/keep", json={})
    assert kept.json()["delegate"]["kept"] is True
    assert registry._manifest()["delegates"][child]["kept"] is True

    freed = client.post(
        f"/api/sessions/boss/delegates/{child}/keep", json={"kept": False}
    )
    assert freed.json()["delegate"]["kept"] is False
    assert registry._manifest()["delegates"][child]["kept"] is False


def test_keeping_a_delegate_of_another_session_is_a_404(studio):
    client, registry = studio
    child = _delegate(registry)
    registry.open("other")

    res = client.post(f"/api/sessions/other/delegates/{child}/keep", json={})

    assert res.status_code == 404
    assert child in res.json()["error"]
    assert client.get("/api/sessions/other/delegates").json()["delegates"] == []


# -- the drill-down: a delegate opens, and opens read-only -------------------

# Every verb that would DRIVE a session's turns. The parent agent writes
# a delegate's prompts; a human's landing in the middle would rewrite a
# transcript the parent is still reading.
DRIVING = [
    ("chat", {"json": {"message": "do it"}}),
    ("edit", {"json": {"message": "do it", "seq": 0}}),
    ("restore", {"json": {"seq": 0}}),
    ("model", {"json": {"model": "dummy"}}),
    ("title", {"json": {"title": "mine"}}),
    ("upload?name=n.txt", {"content": b"hi"}),
    # publishing commits the delegate's open work and puts a version
    # behind a public URL — as much a turn's doing as a turn is
    ("publish", {"json": {}}),
]


def _wait_for_delegate(client, child: str, status: str, timeout: float = 20) -> dict:
    """The child's row once it reads ``status``. Polls through 404: the
    runner opens the delegate on a worker thread, so the name exists a
    beat after the ask returns."""
    deadline = time.monotonic() + timeout
    seen: object = None
    while time.monotonic() < deadline:
        res = client.get(f"/api/sessions/{child}")
        if res.status_code == 200:
            seen = res.json()["delegate"]
            if seen is not None and seen["status"] == status:
                return seen
        time.sleep(0.05)
    raise AssertionError(f"{child} never read {status!r} (last: {seen})")


def test_the_session_route_says_whose_delegate_this_is(studio):
    """A delegate has no rail row, so a shell that lands on its name has
    only the name — this is the request that tells it the rest."""
    client, registry = studio
    child = _delegate(registry)

    info = client.get(f"/api/sessions/{child}").json()

    assert info["name"] == child
    assert info["title"] and info["busy"] is False
    assert info["delegate"]["parent"] == "boss"
    assert info["delegate"]["status"] == "answered"
    assert info["delegate"]["kept"] is False
    assert info["delegate"]["touched"] > 0
    # an ordinary session is nobody's
    assert client.get("/api/sessions/boss").json()["delegate"] is None
    assert client.get("/api/sessions/nobody").status_code == 404


def test_a_human_cannot_drive_a_delegate(studio):
    client, registry = studio
    child = _delegate(registry)
    refusal = f"{child} is a delegate of boss: the parent drives it"

    for verb, kw in DRIVING:
        res = client.post(f"/api/sessions/{child}/{verb}", **kw)
        assert res.status_code == 409, verb
        assert res.json()["error"] == refusal, verb

    # reading is the whole point of opening one
    assert client.get(f"/api/sessions/{child}/events?wait=0").status_code == 200
    assert client.get(f"/api/sessions/{child}/files").status_code == 200
    assert client.get(f"/api/sessions/{child}/delegates").status_code == 200
    # and forking it is how you take the branch as your own session
    taken = client.post(f"/api/sessions/{child}/fork", json={})
    assert taken.status_code == 200
    assert registry.parent_of(taken.json()["name"]) is None


def test_an_ordinary_session_still_takes_every_verb(studio):
    """The refusal is about the delegates record, not about the routes:
    a session nobody forked answers each of them for itself."""
    client, _ = studio
    client.post("/api/sessions", json={"name": "plain"})

    assert (
        client.post("/api/sessions/plain/title", json={"title": "mine"}).status_code
        == 200
    )
    assert (
        client.post("/api/sessions/plain/upload?name=n.txt", content=b"hi").status_code
        == 200
    )
    # these say no for their own reasons — nothing to edit, nothing to
    # restore to, no model named — which is the point: the refusal is
    # not what answered
    for verb, kw in (
        ("edit", {"json": {"message": "x", "seq": 0}}),
        ("restore", {"json": {"seq": 0}}),
        ("model", {"json": {}}),
    ):
        assert client.post(f"/api/sessions/plain/{verb}", **kw).status_code == 400, verb
    assert (
        client.post("/api/sessions/plain/chat", json={"message": "go"}).status_code
        == 200
    )


def test_an_ordinary_session_still_publishes(studio):
    """The other half of the publish refusal: the route is not broken,
    it is scoped."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "plain"})
    _seed_app(registry.get("plain").ws)

    made = client.post("/api/sessions/plain/publish", json={})

    assert made.status_code == 200
    assert made.json()["url"].startswith("/apps/")


def test_the_session_route_follows_a_running_delegate(tmp_path):
    """A delegate's status is its parent's job table's, and nothing
    about it lands on the delegate's own event feed — so the row has to
    be askable again rather than read once."""
    gate = threading.Event()
    registry = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    registry._build_agent = lambda name, *a, **k: (
        FakeAgent() if name == "boss" else GatedAgent(gate)
    )
    try:
        with TestClient(server.build_app(registry)) as client:
            parent = registry.open("boss")
            job = parent.delegates.ask("go and look")

            running = _wait_for_delegate(client, job.name, "running")
            assert running["parent"] == "boss" and running["known"] is True

            gate.set()
            answered = _wait_for_delegate(client, job.name, "answered")
            assert answered["known"] is True
    finally:
        gate.set()
        registry.close()


def test_a_delegate_has_delegates_of_its_own(studio):
    """Delegation nests and so does the listing: the record is keyed on
    whoever forked, not on the sessions the rail happens to show."""
    client, registry = studio
    _delegate(registry)
    grandchild = _delegate(registry, parent="boss.scout", child="boss.scout.finch")

    rows = client.get("/api/sessions/boss.scout/delegates").json()["delegates"]

    assert [r["name"] for r in rows] == [grandchild]
    # each level names its own parent, which is what the breadcrumb walks
    info = client.get(f"/api/sessions/{grandchild}").json()
    assert info["delegate"]["parent"] == "boss.scout"
    assert client.get(f"/api/sessions/{grandchild}/delegates").json()["delegates"] == []


def test_opening_a_delegate_after_a_restart_leaves_it_one(tmp_path):
    """The job table dies with the process; the record is what says a
    session is somebody's delegate. The shell's create-or-resume POST
    opens the name like any other — and must not promote it."""
    registry = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    registry._build_agent = lambda *a, **k: FakeAgent()
    try:
        with TestClient(server.build_app(registry)):
            child = _delegate(registry)
    finally:
        registry.close()

    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    try:
        with TestClient(server.build_app(reborn)) as client:
            assert client.post("/api/sessions", json={"name": child}).status_code == 200
            assert client.get(f"/api/sessions/{child}").json()["delegate"] == {
                "name": child,
                "parent": "boss",
                "status": "answered",
                "known": False,
                "kept": False,
                "touched": pytest.approx(
                    reborn._manifest()["delegates"][child]["touched"]
                ),
            }
            assert [
                r["name"] for r in client.get("/api/sessions").json()["sessions"]
            ] == ["boss"]
            assert (
                client.post(
                    f"/api/sessions/{child}/chat", json={"message": "x"}
                ).status_code
                == 409
            )
    finally:
        reborn.close()


def test_the_server_sweeps_on_a_timer(tmp_path, monkeypatch):
    """Retention is a verb somebody schedules, and the server is what
    schedules it while the studio is up: the registry sweeps once at
    open, and this loop from then on."""
    monkeypatch.setattr(server, "DELEGATE_SWEEP_EVERY", 0.05)
    registry = sessions_mod.Registry(
        model_factory=lambda *a: None, store=tmp_path, delegate_ttl=1e-9
    )
    registry._build_agent = lambda *a, **k: FakeAgent()
    try:
        with TestClient(server.build_app(registry)):
            child = _delegate(registry)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if child not in registry._store.sessions():
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("the timer never swept")
        assert registry._manifest()["delegates"] == {}
    finally:
        registry.close()


def test_no_ttl_no_timer(tmp_path, monkeypatch):
    """`0` is retention off. Nothing is scheduled, so a delegate nobody
    has touched since the store was made is still there."""
    monkeypatch.setattr(server, "DELEGATE_SWEEP_EVERY", 0.05)
    registry = sessions_mod.Registry(
        model_factory=lambda *a: None, store=tmp_path, delegate_ttl=0
    )
    registry._build_agent = lambda *a, **k: FakeAgent()
    try:
        with TestClient(server.build_app(registry)):
            child = _delegate(registry)
            time.sleep(0.3)
            assert child in registry._store.sessions()
    finally:
        registry.close()
