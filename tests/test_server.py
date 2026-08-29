"""Server plumbing — background turns + event-log transcript, session
lifecycle, preview/publish, time travel — exercised with a fake agent
(no LLM, no key)."""

import json
import re
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
    ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.fs.write(
        "/workspace/app/index.html", b"<html><body><h1>counter</h1></body></html>"
    )
    ws.fs.write("/workspace/app/api/count.py", HANDLER.encode())
    ws.checkpoint()


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
    session.ws.write_file("/workspace/skills/building-apps/SKILL.md", "agent-edited")
    registry.close()
    registry._sessions.clear()
    session2 = registry.open("s1")
    assert (
        session2.ws.fs.read("/workspace/skills/building-apps/SKILL.md")
        == b"agent-edited"
    )


def test_seeded_skill_teaches_curl_only_where_it_exists(studio):
    """The apps `curl` builtin is a LocalExecutor affordance. Under any
    dud backend the terminal is real bash, where `curl api/x` reaches
    the NETWORK instead of the dispatcher — it fails open, silently, so
    the seeded text has to be gated rather than merely softened.

    In the audited session this was half of a compound failure: the
    log rung was invisible (fixed in nontainer) and this rung was
    actively misleading, which left the documented debugging ladder
    with no working step at all under dud-vm.
    """
    client, registry = studio
    client.post("/api/sessions", json={"name": "sk"})
    session = registry.get("sk")
    text = session.ws.fs.read("/workspace/skills/building-apps/SKILL.md").decode()

    # the studio fixture runs the default (Local) executor
    assert session.ws.supports_commands
    assert "curl api/x" in text
    assert "There is no `curl` builtin" not in text
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
            {"name": "s1", "title": "New session", "busy": True, "model": None}
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
    _seed_app(registry.get("s1").ws)
    client.post("/preview/s1/api/count")  # live state: n=1

    pub = client.post("/api/sessions/s1/publish").json()
    assert pub["url"].startswith("/apps/") and pub["checkpoint"]

    # the snapshot serves, read-only: GET works, VFS/cache mutation 500s
    assert client.get(pub["url"]).status_code == 200
    assert client.get(f"{pub['url']}api/count").json() == {"n": 1}
    assert client.post(f"{pub['url']}api/count").status_code == 500

    # the live session keeps moving; the snapshot doesn't
    client.post("/preview/s1/api/count")  # live n=2
    assert client.get("/preview/s1/api/count").json() == {"n": 2}
    assert client.get(f"{pub['url']}api/count").json() == {"n": 1}


def test_published_app_shares_live_db(studio):
    """Frozen code, live state: the published snapshot's handlers call
    the SAME db as the authoring session (the fork carries host_objects,
    and the worker reaches the real one over the RPC bridge)."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    session.ws.fs.write(
        "/workspace/app/api/names.py",
        b"def get(req):\n"
        b'    db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")\n'
        b"    return {'names': [r[0] for r in db.query('SELECT v FROM t')]}\n",
    )
    session.ws.checkpoint()
    pub = client.post("/api/sessions/s1/publish").json()

    session.db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    session.db.execute("INSERT INTO t VALUES ('amy')")
    # the FROZEN app sees the post-publish db write — live state
    assert client.get(f"{pub['url']}api/names").json() == {"names": ["amy"]}


# -- files ----------------------------------------------------------------------


def test_files_tree_and_raw(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws
    ws.fs.makedirs("/workspace/app/screenshots", exist_ok=True)
    ws.fs.write("/workspace/notes.md", b"# hi")
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
        "7753de0000000c49444154089963f8cfc000000301010018dd8db0000000"
        "0049454e44ae426082"
    )
    ws.fs.write("/workspace/app/screenshots/shot-1.png", png)

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


def test_upload_lands_checkpointed_with_notice(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")

    r = client.post("/api/sessions/s1/upload?name=data.csv", content=b"a,b\n1,2\n")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "path": "/workspace/uploads/data.csv", "size": 8}
    assert session.ws.fs.read("/workspace/uploads/data.csv") == b"a,b\n1,2\n"
    # checkpointed: an edit's rewind extends to uploads
    assert any(c.info.get("tool") == "file_write" for c in session.ws.history(limit=3))
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
    before = len(list(session.ws.history()))

    def up(i: int):
        return client.post(
            f"/api/sessions/s1/upload?name=f{i}.txt", content=f"file {i}".encode()
        ).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(up, range(6)))
    assert codes == [200] * 6
    for i in range(6):
        assert (
            session.ws.fs.read(f"/workspace/uploads/f{i}.txt") == f"file {i}".encode()
        )
    assert len(list(session.ws.history())) == before + 6  # one commit per file


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
    cfg = registry.get("s1").ws.python_config
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
        {"name": "s1", "title": "New session", "busy": False, "model": None}
    ]
    # and it opens lazily with its files intact
    registry.get("s1").ws.write_file("keep.txt", "here")
    session = reborn.open("s1")
    assert session.ws.fs.read("keep.txt") == b"here"
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
    client.post(f"/api/sessions/{session}/chat", json={"message": message})
    _collect_until_done(client, session)


# -- edit: rewind + retry as one verb ---------------------------------------------


def _user_seqs(session) -> list[int]:
    return [e["seq"] for e in session.events if e["type"] == "user"]


def test_edit_rewinds_truncates_and_reruns(studio):
    """Editing an earlier prompt rewinds files + agent memory to just
    before that turn, marks the transcript cut with a `truncate` event
    (the log stays append-only), and runs the edited message fresh."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    chat_db = FakeChatDb()
    session.agent.db = chat_db

    _turn(client, "s1", "one")
    session.ws.write_file("a.txt", "A")
    _turn(client, "s1", "two")
    session.ws.write_file("b.txt", "B")
    chat_db.record = SimpleNamespace(
        runs=[SimpleNamespace(run_id="run-1"), SimpleNamespace(run_id="run-2")]
    )

    seq = _user_seqs(session)[1]
    r = client.post(
        "/api/sessions/s1/edit", json={"seq": seq, "message": "two, but better"}
    )
    assert r.status_code == 200
    _collect_until_done(client, "s1", since=r.json()["since"] - 1)

    # files rewound to the edited turn's pre-turn head
    assert session.ws.fs.exists("a.txt") and not session.ws.fs.exists("b.txt")
    # agent memory: turn two forgotten — even though it changed no
    # files (commit order can't see that; the transcript can)
    assert [run.run_id for run in chat_db.record.runs] == ["run-1"]
    # the log: ... truncate{to:seq}, then the fresh turn
    kinds = [e["type"] for e in session.events]
    cut = kinds.index("truncate")
    assert session.events[cut]["to"] == seq
    assert kinds[cut + 1] == "user"
    assert session.events[cut + 1]["text"] == "two, but better"
    assert session.agent.seen[-1] == "two, but better"


def test_edit_unknown_mapping_leaves_memory_alone(studio):
    """A kept turn whose run_id isn't in the agno record (drift) must
    never corrupt memory — leave it rather than guess."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    chat_db = FakeChatDb()
    session.agent.db = chat_db
    _turn(client, "s1", "one")
    _turn(client, "s1", "two")
    chat_db.record = SimpleNamespace(runs=[SimpleNamespace(run_id="mystery")])

    seq = _user_seqs(session)[1]  # keeps turn one, whose run_id is unknown
    r = client.post("/api/sessions/s1/edit", json={"seq": seq, "message": "redo"})
    assert r.status_code == 200
    _collect_until_done(client, "s1", since=r.json()["since"] - 1)
    assert [r.run_id for r in chat_db.record.runs] == ["mystery"]


def test_edit_first_message_clears_memory(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    chat_db = FakeChatDb()
    session.agent.db = chat_db
    _turn(client, "s1", "one")
    chat_db.record = SimpleNamespace(runs=[SimpleNamespace(run_id="run-1")])

    seq = _user_seqs(session)[0]
    r = client.post("/api/sessions/s1/edit", json={"seq": seq, "message": "redo"})
    assert r.status_code == 200
    _collect_until_done(client, "s1", since=r.json()["since"] - 1)
    assert chat_db.record.runs == []  # nothing precedes the first turn


def test_edit_after_edit_respects_the_projection(studio):
    """A second edit must reason about the transcript AS PROJECTED:
    done events behind an earlier cut refer to runs that no longer
    exist in agent memory, and matching one would corrupt the rewind."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    chat_db = FakeChatDb()
    session.agent.db = chat_db

    _turn(client, "s1", "one")  # run-1
    _turn(client, "s1", "two")  # run-2
    chat_db.record = SimpleNamespace(
        runs=[SimpleNamespace(run_id="run-1"), SimpleNamespace(run_id="run-2")]
    )
    first_cut = _user_seqs(session)[1]
    r = client.post(
        "/api/sessions/s1/edit", json={"seq": first_cut, "message": "two v2"}
    )
    _collect_until_done(client, "s1", since=r.json()["since"] - 1)
    # agno would have appended the fresh turn's run (run-3)
    chat_db.record.runs.append(SimpleNamespace(run_id="run-3"))

    # edit the REPLACEMENT prompt: the kept prefix is just turn one —
    # a raw (unprojected) scan would land on stale run-2 instead
    second_cut = _user_seqs(session)[-1]
    assert second_cut > first_cut
    r = client.post(
        "/api/sessions/s1/edit", json={"seq": second_cut, "message": "two v3"}
    )
    assert r.status_code == 200
    _collect_until_done(client, "s1", since=r.json()["since"] - 1)
    assert [run.run_id for run in chat_db.record.runs] == ["run-1"]


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
    session.ws.fs.write("/workspace/ui/fig.plotly.json", _json.dumps(spec).encode())

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
    stable run_id, which is what makes the rewind placeable at all."""
    import asyncio

    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws
    hook = sessions_mod.Registry._retry_rewind_hook(ws)
    ctx = SimpleNamespace(run_id="run-1")

    asyncio.run(hook(ctx))  # attempt 1: records the pre-turn head
    start = ws.head
    ws.write_file("/workspace/app/index.html", "half an app")
    assert ws.head != start, "the write should have moved the head"

    asyncio.run(hook(ctx))  # attempt 2 under the same run: a retry
    assert ws.head == start
    assert not ws.fs.isfile("/workspace/app/index.html")

    # a NEW run is a new turn, not a retry — it re-anchors and rewinds
    # nothing, or the next turn would undo the previous one's work
    ws.write_file("/workspace/keep.txt", "second turn")
    after_write = ws.head
    asyncio.run(hook(SimpleNamespace(run_id="run-2")))
    assert ws.head == after_write
    assert ws.fs.isfile("/workspace/keep.txt")


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
    """The snapshot branch was always durable; the token -> branch
    mapping must be too — and the reborn snapshot reconnects to the
    session's db file (frozen code, live state, across restarts)."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    session.ws.fs.write(
        "/workspace/app/api/names.py",
        b"def get(req):\n"
        b'    db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")\n'
        b"    return {'names': [r[0] for r in db.query('SELECT v FROM t')]}\n",
    )
    session.ws.checkpoint()
    pub = client.post("/api/sessions/s1/publish").json()
    session.db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    session.db.execute("INSERT INTO t VALUES ('before-restart')")
    assert client.get(f"{pub['url']}api/names").json() == {"names": ["before-restart"]}

    # "restart": a fresh registry over the same store, sessions unopened
    reborn = sessions_mod.Registry(model_factory=lambda *a: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    with TestClient(server.build_app(reborn)) as client2:
        r = client2.get(f"{pub['url']}api/names")
        assert r.status_code == 200
        assert r.json() == {"names": ["before-restart"]}  # same db file
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
    assert ws.fs.isdir("/workspace/ui")
    result = ws.run_python("open('/workspace/ui/x.png', 'wb').write(b'png-ish')")
    assert not result.error
    assert ws.fs.read("/workspace/ui/x.png") == b"png-ish"


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
    """Delete takes the workspace branch, app db, transcript, chat
    record, AND published snapshots (views of the same universe)."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.ws.write_file("keep.txt", "data")
    _seed_app(session.ws)
    pub = client.post("/api/sessions/s1/publish").json()
    client.post("/api/sessions/s1/upload?name=u.txt", content=b"x")
    assert (tmp_path / "dbs" / "s1.sqlite").exists()
    assert (tmp_path / "events" / "s1.jsonl").exists()

    assert client.delete("/api/sessions/s1").json() == {"ok": True}

    assert client.get("/api/sessions").json()["sessions"] == []
    assert client.get("/api/sessions/s1/events?wait=0").status_code == 404
    assert client.get(pub["url"]).status_code == 404
    assert not (tmp_path / "dbs" / "s1.sqlite").exists()
    assert not (tmp_path / "events" / "s1.jsonl").exists()

    # recreating the name is a FRESH universe — the branch really died
    # (an orphaned branch would resurrect the old files here)
    client.post("/api/sessions", json={"name": "s1"})
    reborn = registry.get("s1")
    assert not reborn.ws.fs.exists("keep.txt")
    assert not reborn.ws.fs.exists("/workspace/app/index.html")


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
    registry.get("s2").ws.write_file("mine.txt", "s2 data")
    client.delete("/api/sessions/s1")
    assert client.get("/api/sessions").json()["sessions"] == [
        {"name": "s2", "title": "New session", "busy": False, "model": None}
    ]
    assert registry.get("s2").ws.fs.read("mine.txt") == b"s2 data"


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
        {"name": "s1", "title": "New session", "busy": False, "model": "dummy"}
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
        # the tool REALLY ran: the workspace has the file, checkpointed
        ws = registry.get("s1").ws
        assert ws.fs.read("/workspace/notes.md") == b"scripted"
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
    restart path) — must hand workspace() the selected factory. A
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
        assert isinstance(session.ws._executor, MarkedExecutor)
        token, _ = registry.publish(session.name)
        # publish's fork inherits via Workspace.fork; force the OTHER
        # path — the lazy manifest reopen a restart would take
        registry._published.pop(token).close()
        snapshot = registry.resolve(token)
        assert snapshot is not None
        assert isinstance(snapshot._executor, MarkedExecutor)
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

    assert not ws.fs.exists("/workspace/app/vendor")
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
    session.ws.fs.makedirs("/workspace/app", exist_ok=True)
    session.ws.fs.write(
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
    session.ws.checkpoint()

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
    ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.fs.write("/workspace/app/index.html", (refs / "app.html").read_bytes())
    ws.fs.write("/workspace/app/app.jsx", (refs / "app.jsx").read_bytes())
    ws.fs.write("/workspace/app/api/summary.py", REFERENCE_HANDLER)
    ws.checkpoint()
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
    ws.fs.makedirs("/workspace/app", exist_ok=True)
    ws.fs.write("/workspace/app/index.html", html)
    ws.fs.write("/workspace/app/app.jsx", jsx)
    ws.checkpoint()


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
    ws.fs.makedirs("/workspace/app", exist_ok=True)
    ws.fs.write("/workspace/app/index.html", b"<h1>hi</h1>")
    ws.checkpoint()

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
    ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.fs.makedirs("/workspace/app/data", exist_ok=True)
    ws.run_python(
        "import pandas as pd\n"
        'pd.DataFrame({"category": ["a", "b", None],\n'
        '              "region": ["north", None, "south"],\n'
        '              "year": [2020, 2021, 2022],\n'
        '              "value": [1.0, None, None]}\n'
        ').to_parquet("/workspace/app/data/records.parquet")\n'
    )
    ws.fs.write("/workspace/app/api/summary.py", (refs / "api-handler.py").read_bytes())

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
    ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.fs.makedirs("/workspace/app/data", exist_ok=True)
    ws.run_python(
        "import pandas as pd\n"
        'pd.DataFrame({"category": ["a", "b"], "region": ["north", "south"],\n'
        '              "year": [2020, None], "value": [1.0, 3.0]}\n'
        ').to_parquet("/workspace/app/data/records.parquet")\n'
    )
    ws.fs.write("/workspace/app/api/summary.py", (refs / "api-handler.py").read_bytes())

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
    session.ws.fs.makedirs("/workspace/app", exist_ok=True)
    session.ws.fs.write("/workspace/app/custom-theme.css", b".mine { color: red }\n")
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
