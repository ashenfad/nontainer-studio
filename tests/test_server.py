"""Server plumbing — background turns + event-log transcript, session
lifecycle, preview/publish, time travel — exercised with a fake agent
(no LLM, no key)."""

import re
import time
from types import SimpleNamespace

import pytest
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
    ws.fs.makedirs("/app/api", exist_ok=True)
    ws.fs.write("/app/index.html", b"<html><body><h1>counter</h1></body></html>")
    ws.fs.write("/app/api/count.py", HANDLER.encode())
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
                "[ui artifacts: trend -> /ui/trend.plotly.json, "
                "raw -> /ui/raw.table.json]"
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
        "path": "/ui/trend.plotly.json",
        "kind": "plotly",
    }
    assert arts[1]["path"] == "/ui/raw.table.json" and arts[1]["kind"] == "table"
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
            result = "x" * 5_000 + "\n[ui artifacts: big -> /ui/big.plotly.json]"
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
    assert art["path"] == "/ui/big.plotly.json" and art["kind"] == "plotly"


def test_tool_result_without_note_emits_no_artifact_events(studio):
    """The common case: an ordinary tool result yields only tool_end."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    client.post("/api/sessions/s1/chat", json={"message": "list files"})
    events = _collect_until_done(client, "s1")
    assert not any(e["type"] == "artifact" for e in events)


def test_new_sessions_seed_skills(studio):
    """Session creation installs the repo's starter skills into
    /skills as ordinary versioned files; existing sessions keep their
    own (possibly agent-edited) copies."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    files = client.get("/api/sessions/s1/files").json()["files"]
    assert "/skills/building-apps/SKILL.md" in files
    assert "/skills/building-apps/references/preact-app.html" in files

    # creation-only: a reseed must not clobber the session's copies
    session = registry.get("s1")
    session.ws.write_file("/skills/building-apps/SKILL.md", "agent-edited")
    registry.close()
    registry._sessions.clear()
    session2 = registry.open("s1")
    assert session2.ws.fs.read("/skills/building-apps/SKILL.md") == b"agent-edited"


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
    the SAME db as the authoring session (host_objects inherit through
    the fork)."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.ws.fs.makedirs("/app/api", exist_ok=True)
    session.ws.fs.write(
        "/app/api/names.py",
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
    ws.fs.makedirs("/app/screenshots", exist_ok=True)
    ws.fs.write("/notes.md", b"# hi")
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
        "7753de0000000c49444154089963f8cfc000000301010018dd8db0000000"
        "0049454e44ae426082"
    )
    ws.fs.write("/app/screenshots/shot-1.png", png)

    files = client.get("/api/sessions/s1/files").json()["files"]
    assert "/notes.md" in files and "/app/screenshots/shot-1.png" in files

    r = client.get("/api/sessions/s1/file", params={"path": "/notes.md"})
    assert r.status_code == 200 and r.text == "# hi"

    r = client.get(
        "/api/sessions/s1/file", params={"path": "/app/screenshots/shot-1.png"}
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
    assert r.json() == {"ok": True, "path": "/uploads/data.csv", "size": 8}
    assert session.ws.fs.read("/uploads/data.csv") == b"a,b\n1,2\n"
    # checkpointed: an edit's rewind extends to uploads
    assert any(c.info.get("tool") == "file_write" for c in session.ws.history(limit=3))
    # transcript notice
    assert any(
        e["type"] == "notice" and "/uploads/data.csv" in e["text"]
        for e in session.events
    )

    # basename-only: traversal-ish names collapse to a safe filename
    r = client.post("/api/sessions/s1/upload?name=../../etc/passwd", content=b"x")
    assert r.json()["path"] == "/uploads/passwd"

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
        assert session.ws.fs.read(f"/uploads/f{i}.txt") == f"file {i}".encode()
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
        result = "plotted it\n[ui artifacts: fig -> /ui/fig.plotly.json]"
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
    session.ws.fs.write("/ui/fig.plotly.json", _json.dumps(spec).encode())

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
    session.ws.fs.makedirs("/app/api", exist_ok=True)
    session.ws.fs.write(
        "/app/api/names.py",
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
    """Agents predictably savefig straight into /ui instead of
    assigning objects to `ui` — the near-miss should work, not
    FileNotFoundError (VFS open doesn't create parents)."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws
    assert ws.fs.isdir("/ui")
    result = ws.run_python("open('/ui/x.png', 'wb').write(b'png-ish')")
    assert not result.error
    assert ws.fs.read("/ui/x.png") == b"png-ish"


def test_error_truncation_keeps_the_exception_line(studio):
    """Tracebacks cap by cutting the MIDDLE: the final line (the
    exception) is the whole point of the message."""
    from nontainer_studio.server import _short_middle

    trace = (
        "Traceback (most recent call last):\n"
        + "\n".join(f'  File "<x>", line {i}, in frame_{i}' for i in range(200))
        + "\nFileNotFoundError: No such file or directory: '/ui/plot.png'"
    )
    capped = _short_middle(trace)
    assert len(capped) <= 2_100
    assert capped.startswith("Traceback")
    assert capped.endswith(
        "FileNotFoundError: No such file or directory: '/ui/plot.png'"
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
    assert not reborn.ws.fs.exists("/app/index.html")


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
            '!tool file_write {"path": "/notes.md", "content": "scripted"}\n'
            "!text Wrote your note."
        )
        client.post("/api/sessions/s1/chat", json={"message": message})
        events = _collect_until_done(client, "s1")

        kinds = [e["type"] for e in events]
        assert "tool_start" in kinds and "tool_end" in kinds
        started = next(e for e in events if e["type"] == "tool_start")
        assert started["name"] == "file_write"
        assert started["args"]["path"] == "/notes.md"  # structured through agno
        reply = "".join(e["delta"] for e in events if e["type"] == "text")
        assert reply == "Wrote your note."
        # the tool REALLY ran: the workspace has the file, checkpointed
        ws = registry.get("s1").ws
        assert ws.fs.read("/notes.md") == b"scripted"
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
    """The default: agent code runs in a forked worker. Killing that
    worker (a stand-in for segfault/OOM) costs nothing but the moment —
    the server survives, and the next execution respawns and still
    sees the workspace."""
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
