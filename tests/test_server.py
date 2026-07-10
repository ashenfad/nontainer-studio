"""Server plumbing — background turns + event-log transcript, session
lifecycle, preview/publish, time travel — exercised with a fake agent
(no LLM, no key)."""

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
    registry = sessions_mod.Registry(model_factory=lambda: None, store=tmp_path)
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
    assert kinds == ["user", "tool_start", "tool_end", "text", "text", "done"]
    assert events[0]["text"] == "build me a thing"
    assert "".join(e["delta"] for e in events if e["type"] == "text") == "hello world"

    # a SECOND subscriber replays the identical transcript from 0 —
    # this is what makes session switching / reload safe
    replay = _collect_until_done(client, "s1")
    assert [e["type"] for e in replay] == kinds
    # and cursors let a client resume where it left off
    tail = _collect_until_done(client, "s1", since=len(events) - 1)
    assert [e["type"] for e in tail] == ["done"]


def test_chat_missing_session_and_empty_message(studio):
    client, _ = studio
    assert (
        client.post("/api/sessions/nope/chat", json={"message": "x"}).status_code == 404
    )
    client.post("/api/sessions", json={"name": "s1"})
    assert (
        client.post("/api/sessions/s1/chat", json={"message": "  "}).status_code == 400
    )


def test_busy_session_409s_chat_and_restore(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    session.turn_lock.acquire()  # simulate a running turn
    try:
        assert (
            client.post("/api/sessions/s1/chat", json={"message": "x"}).status_code
            == 409
        )
        assert (
            client.post(
                "/api/sessions/s1/restore", json={"checkpoint": "x"}
            ).status_code
            == 409
        )
        assert client.get("/api/sessions").json()["sessions"] == [
            {"name": "s1", "busy": True}
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


# -- time travel ---------------------------------------------------------------


def test_history_restore(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    ws = registry.get("s1").ws
    ws.write_file("v1.txt", "one")
    first = ws.head
    ws.write_file("v2.txt", "two")

    entries = client.get("/api/sessions/s1/history").json()["history"]
    assert len(entries) >= 2

    # restore rewinds files...
    assert client.post(
        "/api/sessions/s1/restore", json={"checkpoint": first}
    ).json() == {"ok": True}
    assert ws.fs.exists("v1.txt") and not ws.fs.exists("v2.txt")
    # ...and shows up in the transcript as a notice
    assert any(e["type"] == "notice" for e in registry.get("s1").events)

    assert (
        client.post(
            "/api/sessions/s1/restore", json={"checkpoint": "bogus"}
        ).status_code
        == 400
    )


def test_fork_copies_db_and_rewinds_workspace(studio):
    """Fork = a new universe: workspace branches (optionally rewound to
    a checkpoint), db is COPIED as-of-now (no history to rewind), and
    the two dbs are independent afterward."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    parent = registry.get("s1")
    parent.ws.write_file("v1.txt", "one")
    at = parent.ws.head
    parent.ws.write_file("v2.txt", "two")
    parent.db.execute("CREATE TABLE t (v TEXT)")
    parent.db.execute("INSERT INTO t VALUES ('shared')")

    r = client.post("/api/sessions/s1/fork", json={"name": "s2", "checkpoint": at})
    assert r.status_code == 200
    child = registry.get("s2")

    # workspace rewound to the checkpoint (fork-from-here)
    assert child.ws.fs.exists("v1.txt") and not child.ws.fs.exists("v2.txt")
    # db copied as-of-now...
    assert child.db.query("SELECT v FROM t") == [("shared",)]
    # ...and independent from here on
    child.db.execute("INSERT INTO t VALUES ('child-only')")
    assert parent.db.query("SELECT v FROM t") == [("shared",)]

    # name collisions and bad parents fail loudly
    assert client.post("/api/sessions/s1/fork", json={"name": "s2"}).status_code == 400
    assert (
        client.post("/api/sessions/nope/fork", json={"name": "s3"}).status_code == 404
    )


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
    # checkpointed: restore/fork semantics extend to uploads
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

    reborn = sessions_mod.Registry(model_factory=lambda: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    assert reborn.list() == [{"name": "s1", "busy": False}]
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

    reborn = sessions_mod.Registry(model_factory=lambda: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    session = reborn.open("s1")
    assert [e["type"] for e in session.events] == [e["type"] for e in events]
    assert session.events[0]["type"] == "user"
    assert session.events[0]["text"] == "hello"
    assert session.events[0]["head"]  # the undo anchor rides the user event
    reborn.close()


def test_event_log_tolerates_torn_lines(studio, tmp_path):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    log = registry.get("s1").log_path
    log.write_text('{"type": "user", "text": "ok"}\n{"type": "trunc')  # crash mid-write
    assert sessions_mod.Registry._load_events(log) == [{"type": "user", "text": "ok"}]


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


def test_restore_rewinds_agent_memory_with_the_files(studio):
    """The synchronized undo: restore rewinds workspace AND agno runs
    together (mapping: done events carry head + run_id), while the
    visible transcript keeps its full record."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    chat_db = FakeChatDb()
    session.agent.db = chat_db

    _turn(client, "s1", "one")
    session.ws.write_file("a.txt", "A")
    at = session.ws.head
    _turn(client, "s1", "two")  # done head == `at`
    session.ws.write_file("b.txt", "B")
    _turn(client, "s1", "three")  # done head is after `at`

    # agno would have stored one run per turn
    chat_db.record = SimpleNamespace(
        runs=[SimpleNamespace(run_id=f"run-{i}") for i in (1, 2, 3)]
    )

    r = client.post("/api/sessions/s1/restore", json={"checkpoint": at})
    assert r.json() == {"ok": True}

    # files rewound...
    assert session.ws.fs.exists("a.txt") and not session.ws.fs.exists("b.txt")
    # ...agent memory rewound in sync (turn three forgotten)...
    assert [run.run_id for run in chat_db.record.runs] == ["run-1", "run-2"]
    # ...and the transcript kept everything, plus the notice
    kinds = [e["type"] for e in session.events]
    assert kinds.count("user") == 3
    assert kinds[-1] == "notice"


def test_restore_to_before_all_turns_clears_memory(studio):
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    chat_db = FakeChatDb()
    session.agent.db = chat_db
    genesis = session.ws.head

    session.ws.write_file("a.txt", "A")
    _turn(client, "s1", "one")
    chat_db.record = SimpleNamespace(runs=[SimpleNamespace(run_id="run-1")])

    client.post("/api/sessions/s1/restore", json={"checkpoint": genesis})
    assert chat_db.record.runs == []  # no turn is at-or-before genesis


def test_restore_unknown_mapping_leaves_memory_alone(studio):
    """A kept turn whose run_id isn't in the agno record (drift) must
    never corrupt memory — leave it rather than guess."""
    client, registry = studio
    client.post("/api/sessions", json={"name": "s1"})
    session = registry.get("s1")
    chat_db = FakeChatDb()
    session.agent.db = chat_db
    _turn(client, "s1", "one")
    at = session.ws.head
    session.ws.write_file("a.txt", "A")
    chat_db.record = SimpleNamespace(runs=[SimpleNamespace(run_id="mystery")])

    client.post("/api/sessions/s1/restore", json={"checkpoint": at})
    assert [r.run_id for r in chat_db.record.runs] == ["mystery"]


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
    reborn = sessions_mod.Registry(model_factory=lambda: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    with TestClient(server.build_app(reborn)) as client2:
        r = client2.get(f"{pub['url']}api/names")
        assert r.status_code == 200
        assert r.json() == {"names": ["before-restart"]}  # same db file
        assert client2.get("/apps/not-a-real-token/").status_code == 404
    reborn.close()


# -- dummy model: the real agent loop, scripted -----------------------------------


def test_dummy_model_drives_real_agent(tmp_path):
    """The E2E test double: DummyModel fakes only the LLM — the agno
    run loop, WorkspaceTools, and the workspace all execute for real.
    Directives in the user message script the turn."""
    from nontainer_studio.dummy import DummyModel

    registry = sessions_mod.Registry(model_factory=DummyModel, store=tmp_path)
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
        reply = "".join(e["delta"] for e in events if e["type"] == "text")
        assert reply == "Wrote your note."
        # the tool REALLY ran: the workspace has the file, checkpointed
        ws = registry.get("s1").ws
        assert ws.fs.read("/notes.md") == b"scripted"
        # and the done event carries the run mapping for undo
        done = next(e for e in events if e["type"] == "done")
        assert done["run_id"] and done["head"]
    registry.close()


def test_v1_manifest_format_tolerated(studio, tmp_path):
    (tmp_path / "sessions.json").write_text('["old-style"]')
    reborn = sessions_mod.Registry(model_factory=lambda: None, store=tmp_path)
    reborn._build_agent = lambda *a, **k: FakeAgent()
    assert {"name": "old-style", "busy": False} in reborn.list()
    assert reborn.resolve("nope") is None
    reborn.close()
