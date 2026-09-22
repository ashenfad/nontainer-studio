"""Delegation: the studio's SessionRunner over a forked session.

Real everything below the model — the registry, the fork, agno's run
loop, WorkspaceTools, the workspace — with the scripted DummyModel
standing in for the LLM, so a delegate's `!tool` directives execute for
real against its own branch.
"""

import asyncio
import json
import threading
import time

import pytest
from agno.models.response import ModelResponse
from nontainer.errors import BranchExpired
from nontainer.sessions import Sessions

from nontainer_studio import delegates, server
from nontainer_studio import sessions as sessions_mod
from nontainer_studio import summaries as summaries_mod
from nontainer_studio.dummy import DummyModel

WRITE_A_NOTE = (
    '!tool file_write {"path": "/workspace/from_delegate.md", '
    '"content": "delegate was here"}\n'
    "!text Wrote the note."
)


@pytest.fixture(autouse=True)
def delegation_on(monkeypatch):
    """Both agent-facing knobs on for this file.

    Delegation is what every test here is about, and `ws-git` is how a
    delegate's work comes back — neither is given to an agent unless the
    studio is told to. The default (both off) is asserted in
    test_server.py, beside the primer it shapes.
    """
    monkeypatch.setenv("NONTAINER_STUDIO_SESSIONS", "1")
    monkeypatch.setenv("NONTAINER_STUDIO_WSGIT", "1")


@pytest.fixture
def registry(tmp_path):
    reg = sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path,
        default_model="dummy",
    )
    yield reg
    reg.close()


def _db_of(registry, name: str) -> str:
    """Where a session's db is: the manifest row says so."""
    return registry._manifest()["sessions"][name]["db"]


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


def test_a_delegate_and_its_parent_share_one_db(registry):
    """`db` is a handle to an external store, and forking a session is
    not forking the store: the delegate's row names the file its
    parent's row names. So what the delegate's own code writes is
    there for the parent's next turn to read."""
    parent = registry.open("boss")
    parent.db.execute("CREATE TABLE t (n INT)")
    parent.db.execute("INSERT INTO t VALUES (7)")

    answer = _delegate(
        registry,
        parent,
        '!tool run_python {"code": "db.execute(\'INSERT INTO t VALUES (9)\')"}\n'
        "!text Wrote a row.",
    )
    assert answer.status == "answered"
    # the delegate's write, read by the session that asked for it
    assert parent.db.query("SELECT n FROM t ORDER BY n") == [(7,), (9,)]

    child = registry.open(answer.branch)
    assert child.db is parent.db  # one handle, so the writes queue
    assert child.db.query("SELECT n FROM t ORDER BY n") == [(7,), (9,)]
    rows = registry._manifest()["sessions"]
    assert rows[answer.branch]["db"] == rows["boss"]["db"]


def test_releasing_a_delegate_leaves_the_parents_db_open(registry):
    """`release` drops a finished delegate's handles. The db handle is
    not one of them: the file is the parent's, and closing the
    connection the parent is mid-conversation with would be a delegate
    finishing its work by breaking the session that asked."""
    parent = registry.open("boss")
    parent.db.execute("CREATE TABLE t (n INT)")
    answer = _delegate(registry, parent, WRITE_A_NOTE)
    assert registry.get(answer.branch) is None  # released when it answered

    parent.db.execute("INSERT INTO t VALUES (1)")
    assert parent.db.query("SELECT n FROM t") == [(1,)]


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
    assert (registry._store.path / _db_of(registry, "foo.notes")).exists()
    assert (registry._store.path / "events" / "foo.notes.jsonl").exists()


def test_the_delegate_record_outlives_the_registry(registry, tmp_path):
    """The record is in the manifest, so a restart still knows which
    sessions are somebody's delegates."""
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)

    manifest = json.loads((tmp_path / "sessions.json").read_text())
    row = manifest["delegates"][answer.branch]
    assert row["parent"] == "boss"

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
    db = registry._store.path / _db_of(registry, "boss")

    registry.delete(parent)

    assert child not in registry.known()
    assert child not in registry._store.sessions()
    assert not (registry._store.path / "events" / f"{child}.jsonl").exists()
    # the delegate never had a db of its own to take: it named its
    # parent's, and no deletion removes a db file
    assert db.exists()
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


def _turn(session, message, registry=None):
    """One turn, run exactly as the server runs a human's.

    ``registry`` is what the server passes, and what makes the turn
    snapshot its delegates' retention — the tests that care about that
    pass it, the rest run the turn without it."""
    session.turn_lock.acquire()  # _run_turn releases it
    since = session.next_seq
    asyncio.run(server._run_turn(session, message, registry))
    return [e for e in session.events if e.get("seq", -1) >= since]


def _run_ids(registry, name: str) -> list[str]:
    """The agent's memory on that branch, as the chat db holds it."""
    record = registry.db.get_session(name)
    return [run.run_id for run in (record.runs or [])] if record is not None else []


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
        await server._run_turn(session, message, registry)

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


# -- the published action: apps as a starting point --------------------------

PUBLISHED = '!tool sessions {"action": "published"}\n!text Listed them.'


def _make_app(registry, session, title: str, body: bytes = b"<h1>app</h1>") -> dict:
    """A session with an app under `app/`, a title, and a version."""
    registry.set_agent_title(session.name, title)
    session.ws.files.fs.makedirs("/workspace/app", exist_ok=True)
    session.ws.files.fs.write("/workspace/app/index.html", body)
    session.ws.commit()
    return registry.publish(session.name)


def test_the_published_action_lists_the_apps_the_human_has(registry):
    """What an agent may start from is what the human published, and it
    reads the studio's own app registry — so the list is the rail's."""
    boss = registry.open("boss")
    assert _tool_results(_turn(boss, PUBLISHED), "sessions") == [
        "The human has published nothing yet."
    ]

    first = _make_app(registry, registry.open("alice"), "Revenue dashboard")
    second = _make_app(registry, registry.open("bob"), "Signup funnel")

    listed = _tool_results(_turn(boss, PUBLISHED), "sessions")[0]
    lines = listed.splitlines()
    assert lines[0].endswith("title (current version), origin tag:")
    # newest first, each with the token the terminal and fork_from take
    assert lines[1] == f"- Signup funnel (v1) — {second['origin']}"
    assert lines[2] == f"- Revenue dashboard (v1) — {first['origin']}"
    assert len(lines) == 3


def test_the_published_action_carries_the_description(registry, monkeypatch):
    """A title names an app; the description says what is IN it, which
    is what decides whether starting from this one beats starting from
    a blank page. It rides an indented line under the app it belongs
    to."""
    monkeypatch.setattr(
        summaries_mod,
        "generate_description",
        lambda spec, transcript: "Charts revenue by month over the sales db.",
    )
    boss = registry.open("boss")
    alice = registry.open("alice")
    _turn(alice, "!text Built the dashboard.")
    published = _make_app(registry, alice, "Revenue dashboard")

    lines = _tool_results(_turn(boss, PUBLISHED), "sessions")[0].splitlines()
    assert lines[1] == f"- Revenue dashboard (v1) — {published['origin']}"
    assert lines[2] == "    Charts revenue by month over the sales db."


def test_a_version_with_no_origin_tag_says_so(registry):
    """A row that names no origin has nothing to mount, take from or
    fork — and saying so is better than printing a name that would
    resolve to nothing."""
    published = _make_app(registry, registry.open("alice"), "Revenue dashboard")
    manifest = registry._manifest()
    manifest["apps"][published["token"]]["versions"]["v1"].pop("origin")
    registry._save_manifest(manifest)

    boss = registry.open("boss")
    listed = _tool_results(_turn(boss, PUBLISHED), "sessions")[0]
    assert listed.splitlines()[1] == (
        "- Revenue dashboard (v1) — no origin tag: nothing to start from here"
    )


def test_every_other_action_is_nontainers_to_dispatch(registry, monkeypatch):
    """The studio owns the tool and one action; the rest arrive at
    `run_action` as the model sent them, over this session's own
    helper."""
    boss = registry.open("boss")
    seen = {}
    dispatch = sessions_mod.run_action

    def spy(helper, action, **kwargs):
        seen["helper"], seen["action"], seen["kwargs"] = helper, action, kwargs
        return dispatch(helper, action, **kwargs)

    monkeypatch.setattr(sessions_mod, "run_action", spy)
    result = _tool_results(
        _turn(boss, '!tool sessions {"action": "list"}\n!text ok'), "sessions"
    )[0]

    assert result.startswith("no delegated jobs yet")
    assert seen["action"] == "list"
    assert seen["helper"] is boss.delegates
    assert seen["kwargs"] == {
        "task": "",
        "name": "",
        "paths": None,
        "inherit": "fresh",
        "fork_from": "",
        "resume": "",
        "wait": False,
    }


def test_the_tool_is_nontainers_shape_under_nontainers_name(registry):
    """One `sessions` tool, with nontainer's signature argument for
    argument, so a model that has learned the spelling anywhere can use
    it here. Its description is a constant: a tool description is the
    head of the prompt cache and must not move between turns."""
    import inspect

    boss = registry.open("boss")
    tool = registry._sessions_tool(boss.name, boss.delegates)

    assert tool.__name__ == "sessions"
    assert list(inspect.signature(tool).parameters) == [
        "action",
        "task",
        "name",
        "paths",
        "inherit",
        "fork_from",
        "resume",
        "wait",
    ]
    assert tool.__doc__ is sessions_mod.SESSIONS_TOOL_DESCRIPTION
    assert registry._sessions_tool(boss.name, boss.delegates).__doc__ is tool.__doc__
    assert '  action="published"' in tool.__doc__

    # and the agent is handed it once: nontainer's is not registered
    # beside it. The toolkit still KNOWS the helper — that is what lets
    # it hand a delegate's answer over mid-turn — but it learned it
    # after construction, which is the half that registers no tool.
    toolkit = boss.agent.tools[0]
    assert toolkit.sessions is boss.delegates
    assert [f for f in toolkit.functions if f == "sessions"] == []
    assert [getattr(t, "__name__", None) for t in boss.agent.tools].count(
        "sessions"
    ) == 1


NOTE = "the CSV comes in UTF-16, which is why the loader decodes"


def test_an_agent_starts_from_an_app_whose_session_is_gone(registry, tmp_path):
    """The workflow the origin tag exists for. A session builds an app
    and publishes it; the session is deleted; a later session mounts
    that origin, takes a file out of it, and puts a question to the
    agent that built it — none of which the published version alone
    could answer, since it holds `app/` and nothing else."""
    maker = registry.open("maker")
    registry.set_agent_title("maker", "Revenue dashboard")
    _turn(
        maker,
        '!tool file_write {"path": "/workspace/app/index.html", '
        '"content": "<h1>revenue</h1>"}\n'
        '!tool file_write {"path": "/workspace/notes/loader.md", '
        f'"content": "{NOTE}"}}\n'
        "!text Built the dashboard.",
    )
    published = registry.publish("maker")
    tag = published["origin"]
    remembered = _run_ids(registry, "maker")
    assert remembered

    registry.delete(maker)
    assert "maker" not in registry._store.sessions()

    builder = registry.open("builder")
    assert tag in _tool_results(_turn(builder, PUBLISHED), "sessions")[0]

    # MOUNTED: the origin is the whole tree, where the version is `app/`
    mounted = _turn(
        builder,
        f'!tool terminal {{"command": "ws-git worktree add old {tag}"}}\n'
        "!text Mounted it.",
    )
    # The verb's own answer first, and the exact line a mount prints,
    # so a mount that did not happen is reported as what the terminal
    # said and not as a missing file. A refusal from the verb also
    # contains the word "worktree", so the word alone proves nothing.
    said = _tool_results(mounted, "terminal")[0]
    assert said.startswith("worktree old: ") and said.rstrip().endswith(
        "(read-only)"
    ), said
    fs = builder.ws.files.fs
    # What the mount holds, before what one file in it says: a file
    # that is not there is then reported beside the tree it is missing
    # from, and beside what the terminal claimed to have mounted.
    mounted_tree = sorted(fs.list("/workspace/old", recursive=True))
    assert "app/index.html" in mounted_tree, (said, mounted_tree)
    assert "notes/loader.md" in mounted_tree, (said, mounted_tree)
    assert fs.read("/workspace/old/app/index.html") == b"<h1>revenue</h1>"
    assert fs.read("/workspace/old/notes/loader.md").decode() == NOTE

    # TAKEN FROM: one path, landed here
    _turn(
        builder,
        f'!tool terminal {{"command": "ws-git checkout {tag} -- notes/loader.md"}}\n'
        "!text Took the note.",
    )
    assert fs.read("/workspace/notes/loader.md").decode() == NOTE

    # ASKED: a clone of the agent that built it, memory included
    answer = _delegate(
        registry,
        builder,
        "!text The loader decodes UTF-16.",
        fork_from=tag,
        inherit="full",
    )
    assert answer.status == "answered"
    # The chat db reads the clone as its own session: the maker's runs
    # first, then the turn the question just produced, which the db
    # accepted as the child's — memory read and memory written.
    recalled = _run_ids(registry, answer.branch)
    assert recalled[: len(remembered)] == remembered
    assert len(recalled) == len(remembered) + 1
    # and the files came with it, which a fresh inherit would give too
    child = registry.open(answer.branch)
    assert child.ws.files.fs.read("/workspace/notes/loader.md").decode() == NOTE


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


# -- a swept branch: the `expired` status ------------------------------------


def test_a_swept_delegate_is_not_deliverable(registry):
    """Retention for a delegate's branch is an idle TTL, and a sweep
    takes the branch of a job nobody read. What is left is a row whose
    status is `expired`, with no answer behind it: `result` on one
    raises rather than answering, so an expired job is not something the
    next turn can deliver and not something the rail should count."""
    parent = registry.open("boss")
    _turn(parent, ASK_ASYNC)
    _await_delegates(parent)
    assert [r["delegates"] for r in registry.list() if r["name"] == "boss"] == [1]

    assert parent.delegates.sweep(idle=0, min_age=0) == ["boss.scout"]

    assert parent.answered_delegates() == []
    assert parent.take_delegate_answers() == []
    # the badge counts what a turn would deliver, so it clears with it
    assert [r["delegates"] for r in registry.list() if r["name"] == "boss"] == [0]
    assert not any(e["type"] == "delegate" for e in _turn(parent, "!text ok"))


class _SweptBetween:
    """A job table whose answered job is swept between the listing and
    the read — the race the delivery loop has to survive."""

    def __init__(self) -> None:
        self.status = "answered"
        self.reads = 0

    def list(self):
        from types import SimpleNamespace

        return [SimpleNamespace(name="boss.scout", status=self.status)]

    def result(self, name):
        self.reads += 1
        raise BranchExpired(f"job {name!r} expired: its branch was swept")

    def close(self):
        """Closing the session closes its job table; this one holds
        nothing to release."""


def test_a_delegate_swept_mid_delivery_is_dropped_not_retried(registry):
    """A sweep can land between the listing and the read. The turn
    skips that job instead of failing, and the turn after it does not
    try again: the row says `expired` by then, and a job with no branch
    left has nothing to deliver however many turns ask."""
    parent = registry.open("boss")
    parent.delegates = _SweptBetween()

    assert parent.take_delegate_answers() == []
    assert parent.delegates.reads == 1

    parent.delegates.status = "expired"
    assert parent.answered_delegates() == []
    assert parent.take_delegate_answers() == []
    assert parent.delegates.reads == 1  # never asked for a swept branch again


# -- the ws-git gate: one answer, from the function that wired it ------------


def test_a_session_records_whether_ws_git_installed(registry):
    """`register_wsgit` says whether the agent can type the verb, and
    the session keeps that answer. Everything that teaches the verb —
    the primer, a delegate's brief — asks the session rather than
    re-reading the runtime flags the function read itself."""
    session = registry.open("boss")
    assert session.wsgit is True
    assert sessions_mod._versioning_primer(True) is sessions_mod.VERSIONING_PRIMER
    assert sessions_mod._versioning_primer(False) == ""


def test_no_verb_no_delegation_half(registry, monkeypatch):
    """An executor that can carry neither an injected command nor a
    ferried ws-* verb installs nothing, and the agent is told so: it is
    taught to ask a delegate for findings instead of for edits, and
    never taught a spelling that answers `command not found`."""
    monkeypatch.setattr(sessions_mod, "register_wsgit", lambda ws: False)
    session = registry.open("verbless")

    assert session.wsgit is False
    primer = sessions_mod._delegation_primer(True, False)
    assert primer is sessions_mod.NO_VERSIONING_PRIMER
    assert "ws-git" not in primer
    assert "ws-git" not in session.agent.instructions
    # and a delegate of that session opens with the same honesty
    assert delegates.VERSIONING not in delegates.brief("boss", None, versioning=False)
    assert delegates.VERSIONING in delegates.brief("boss", None, versioning=True)


def test_a_delegates_brief_follows_the_sessions_own_answer(registry, monkeypatch):
    """The brief is built from what the child session recorded, not
    from a second reading of the executor's flags."""
    monkeypatch.setattr(sessions_mod, "register_wsgit", lambda ws: False)
    parent = registry.open("boss")
    runner = delegates.StudioRunner(registry, "boss", 1)

    assert delegates.VERSIONING not in runner._brief(parent, None)

    parent.wsgit = True
    assert delegates.VERSIONING in runner._brief(parent, None)


def test_a_delegate_can_start_from_another_sessions_commit(registry):
    """What the primer promises about `fork_from`, end to end: every
    session in this studio is in one store, so a commit of one is a
    fork point for a delegate of another — it opens holding that
    session's tree, and its answer still comes back to the session that
    asked."""
    other = registry.open("other")
    other.ws.files.write("/workspace/from_other.md", "written elsewhere")
    other.ws.commit(info={"tool": "test"})
    parent = registry.open("boss")

    answer = _delegate(
        registry,
        parent,
        '!tool terminal {"command": "cat /workspace/from_other.md"}\n'
        "!text Read the other session's file.",
        fork_from=f"other@{other.ws.head}",
    )

    assert answer.status == "answered"
    child = registry.open(answer.branch)
    assert child.ws.files.fs.read("/workspace/from_other.md") == b"written elsewhere"
    # and it started there, not here: the asking session never had it
    assert not parent.ws.files.fs.exists("/workspace/from_other.md")


def test_a_delegates_own_turn_is_run_with_the_registry(registry, monkeypatch):
    """The runner drives a delegate's turn the way the server drives a
    human's, and that includes handing over the registry: a delegate
    may delegate, and what its job table knows is written down on the
    turn that ends — the runner releases the child, and the table, as
    soon as it answers."""
    seen = []
    original = server._run_turn

    async def spy(session, message, reg=None):
        seen.append(reg)
        await original(session, message, reg)

    monkeypatch.setattr(server, "_run_turn", spy)
    parent = registry.open("boss")

    _delegate(registry, parent, WRITE_A_NOTE)

    assert seen and all(reg is registry for reg in seen)


def test_a_delegate_that_kept_its_own_delegate_is_believed_after_it_goes(registry):
    """The nested case, end to end: a delegate of a delegate is kept in
    the CHILD's job table, and that table is released with the child
    when it answers. The snapshot on the child's turn is the only thing
    that outlives it."""
    registry.open("boss")
    child = registry.open_delegate("boss", "boss.scout")
    child.delegates.ask("!text Had a look.", name="finch", wait=True)
    grandchild = "boss.scout.finch"
    assert _record(registry, grandchild)["parent"] == "boss.scout"
    child.delegates.keep(grandchild)

    _turn(child, "!text done", registry)
    registry.release(child.name)

    assert _record(registry, grandchild)["kept"] is True
    assert registry.sweep_delegates(now=time.time() + 10**6) == []
    assert grandchild in registry._store.sessions()


# -- retention: the studio schedules what nontainer supplies -----------------


def _record(registry, child=None):
    """The delegates record, or one child's row in it."""
    record = registry._manifest()["delegates"]
    return record if child is None else record[child]


def _tiny_ttl(tmp_path, hours=1e-9):
    """A registry whose delegates are idle the moment they answer."""
    return sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path,
        default_model="dummy",
        delegate_ttl=hours,
    )


def test_the_setting_is_hours_and_a_bad_one_falls_back(monkeypatch):
    """The TTL is read where the other settings are read, in the unit
    the decision is made in. A value that is not a number keeps the
    default rather than taking the studio down at startup."""
    assert sessions_mod._delegate_ttl_hours() == 24.0
    monkeypatch.setenv("NONTAINER_STUDIO_DELEGATE_TTL", "2")
    assert sessions_mod._delegate_ttl_hours() == 2.0
    monkeypatch.setenv("NONTAINER_STUDIO_DELEGATE_TTL", "overnight")
    assert sessions_mod._delegate_ttl_hours() == 24.0
    monkeypatch.setenv("NONTAINER_STUDIO_DELEGATE_TTL", "-3")
    assert sessions_mod._delegate_ttl_hours() == 0.0


def test_the_record_carries_what_the_sweep_measures(registry):
    """A delegate has been dealt with as of the moment it was asked
    for, and the record says so — the job table that also knows it
    does not survive a restart."""
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)

    row = _record(registry, answer.branch)
    assert row["parent"] == "boss"
    assert row["touched"] > time.time() - 60
    # nobody has said whether to keep it, which is not the same as
    # "no": the job table's own flag answers until somebody does
    assert row["kept"] is None


def test_an_older_record_ages_from_the_session_birthday(registry, tmp_path):
    """The record used to be `{child: parent}` and said nothing about
    retention. Reading one, a delegate ages from the only evidence on
    disk of when it was dealt with: when it was created."""
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)
    path = tmp_path / "sessions.json"
    manifest = json.loads(path.read_text())
    manifest["delegates"] = {answer.branch: "boss"}  # the old shape
    born = manifest["created"][answer.branch]
    path.write_text(json.dumps(manifest))

    row = _record(registry, answer.branch)
    assert row == {"parent": "boss", "touched": born, "kept": None}
    # and a record with no birthday either reads as never dealt with,
    # which is what lets the sweep collect it
    manifest["delegates"] = {answer.branch: "boss"}
    manifest["created"] = {}
    path.write_text(json.dumps(manifest))
    assert _record(registry, answer.branch)["touched"] == 0.0


def test_a_delegate_nobody_read_is_swept_after_the_ttl(tmp_path):
    """The live half: a job its session's helper still holds is swept
    by nontainer's own rule, and the studio takes the record, the
    session row and the transcript with the branch."""
    registry = _tiny_ttl(tmp_path)
    try:
        parent = registry.open("boss")
        _turn(parent, ASK_ASYNC, registry)
        _await_delegates(parent)
        child = "boss.scout"
        assert (tmp_path / "events" / f"{child}.jsonl").exists()

        assert registry.sweep_delegates() == [child]

        assert child not in registry._store.sessions()
        assert child not in registry.known()
        assert _record(registry) == {}
        assert not (tmp_path / "events" / f"{child}.jsonl").exists()
        # and the badge clears with it: a swept job has no answer left
        assert [r["delegates"] for r in registry.list() if r["name"] == "boss"] == [0]
    finally:
        registry.close()


def test_a_delegate_from_before_a_restart_is_swept_too(registry, tmp_path):
    """The half nontainer cannot reach. A job table is per `Sessions`
    object, built per live session in this process, so a delegate asked
    for before the last restart is in no table — the record is what
    remembers it, and the studio sweeps it through the store itself."""
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)
    registry.close()

    reborn = sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path,
        default_model="dummy",
    )
    try:
        assert reborn._live_jobs() == {}  # nothing here knows that job
        assert reborn.sweep_delegates() == []  # and it is not idle yet

        assert reborn.sweep_delegates(now=time.time() + 25 * 3600) == [answer.branch]
        assert answer.branch not in reborn._store.sessions()
        assert _record(reborn) == {}
    finally:
        reborn.close()


def test_the_sweep_runs_when_the_registry_opens(registry, tmp_path):
    """Retention is a verb somebody schedules, and opening the store is
    the first of the two moments the studio schedules it for."""
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)
    registry.close()

    reborn = _tiny_ttl(tmp_path)
    try:
        assert answer.branch not in reborn._store.sessions()
        assert _record(reborn) == {}
    finally:
        reborn.close()


def test_no_ttl_no_sweep(registry, tmp_path):
    """`0` is retention off: nothing ages out, however long ago it was
    asked for."""
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)
    registry.close()

    reborn = sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path,
        default_model="dummy",
        delegate_ttl=0,
    )
    try:
        assert reborn.sweep_delegates(now=time.time() + 10**6) == []
        assert answer.branch in reborn._store.sessions()
    finally:
        reborn.close()


def test_a_kept_delegate_is_not_swept(registry, tmp_path):
    """`keep` is the flag that exempts a delegate for good, and the
    studio's record of it is what a sweep after a restart reads."""
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)
    registry.keep_delegate("boss", answer.branch, True)

    assert _record(registry, answer.branch)["kept"] is True
    assert registry.sweep_delegates(now=time.time() + 10**6) == []
    assert answer.branch in registry._store.sessions()


def test_a_keep_the_agent_asked_for_survives_a_restart(tmp_path):
    """`keep` flags a job, and the job table goes when the process
    does. The snapshot at the end of the turn is what carries the flag
    over — without it the delegate an agent asked to keep last night is
    swept this morning."""
    registry = _tiny_ttl(tmp_path, hours=24)
    try:
        parent = registry.open("boss")
        _turn(parent, ASK_ASYNC, registry)
        _await_delegates(parent)
        parent.delegates.keep("boss.scout")  # what the tool's keep calls

        _turn(parent, "!text ok", registry)

        assert _record(registry, "boss.scout")["kept"] is True
    finally:
        registry.close()

    reborn = _tiny_ttl(tmp_path)  # sweeps at open, with no table left
    try:
        assert "boss.scout" in reborn._store.sessions()
        assert reborn.sweep_delegates(now=time.time() + 10**6) == []
    finally:
        reborn.close()


def test_a_delivered_answer_counts_as_dealing_with_the_delegate(tmp_path):
    """Retention is an IDLE ttl: reading an answer moves the clock, and
    the studio reads answers on the parent's next turn. The record has
    to move with it or a delegate read this morning ages from the ask."""
    registry = _tiny_ttl(tmp_path, hours=24)
    try:
        parent = registry.open("boss")
        _turn(parent, ASK_ASYNC, registry)
        _await_delegates(parent)
        asked = _record(registry, "boss.scout")["touched"]
        time.sleep(0.01)

        _turn(parent, "what did the scout say?", registry)

        assert _record(registry, "boss.scout")["touched"] > asked
    finally:
        registry.close()


def test_un_keeping_is_the_manifest_and_it_outranks_the_live_job(tmp_path):
    """nontainer has no un-keep: a job it flagged stays flagged for as
    long as its session lives. So the record carries the later stamp
    and every reader takes the human's answer from it — including the
    sweep, once that job table is gone."""
    registry = _tiny_ttl(tmp_path, hours=24)
    try:
        parent = registry.open("boss")
        _turn(parent, ASK_ASYNC, registry)
        _await_delegates(parent)
        registry.keep_delegate("boss", "boss.scout", True)
        assert parent.delegates.list()[0].kept is True

        row = registry.keep_delegate("boss", "boss.scout", False)

        assert row["kept"] is False  # the live job still says True
        assert parent.delegates.list()[0].kept is True
        assert registry.delegate_rows("boss")[0]["kept"] is False
        # and a turn's snapshot does not put the flag back
        _turn(parent, "!text ok", registry)
        assert _record(registry, "boss.scout")["kept"] is False
    finally:
        registry.close()

    # the record is all that is left after a restart, and it says no
    reborn = _tiny_ttl(tmp_path)  # sweeps at open
    try:
        assert "boss.scout" not in reborn._store.sessions()
        assert _record(reborn) == {}
    finally:
        reborn.close()


def test_a_kept_grandchild_holds_its_whole_subtree(registry, tmp_path):
    """A delegate may delegate, and a swept parent's subtree goes with
    it. Something somebody asked to keep stops that: the branch under
    it is the one they meant to come back to, and it is only reachable
    through the record the parent's deletion would take."""
    registry.open("boss")
    registry.open_delegate("boss", "boss.scout")
    registry.open_delegate("boss.scout", "boss.scout.finch")
    registry.release("boss.scout")
    registry.release("boss.scout.finch")
    registry.keep_delegate("boss.scout", "boss.scout.finch", True)

    assert registry.sweep_delegates(now=time.time() + 10**6) == []
    assert "boss.scout" in registry._store.sessions()
    assert "boss.scout.finch" in registry._store.sessions()

    # let the grandchild go and the subtree goes together
    registry.keep_delegate("boss.scout", "boss.scout.finch", False)
    swept = registry.sweep_delegates(now=time.time() + 10**6)
    assert swept == ["boss.scout", "boss.scout.finch"]
    assert _record(registry) == {}


def test_a_kept_grandchild_holds_a_live_parents_branch(tmp_path):
    """The subtree rule has to hold for a delegate whose job is still
    in a live table, which is the case nontainer's own sweep decides.
    It sweeps a whole table in one go and takes no exclusion list, so
    the studio has to spare the root BEFORE that runs — after it, the
    branch is gone and so is the record the subtree hangs off."""
    registry = _tiny_ttl(tmp_path)  # everything is idle the moment it lands
    try:
        parent = registry.open("boss")
        _turn(parent, ASK_ASYNC, registry)  # a live job for boss.scout
        _await_delegates(parent)
        registry.open_delegate("boss.scout", "boss.scout.finch")
        registry.release("boss.scout.finch")
        registry.keep_delegate("boss.scout", "boss.scout.finch", True)

        assert registry.sweep_delegates() == []

        assert "boss.scout" in registry._store.sessions()
        assert "boss.scout.finch" in registry._store.sessions()
        assert set(_record(registry)) == {"boss.scout", "boss.scout.finch"}
        # and holding it back is not a keep: nobody asked for one, so
        # the record still says nobody has, and the rail agrees
        assert _record(registry, "boss.scout")["kept"] is None
        assert registry.delegate_rows("boss")[0]["kept"] is False

        # freeing the grandchild frees the subtree — for the next run,
        # since nontainer's flag cannot be taken off a live job
        registry.keep_delegate("boss.scout", "boss.scout.finch", False)
    finally:
        registry.close()

    reborn = _tiny_ttl(tmp_path)  # sweeps at open, with no table left
    try:
        assert "boss.scout" not in reborn._store.sessions()
        assert _record(reborn) == {}
    finally:
        reborn.close()


def test_the_sweep_leaves_a_delegate_the_registry_holds_open(registry):
    """A kvgit handle pins its branch, so the store refuses to delete
    one that is open — and a delegate with a run in flight is open."""
    registry.open("boss")
    child = registry.open_delegate("boss", "boss.scout")

    assert registry.sweep_delegates(now=time.time() + 10**6) == []
    assert child.name in registry._store.sessions()

    registry.release(child.name)
    assert registry.sweep_delegates(now=time.time() + 10**6) == [child.name]


def test_an_open_grandchild_holds_the_subtree_it_is_in(registry):
    """A delegate's own delegates go with it, so one of them being open
    is the parent's problem too: the store refuses to delete a pinned
    branch, and a sweep that asked anyway would lose the whole pass."""
    registry.open("boss")
    registry.open_delegate("boss", "boss.scout")
    registry.open_delegate("boss.scout", "boss.scout.finch")
    registry.release("boss.scout")

    assert registry.sweep_delegates(now=time.time() + 10**6) == []
    assert "boss.scout" in registry._store.sessions()

    registry.release("boss.scout.finch")
    swept = registry.sweep_delegates(now=time.time() + 10**6)
    assert swept == ["boss.scout", "boss.scout.finch"]


def test_the_rows_say_what_became_of_each_delegate(tmp_path):
    """The per-session listing: status off the live job where a table
    holds one, and a row that says so where none does."""
    registry = _tiny_ttl(tmp_path, hours=24)
    try:
        parent = registry.open("boss")
        _turn(parent, ASK_ASYNC, registry)
        _await_delegates(parent)

        (row,) = registry.delegate_rows("boss")
        assert row["name"] == "boss.scout"
        assert row["status"] == "answered"
        assert row["known"] is True
        assert row["kept"] is False
    finally:
        registry.close()

    reborn = _tiny_ttl(tmp_path, hours=24)
    try:
        (row,) = reborn.delegate_rows("boss")
        # no job table survived the restart: what is left is the record,
        # which says this delegate was asked for and its branch is here
        assert row["known"] is False
        assert row["status"] == "answered"
    finally:
        reborn.close()


def test_a_delegate_of_another_session_is_not_this_ones_to_keep(registry):
    parent = registry.open("boss")
    answer = _delegate(registry, parent, WRITE_A_NOTE)
    registry.open("other")

    assert registry.delegate_rows("other") == []
    with pytest.raises(KeyError):
        registry.keep_delegate("other", answer.branch, True)


def test_the_primer_says_the_number_and_the_verb(registry, tmp_path):
    """nontainer's `sessions` tool says `keep` exists. Whether anything
    sweeps, and on what clock, is the studio's to say — so the agent is
    told the hours and that it is on."""
    primer = sessions_mod._retention_primer(24)
    assert "24 hours" in primer
    assert "sessions keep" in primer
    assert sessions_mod._retention_primer(0) == ""
    assert primer in registry.open("boss").agent.instructions

    off = sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path / "off",
        default_model="dummy",
        delegate_ttl=0,
    )
    try:
        assert "is swept" not in off.open("boss").agent.instructions
    finally:
        off.close()


# -- the nesting cap: a delegate is a full agent ----------------------------


def _tool(session):
    """The `sessions` tool as the agent holds it."""
    return next(
        t for t in session.agent.tools if getattr(t, "__name__", "") == "sessions"
    )


def _nest(registry, *names):
    """A chain of delegates, each forked by the one before it."""
    session = registry.open(names[0])
    for parent, child in zip(names, names[1:]):
        session = registry.open_delegate(parent, child)
    return session


def test_the_depth_setting_is_hops_and_a_bad_one_falls_back(monkeypatch, tmp_path):
    """Read where the other settings are read, in the unit the rule is
    stated in. A value that is not a number keeps the default rather
    than taking the studio down at startup."""
    assert sessions_mod._delegate_depth() == 2
    monkeypatch.setenv("NONTAINER_STUDIO_DELEGATE_DEPTH", "deep")
    assert sessions_mod._delegate_depth() == 2
    monkeypatch.setenv("NONTAINER_STUDIO_DELEGATE_DEPTH", "-1")
    assert sessions_mod._delegate_depth() == 0

    monkeypatch.setenv("NONTAINER_STUDIO_DELEGATE_DEPTH", "1")
    registry = sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path,
        default_model="dummy",
    )
    try:
        # one hop: the session a human started delegates, and its
        # delegate does the work itself
        child = _nest(registry, "boss", "boss.scout")
        assert registry.delegate_depth == 1
        assert registry.at_depth_cap("boss") is False
        assert "refused" in _tool(child)(action="ask", task="have a look")
    finally:
        registry.close()


def test_depth_is_counted_off_the_record_and_not_off_the_name(registry):
    """A dot is a naming convention and a human may type one, so the
    hops are walked over who forked whom."""
    _nest(registry, "boss", "boss.scout", "boss.scout.finch")
    registry.open("boss.notes")  # a human's own session, named under boss

    assert registry.depth_of("boss") == 0
    assert registry.depth_of("boss.scout") == 1
    assert registry.depth_of("boss.scout.finch") == 2
    assert registry.depth_of("boss.notes") == 0
    assert registry.depth_of("nobody") == 0


def test_a_grandchild_is_refused_a_delegate_and_told_what_to_do(registry):
    """Two hops is as deep as forks nest by default: the session a
    human started delegates, its delegate delegates, and the one after
    that does the work itself."""
    grandchild = _nest(registry, "boss", "boss.scout", "boss.scout.finch")

    refused = _tool(grandchild)(action="ask", task="have a look at this")

    assert "refused" in refused
    # the way forward, not just the wall
    assert "Do the task yourself" in refused
    assert "answer with what you have found" in refused
    # and nothing was forked to say it
    assert registry.delegates_of("boss.scout.finch") == []
    # the rest of the tool is untouched
    assert "no delegated jobs yet" in _tool(grandchild)(action="list")


def test_the_asks_above_the_cap_go_through(registry):
    """A missing task is nontainer's refusal, not the studio's: reading
    it back is how a test knows the ask reached the dispatch rather
    than stopping at the gate."""
    _nest(registry, "boss", "boss.scout")

    for name in ("boss", "boss.scout"):
        assert registry.at_depth_cap(name) is False
        assert "needs a task" in _tool(registry.open(name))(action="ask")


def test_zero_is_no_cap_at_all(tmp_path):
    """`0` turns the cap off: delegation nests as far as the agents
    take it."""
    registry = sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path,
        default_model="dummy",
        delegate_depth=0,
    )
    try:
        deep = _nest(registry, "boss", "boss.a", "boss.a.b", "boss.a.b.c")
        assert registry.depth_of(deep.name) == 3
        assert registry.at_depth_cap(deep.name) is False
        assert "needs a task" in _tool(deep)(action="ask")
    finally:
        registry.close()


def test_only_the_session_at_the_cap_is_told_about_it(registry):
    """A cap nobody above it will hit is a sentence every session pays
    for in prompt, so it is told to the one it binds."""
    grandchild = _nest(registry, "boss", "boss.scout", "boss.scout.finch")

    assert sessions_mod.DEPTH_CAP_PRIMER in grandchild.agent.instructions
    for name in ("boss", "boss.scout"):
        assert sessions_mod.DEPTH_CAP_PRIMER not in (
            registry.open(name).agent.instructions
        )


# -- the tool-call cap: a delegate's loop has nobody watching it ------------


def test_the_tool_call_setting_is_calls_and_a_bad_one_falls_back(monkeypatch):
    """Read where the other settings are read. A value that is not a
    number keeps the default rather than taking the studio down at
    startup."""
    assert sessions_mod._delegate_tool_calls() == 60
    monkeypatch.setenv("NONTAINER_STUDIO_DELEGATE_TOOL_CALLS", "12")
    assert sessions_mod._delegate_tool_calls() == 12
    monkeypatch.setenv("NONTAINER_STUDIO_DELEGATE_TOOL_CALLS", "lots")
    assert sessions_mod._delegate_tool_calls() == 60
    monkeypatch.setenv("NONTAINER_STUDIO_DELEGATE_TOOL_CALLS", "-1")
    assert sessions_mod._delegate_tool_calls() == 0


def test_only_a_delegates_agent_carries_the_cap(registry):
    """The human's session is watched and can be stopped; a delegate's
    turn is neither, which is the whole of why one has the cap and the
    other does not."""
    parent = registry.open("boss")
    child = registry.open_delegate("boss", "boss.scout")

    assert parent.agent.tool_call_limit is None
    assert child.agent.tool_call_limit == registry.delegate_tool_calls == 60


def test_no_cap_no_limit(tmp_path):
    """`0` is the cap off: a delegate's loop is bounded by its turns
    and nothing else."""
    registry = sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path,
        default_model="dummy",
        delegate_tool_calls=0,
    )
    try:
        registry.open("boss")
        child = registry.open_delegate("boss", "boss.scout")
        assert child.agent.tool_call_limit is None
    finally:
        registry.close()


def _turn_that_reports_its_cancel(loop, seen, *, sleep=10):
    async def turn():
        try:
            await asyncio.sleep(sleep)
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise
        seen.append("finished")

    return loop.create_task(turn())


def test_a_turn_registered_after_the_sweep_is_stopped_on_arrival(registry):
    """`sessions ask` queues a worker, and a worker still on its way
    to registering when shutdown swept the table is one the sweep
    never saw. Registering after the sweep is what stops it."""
    assert registry.stop_delegate_runs() == []
    loop = asyncio.new_event_loop()
    seen = []
    try:
        task = _turn_that_reports_its_cancel(loop, seen)
        registry.hold_delegate_run("boss.scout", loop, task)
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(task)
    finally:
        registry.drop_delegate_run("boss.scout")
        loop.close()
    assert seen == ["cancelled"]


def test_a_turn_registered_before_any_sweep_runs(registry):
    """The control: with no sweep behind it, registering stops nothing."""
    loop = asyncio.new_event_loop()
    seen = []
    try:
        task = _turn_that_reports_its_cancel(loop, seen, sleep=0)
        registry.hold_delegate_run("boss.scout", loop, task)
        loop.run_until_complete(task)
    finally:
        registry.drop_delegate_run("boss.scout")
        loop.close()
    assert seen == ["finished"]


def test_a_delegate_past_the_cap_stops_calling_and_still_answers(tmp_path):
    """What the cap buys, end to end: the calls past it do not run, and
    the turn still resolves into an answer rather than hanging."""
    registry = sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path,
        default_model="dummy",
        delegate_tool_calls=2,
    )
    try:
        parent = registry.open("boss")
        answer = _delegate(
            registry,
            parent,
            "\n".join(
                '!tool file_write {"path": "/workspace/n%d.md", "content": "%d"}'
                % (i, i)
                for i in range(3)
            )
            + "\n!text Wrote what I could.",
        )

        assert answer.status == "answered"
        assert answer.text == "Wrote what I could."
        child = registry.open(answer.branch)
        assert child.ws.files.fs.exists("/workspace/n0.md")
        assert child.ws.files.fs.exists("/workspace/n1.md")
        assert not child.ws.files.fs.exists("/workspace/n2.md")
    finally:
        registry.close()


# -- delegates a restart parted from their answers --------------------------


def _reborn(tmp_path):
    """A registry over a store some earlier process was using."""
    return sessions_mod.Registry(
        model_factory=lambda *a, **k: DummyModel(),
        store=tmp_path,
        default_model="dummy",
    )


def _asked_before_a_restart(tmp_path):
    """A parent with a delegate in the store and no job table left."""
    registry = _reborn(tmp_path)
    try:
        parent = registry.open("boss")
        _turn(parent, ASK_ASYNC, registry)
        _await_delegates(parent)
    finally:
        registry.close()


def test_a_delegate_from_before_a_restart_is_named_on_the_next_turn(tmp_path):
    """The job table is per process and the branch is in the store, so
    a restart parts them: the answer is gone and the branch is not.
    Nothing else would say so — the live helper lists no job, so
    `sessions list` reads as though the delegate was never asked
    for."""
    _asked_before_a_restart(tmp_path)

    registry = _reborn(tmp_path)
    try:
        parent = registry.open("boss")
        assert parent.delegates.list() == []
        assert "boss.scout" in registry._store.sessions()

        events = _turn(parent, "where are we?", registry)

        note = next(e for e in events if e["type"] == "delegate")
        assert note["name"] == "boss.scout"
        assert note["status"] == "unanswered"
        assert "before the studio restarted" in note["text"]
        # what is left of it, and how to ask again
        assert "ws-git diff boss.scout" in note["text"]
        assert "ws-git merge boss.scout" in note["text"]
        assert "sessions ask" in note["text"]
        assert "resume" in note["text"]

        # the model was sent it too, ahead of the human's message (the
        # dummy echoes what it was asked)
        reply = "".join(e["delta"] for e in events if e["type"] == "text")
        assert reply.startswith("dummy: [delegate `boss.scout`")

        # once: the turn after it carries nothing
        assert not any(
            e["type"] == "delegate" for e in _turn(parent, "!text ok", registry)
        )
    finally:
        registry.close()

    # and not again after another restart: the transcript still shows
    # the note, and delivery is a fact of the transcript
    again = _reborn(tmp_path)
    try:
        assert not any(
            e["type"] == "delegate"
            for e in _turn(again.open("boss"), "!text still ok", again)
        )
    finally:
        again.close()


def test_a_rewind_past_the_note_delivers_it_again(tmp_path):
    """An edit unsays the turn the note landed in, so the replacement
    turn has to carry it or the rewound conversation never hears of
    that delegate."""
    _asked_before_a_restart(tmp_path)

    registry = _reborn(tmp_path)
    try:
        parent = registry.open("boss")
        delivered = _turn(parent, "where are we?", registry)
        assert [e["name"] for e in delivered if e["type"] == "delegate"] == [
            "boss.scout"
        ]

        seq = next(e["seq"] for e in delivered if e["type"] == "user")
        again = _edit(registry, parent, seq, "actually, where are we?")

        assert [e["name"] for e in again if e["type"] == "delegate"] == ["boss.scout"]
        # once, though: the turn after it is not a third delivery
        assert not any(
            e["type"] == "delegate" for e in _turn(parent, "!text ok", registry)
        )
    finally:
        registry.close()


def test_the_note_stays_delivered_past_the_event_window(tmp_path, monkeypatch):
    """Memory holds the transcript's tail. A delivery that has aged
    out of it is still on disk, and that is where the check looks
    before it names the same delegate a second time."""
    _asked_before_a_restart(tmp_path)

    registry = _reborn(tmp_path)
    try:
        parent = registry.open("boss")
        first = _turn(parent, "where are we?", registry)
        assert [e["name"] for e in first if e["type"] == "delegate"] == ["boss.scout"]

        monkeypatch.setattr(sessions_mod, "MAX_EVENTS", 2)
        for _ in range(3):
            _turn(parent, "!text ok", registry)
        assert len(parent.events) <= 2
        assert not any(e.get("type") == "delegate" for e in parent.events)

        assert registry.orphaned_delegates(parent) == []
        assert not any(
            e["type"] == "delegate" for e in _turn(parent, "!text ok", registry)
        )
    finally:
        registry.close()


def test_a_swept_delegate_is_not_named(tmp_path):
    """The note points at a branch. One the sweep has taken has no
    branch and no record, so there is nothing to point at."""
    _asked_before_a_restart(tmp_path)

    registry = _tiny_ttl(tmp_path)  # sweeps at open, with no table left
    try:
        parent = registry.open("boss")
        assert "boss.scout" not in registry._store.sessions()
        assert registry.orphaned_delegates(parent) == []
        assert not any(
            e["type"] == "delegate" for e in _turn(parent, "!text ok", registry)
        )
    finally:
        registry.close()


def test_a_delegate_with_a_live_job_is_not_named(registry):
    """The note is for the ones no table remembers. A delegate this
    process asked for is in its parent's table, and its answer is
    delivered the ordinary way."""
    parent = registry.open("boss")
    _turn(parent, ASK_ASYNC, registry)
    _await_delegates(parent)

    assert registry.orphaned_delegates(parent) == []
    delivered = _turn(parent, "what did the scout say?", registry)
    note = next(e for e in delivered if e["type"] == "delegate")
    assert note["status"] == "answered"


# -- shutdown does not wait out a delegate ----------------------------------


class BlockingModel(DummyModel):
    """A model whose turn never ends on its own.

    The wait is on the turn's own loop, so only a cancel arriving
    there ends it — which is exactly the thing under test, and a wait
    on a worker thread would outlive the loop it was started from.
    """

    def __init__(self, entered):
        super().__init__()
        self._entered = entered

    async def ainvoke_stream(self, messages, **kwargs):
        self._entered.set()
        await asyncio.Event().wait()
        yield ModelResponse(role="assistant", content="unreachable")


def test_closing_the_registry_does_not_wait_out_a_delegates_turn(tmp_path):
    """Closing a session joins its delegate workers, so a delegate
    mid-turn used to hold Ctrl-C for as many turns as it had left.
    The turns are asked to stop first."""
    entered = threading.Event()
    registry = sessions_mod.Registry(
        model_factory=lambda *a, **k: BlockingModel(entered),
        store=tmp_path,
        default_model="dummy",
    )
    parent = registry.open("boss")
    closed = False
    try:
        parent.delegates.ask("a task it will never finish", name="scout")
        assert entered.wait(20), "the delegate's turn never started"

        began = time.monotonic()
        registry.close()
        closed = True
        assert time.monotonic() - began < 10
    finally:
        if not closed:
            registry.close()

    # the child's own transcript records the cut
    log = (tmp_path / "events" / "boss.scout.jsonl").read_text()
    cut = [json.loads(line) for line in log.splitlines()]
    assert [e for e in cut if e["type"] == "error"] == [
        {"type": "error", "message": server.STOPPED_AT_SHUTDOWN, "seq": 1}
    ]
    assert cut[-1]["type"] == "done"  # the turn was closed out, not abandoned

    # and the job it belonged to resolved, in words
    answer = parent.delegates.result("boss.scout")
    assert answer.status == "failed"
    assert answer.text == server.STOPPED_AT_SHUTDOWN


# -- an answer that lands mid-turn -------------------------------------------


ASK_A_SCOUT = (
    '!tool sessions {"action": "ask", "name": "scout", '
    '"task": "!text Found it."}\n'
    "!text Sent a scout."
)

WRITE_SOMETHING = (
    '!tool file_write {"path": "/workspace/looked.md", "content": "looking"}\n'
    "!text Had a look."
)


@pytest.fixture
def thinking_model(monkeypatch):
    """A model that takes half a second to decide on its first tool
    call, as every real one does. The dummy answers instantly, which
    closes a window a real session always has open: the stretch of a
    turn between the message and the first tool result, where a
    delegate can land."""
    plain = DummyModel.ainvoke_stream

    async def slow_first_call(self, messages, **kwargs):
        if DummyModel._plan(messages).tool_calls:
            await asyncio.sleep(0.5)
        async for chunk in plain(self, messages, **kwargs):
            yield chunk

    monkeypatch.setattr(DummyModel, "ainvoke_stream", slow_first_call)


def test_an_answer_that_lands_mid_turn_is_delivered_once(registry, thinking_model):
    """A delegate that answers after a turn has begun reaches the
    parent THERE, appended to the next tool result, instead of waiting
    for the turn after. It is the same fact either way, so it is the
    same `delegate` event — and that event is the delivery record, so
    nothing carries the answer a second time."""
    parent = registry.open("boss")
    _turn(parent, ASK_A_SCOUT, registry)
    # deliberately NOT awaited: the next turn begins while the scout is
    # still working, so the between-turns path has nothing to hand over
    # and the answer has to arrive mid-turn or not at all
    assert parent.answered_delegates() == []

    events = _turn(parent, WRITE_SOMETHING, registry)
    kinds = [e["type"] for e in events]
    answer = next(e for e in events if e["type"] == "delegate")
    assert answer["name"] == "boss.scout"
    assert answer["status"] == "answered"
    # the studio's own framing, the one the between-turns path emits
    assert "not the person at the keyboard" in answer["text"]
    assert "Found it." in answer["text"]
    # mid-turn: after this turn's own message and its tool call, and
    # before the turn ends
    assert kinds.index("delegate") > kinds.index("tool_start")
    assert kinds.index("delegate") < kinds.index("done")

    # the tool box shows the tool's own output; the answer is its own event
    for event in events:
        if event["type"] == "tool_end":
            assert "---- inbox ----" not in event["result"]
    # the model read it where the transcript says it arrived
    stored = registry.db.get_session("boss")
    carried = [
        str(m.content)
        for run in (stored.runs or [])
        for m in (run.messages or [])
        if m.role == "tool" and "---- inbox ----" in str(m.content)
    ]
    assert len(carried) == 1
    assert "Found it." in carried[0]
    assert "the delegation mechanism speaking" in carried[0]

    # delivered once: the transcript says so, so nothing carries it again
    assert parent.answered_delegates() == []
    assert parent.take_delegate_answers() == []
    assert [r["delegates"] for r in registry.list() if r["name"] == "boss"] == [0]
    assert not any(e["type"] == "delegate" for e in _turn(parent, "!text ok", registry))
