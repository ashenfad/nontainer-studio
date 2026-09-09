"""Delegation: the studio's SessionRunner over a forked session.

Real everything below the model — the registry, the fork, agno's run
loop, WorkspaceTools, the workspace — with the scripted DummyModel
standing in for the LLM, so a delegate's `!tool` directives execute for
real against its own branch.
"""

import asyncio
import json
import time

import pytest
from agno.models.response import ModelResponse
from nontainer.sessions import Sessions

from nontainer_studio import delegates, server
from nontainer_studio import sessions as sessions_mod
from nontainer_studio.dummy import DummyModel

WRITE_A_NOTE = (
    '!tool file_write {"path": "/workspace/from_delegate.md", '
    '"content": "delegate was here"}\n'
    "!text Wrote the note."
)


@pytest.fixture
def registry(tmp_path):
    reg = sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path,
        default_model="dummy",
    )
    yield reg
    reg.close()


def _delegate(registry, parent, task, **kw):
    """Ask, wait, answer — the blocking form, so a test is a straight line."""
    runner = delegates.StudioRunner(registry, parent.name, delegates.DELEGATE_TURNS)
    with Sessions(parent.ws, runner) as helper:
        return helper.ask(task, wait=True, **kw)


def test_a_delegate_answers_and_leaves_its_work_on_its_own_branch(registry):
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)

    assert answer.status == "answered"
    assert answer.text == "Wrote the note."
    # scoped under the session that asked, and that IS the merge handle
    assert answer.branch.startswith("boss.")
    assert answer.ref.startswith(answer.branch + "@")
    assert "/workspace/from_delegate.md" in answer.changed["seed"]

    # nothing reached the parent: the work is a branch, and merging it
    # is the parent's own decision in the terminal
    assert not parent.ws.files.fs.exists("/workspace/from_delegate.md")
    child = registry.open(answer.branch)
    assert child.ws.files.fs.read("/workspace/from_delegate.md") == b"delegate was here"


def test_the_task_arrives_with_a_provenance_header(registry):
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)

    child = registry.open(answer.branch)
    asked = next(e for e in child.events if e["type"] == "user")
    assert asked["text"].startswith("[delegated by session `boss` at commit ")
    # mechanism, not the human principal
    assert "not the person at the keyboard" in asked["text"]
    # and the commit it names is the parent's own, not one of the fork's
    commit = asked["text"].split("at commit ")[1][:8]
    assert any(c.id.startswith(commit) for c in parent.ws.log(limit=4))
    # the task itself is intact underneath it
    assert asked["text"].endswith(WRITE_A_NOTE)


def test_a_delegate_reads_the_parents_app_db(registry):
    """Live app state is copied in, not shared: the fork carries files
    and cache, and the db versions with neither."""
    parent = registry.open("boss")
    parent.db.execute("CREATE TABLE t (n INT)")
    parent.db.execute("INSERT INTO t VALUES (7)")

    answer = _delegate(
        registry,
        parent,
        '!tool run_python {"code": "print(db.query(\'SELECT n FROM t\'))"}\n'
        "!text Read it.",
    )
    child = registry.open(answer.branch)
    assert child.db.query("SELECT n FROM t") == [(7,)]

    # a copy: the delegate's rows stay on its side
    child.db.execute("INSERT INTO t VALUES (9)")
    assert parent.db.query("SELECT n FROM t") == [(7,)]


def test_the_runner_releases_the_delegate_when_it_answers(registry):
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)
    # handles released, everything on disk kept: the branch is what the
    # parent merges from, and it needs no open session to be there
    assert registry.get(answer.branch) is None
    assert answer.branch in registry.known()


def test_a_delegate_that_never_replies_is_capped(registry, monkeypatch):
    """A turn that produces no prose burns budget; running out resolves
    as `capped` rather than raising — the caller reads a status."""
    turns = []
    monkeypatch.setattr(
        delegates, "_reply", lambda child, since: (turns.append(since), ("", None))[1]
    )
    parent = registry.open("boss")
    runner = delegates.StudioRunner(registry, parent.name, 2)
    with Sessions(parent.ws, runner) as helper:
        answer = helper.ask("!text ignored", wait=True)

    assert answer.status == "capped"
    assert len(turns) == 2


def test_budget_caps_turns_and_a_nonsense_budget_falls_back(registry):
    runner = delegates.StudioRunner(registry, "boss", 3)
    assert runner._budget(1) == 1
    assert runner._budget(None) == 3
    assert runner._budget("two") == 3  # a model's tool call, not a type error
    assert runner._budget(0) == 3


# -- how delegates appear (and don't) in the studio --------------------------


def test_delegates_stay_out_of_the_rail(registry):
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)

    assert [row["name"] for row in registry.list()] == ["boss"]
    # still a session in every other sense — openable, and known
    assert answer.branch in registry.known()
    assert registry.is_delegate(answer.branch)


def test_a_dotted_name_nobody_forked_is_a_session_like_any_other(registry):
    registry.open("my.notes")
    assert not registry.is_delegate("my.notes")
    assert [row["name"] for row in registry.list()] == ["my.notes"]


def test_a_dotted_session_is_not_owned_by_the_session_it_is_named_under(registry):
    """Ownership is recorded, never inferred. `foo.notes` typed by a
    human is an ordinary session even when `foo` exists beside it."""
    foo = registry.open("foo")
    notes = registry.open("foo.notes")
    _turn(notes, "!text a session of its own")  # gives it a transcript to keep

    assert not registry.is_delegate("foo.notes")
    assert sorted(row["name"] for row in registry.list()) == ["foo", "foo.notes"]

    registry.delete(foo)

    assert [row["name"] for row in registry.list()] == ["foo.notes"]
    assert "foo.notes" in registry._store.sessions()
    assert (registry._store.path / "dbs" / "foo.notes.sqlite").exists()
    assert (registry._store.path / "events" / "foo.notes.jsonl").exists()


def test_the_delegate_record_outlives_the_registry(registry, tmp_path):
    """The record is in the manifest, so a restart still knows which
    sessions are somebody's delegates."""
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)

    manifest = json.loads((tmp_path / "sessions.json").read_text())
    assert manifest["delegates"][answer.branch] == "boss"

    reborn = sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path,
        default_model="dummy",
    )
    assert reborn.is_delegate(answer.branch)
    assert [row["name"] for row in reborn.list()] == ["boss"]


def test_deleting_a_session_takes_its_delegates_with_it(registry):
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)
    child = answer.branch

    registry.delete(parent)

    assert child not in registry.known()
    assert child not in registry._store.sessions()
    assert not (registry._store.path / "dbs" / f"{child}.sqlite").exists()
    assert not (registry._store.path / "events" / f"{child}.jsonl").exists()
    # the ownership record goes with the branch it described
    assert registry._manifest()["delegates"] == {}


def test_deleting_a_delegate_on_its_own_takes_its_record(registry):
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)

    registry.delete(registry.open(answer.branch))

    assert registry._manifest()["delegates"] == {}
    assert answer.branch not in registry._store.sessions()
    assert [row["name"] for row in registry.list()] == ["boss"]


# -- the sessions tool, end to end ------------------------------------------


def _turn(session, message):
    """One turn, run exactly as the server runs a human's."""
    session.turn_lock.acquire()  # _run_turn releases it
    since = session.next_seq
    asyncio.run(server._run_turn(session, message))
    return [e for e in session.events if e.get("seq", -1) >= since]


def _tool_results(events, name):
    return [
        e["result"] for e in events if e["type"] == "tool_end" and e["name"] == name
    ]


ASK = (
    '!tool sessions {"action": "ask", "name": "scout", "wait": true, '
    '"task": "!tool file_write {\\"path\\": \\"/workspace/scouted.md\\", '
    '\\"content\\": \\"found it\\"}\\n!text Found it."}\n'
    "!text Sent a scout."
)


def test_the_agent_delegates_and_merges_the_work_back(registry):
    parent = registry.open("boss")

    asked = _turn(parent, ASK)
    result = _tool_results(asked, "sessions")[0]
    assert "Found it." in result
    # the tool's own next step, spelled for the terminal
    assert "ws-git merge boss.scout" in result
    # and nothing landed here: a delegate works on a branch of its own
    assert not parent.ws.files.fs.exists("/workspace/scouted.md")

    merged = _turn(
        parent,
        '!tool terminal {"command": "ws-git merge boss.scout"}\n'
        "!text Merged the scout's work.",
    )
    assert _tool_results(merged, "terminal")
    assert parent.ws.files.fs.read("/workspace/scouted.md") == b"found it"


ASK_ASYNC = (
    '!tool sessions {"action": "ask", "name": "scout", '
    '"task": "!tool file_write {\\"path\\": \\"/workspace/scouted.md\\", '
    '\\"content\\": \\"found it\\"}\\n!text Found it."}\n'
    "!text Sent a scout."
)


def _await_delegates(session, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.answered_delegates():
            return
        time.sleep(0.02)
    raise AssertionError("no delegate answered in time")


def test_a_delegates_answer_reaches_the_parent_on_its_next_turn(registry):
    parent = registry.open("boss")
    _turn(parent, ASK_ASYNC)  # cross-turn: the tool returns at once
    _await_delegates(parent)

    # the rail says an answer is waiting on a session nobody is watching
    assert [r["delegates"] for r in registry.list() if r["name"] == "boss"] == [1]

    events = _turn(parent, "what did the scout say?")
    injected = next(e for e in events if e["type"] == "delegate")
    assert injected["name"] == "boss.scout"
    assert injected["status"] == "answered"
    # mechanism, and the next step spelled for the terminal
    assert "not the person at the keyboard" in injected["text"]
    assert "Found it." in injected["text"]
    assert "ws-git merge boss.scout" in injected["text"]

    # the model was sent it too, ahead of the human's message (the dummy
    # echoes what it was asked when the message carries no directives)
    reply = "".join(e["delta"] for e in events if e["type"] == "text")
    assert reply.startswith("dummy: [delegate `boss.scout` answered")

    # delivered once: the next turn has nothing waiting
    assert [r["delegates"] for r in registry.list() if r["name"] == "boss"] == [0]
    assert not any(e["type"] == "delegate" for e in _turn(parent, "!text ok"))


def _edit(registry, session, seq, message):
    """The /edit route's sequence: rewind, cut the transcript, re-run."""
    session.turn_lock.acquire()  # _run_turn releases it
    registry.rewind_to_event(session, seq)
    since = session.next_seq

    async def go():
        await session.emit({"type": "truncate", "to": seq})
        await server._run_turn(session, message)

    asyncio.run(go())
    return [e for e in session.events if e.get("seq", -1) >= since]


def test_an_edited_turn_gets_the_delegates_answer_again(registry):
    """Delivery is a fact of the transcript. An edit unsays the turn the
    answer landed in — out of the files, out of the agent's memory, out
    of the visible transcript — so the replacement turn has to carry it
    again or the rewound conversation is missing it for good."""
    parent = registry.open("boss")
    _turn(parent, ASK_ASYNC)
    _await_delegates(parent)

    delivered = _turn(parent, "what did the scout say?")
    assert [e["name"] for e in delivered if e["type"] == "delegate"] == ["boss.scout"]

    seq = next(e["seq"] for e in delivered if e["type"] == "user")
    again = _edit(registry, parent, seq, "actually, what did the scout say?")
    assert [e["name"] for e in again if e["type"] == "delegate"] == ["boss.scout"]

    # once, though: the turn after it is not a third delivery
    assert not any(e["type"] == "delegate" for e in _turn(parent, "!text ok"))
    assert [r["delegates"] for r in registry.list() if r["name"] == "boss"] == [0]


class FailingModel(DummyModel):
    """Streams a sentence, then the provider dies mid-run."""

    async def ainvoke_stream(self, messages, **kwargs):
        yield ModelResponse(role="assistant", content="Got part of the way. ")
        raise RuntimeError("provider exploded")


def test_a_failure_after_partial_prose_is_failed_not_answered(tmp_path):
    """Prose that simply stops reads as a complete answer. A run that
    died says so, and keeps what the delegate managed to say."""
    registry = sessions_mod.Registry(
        model_factory=lambda *a, **k: FailingModel(),
        store=tmp_path,
        default_model="dummy",
    )
    try:
        parent = registry.open("boss")
        answer = _delegate(registry, parent, "look into it")
    finally:
        registry.close()

    assert answer.status == "failed"
    assert "Got part of the way." in answer.text
    assert "provider exploded" in answer.text
