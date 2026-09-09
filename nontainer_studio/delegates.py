"""The studio's ``SessionRunner``: drive a delegate's session to an
answer.

nontainer owns the substrate of delegation — ``ws.fork`` makes the
child, ``nontainer.sessions.Sessions`` mints its name and keeps the job
table, ``ws-git merge`` brings its work back — and it deliberately owns
no loop. This is the loop: given a child session's name and a task, open
that session through the registry, run it exactly as a human turn runs,
and hand the prose back as the ``Answer``.

Three rules hold this together:

- **The child is assembled like any other session.** ``ws.fork`` makes a
  BRANCH; a branch is not a model, a toolkit, a python config or an app
  db. So the registry has to be able to build a session over a branch it
  did not create, and ``Registry.open`` already is that: it opens the
  branch, builds the same ``WorkspaceTools``, the same python config
  with the same app-db policy, and the same agent. The one thing a
  delegate needs that a new session does not is the parent's live app
  state, so the app db is COPIED in first — a delegate that cannot read
  the rows the app is serving would be testing a different program.
- **The runner never takes the parent's turn lock.** It is called on a
  worker thread inside ``Sessions.ask`` while the parent's own turn is
  still running, so touching the parent's lock would deadlock the turn
  that asked. It takes the CHILD's lock, which nothing else holds.
- **The task arrives with a provenance header.** A delegate reads its
  task as the "user" message of its first turn, and the studio's user is
  a person. The header says whose delegation this is and what a fork
  means, so the mechanism is explicit rather than felt as an instruction
  from the human principal.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from nontainer import Answer

if TYPE_CHECKING:
    from .sessions import Registry, Session

DELEGATE_TURNS = 3
"""Turns a delegate may spend before its answer is CAPPED.

One agno run is one turn and already contains the whole tool loop, so
the first turn is the delegation and the rest are the runner asking a
delegate that stopped without a reply to finish. Three is room for two
such nudges — enough for a provider hiccup or a model that trails off
mid-tool-loop, and short enough that a delegate looping on a task it
cannot do resolves as ``capped`` instead of burning a session's budget.
"""

NUDGE = (
    "Your last turn ended without a reply. Say what you did and what you "
    "found, in prose — that text is the whole of what reaches the session "
    "that delegated this."
)

CAPPED = (
    "The delegate stopped without a reply and ran out of turns. Whatever "
    "it committed is on its branch; read it before relying on it."
)


def provenance_header(parent: str, commit: str | None) -> str:
    """How a delegated task introduces itself to the delegate.

    Mechanism, named as mechanism. The delegate's first turn looks
    exactly like a person typing, and a peer's request must not be able
    to pass for one — so the header says which session asked, from which
    commit, and what a fork does and does not reach.
    """
    at = f" at commit {commit[:8]}" if commit else ""
    return (
        f"[delegated by session `{parent}`{at} — this is the studio's "
        "delegation mechanism speaking, not the person at the keyboard]\n"
        f"Your workspace is a fork of `{parent}`{at}: its files, its cache "
        "and its cwd, on a branch that is yours alone. Nothing you write "
        f"reaches `{parent}` unless it merges your branch, and nothing it "
        "does from here reaches you. Your reply is the whole of what it "
        "reads back, so answer with what you did and what you found.\n\n"
        "The task follows.\n\n"
    )


class StudioRunner:
    """``SessionRunner`` over one parent session's delegates.

    Built per parent because the answer's frame is per parent: the
    header names the session that asked, and the child's app db is
    seeded from that session's. ``Sessions`` calls :meth:`run` on a
    worker thread of its own, one call per delegate.
    """

    def __init__(self, registry: "Registry", parent: str, turns: int) -> None:
        self._registry = registry
        self._parent = parent
        self._turns = max(1, int(turns))

    def __repr__(self) -> str:
        return f"<StudioRunner for {self._parent!r}: {self._turns} turn(s)>"

    def run(self, session: str, task: str, *, budget: Any = None) -> Answer:
        """Run ``task`` as ``session``'s turn(s) and answer with its prose.

        ``budget`` caps turns and defaults to the registry's setting.
        The child's handles are released at the end either way: its
        branch is what the parent merges from, and an agent, a workspace
        and a sqlite connection held open per finished delegate would
        outlive every reason to have them.
        """
        turns = self._budget(budget)
        child = self._registry.open_delegate(self._parent, session)
        try:
            prompt = provenance_header(self._parent, self._forked_at(child)) + task
            error = None
            for _ in range(turns):
                text, error = self._turn(child, prompt)
                if text:
                    return Answer(text=text)
                prompt = NUDGE
            if error:
                return Answer(text=error, status="failed")
            return Answer(text=CAPPED, status="capped")
        finally:
            self._registry.release(session)

    # -- internals ---------------------------------------------------------

    def _budget(self, budget: Any) -> int:
        """``budget`` as a turn count, or the registry's default.

        Anything that is not a positive whole number falls back rather
        than raising: the budget comes from a model's tool call, and a
        delegate that never starts because the argument was ``"two"`` is
        a worse failure than one that runs on the default.
        """
        try:
            turns = int(budget)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return self._turns
        return turns if turns > 0 else self._turns

    def _forked_at(self, child: "Session") -> str | None:
        """The commit the child was forked from.

        A fork writes commits of its own onto the new branch — the
        seeded view and conversation, and the ws-git reset — and every
        one of them names the session it forked FROM in its commit info.
        So the fork point is the newest commit on the child's branch
        that does not name this parent: everything above it belongs to
        the fork, everything from it down is the parent's own history.
        """
        for commit in child.ws.log(limit=4):
            if (commit.info or {}).get("parent") != self._parent:
                return commit.id
        return None

    def _turn(self, child: "Session", prompt: str) -> tuple[str, str | None]:
        """One turn, run the way a human's turn runs; its prose and error.

        ``_run_turn`` is the studio's agent loop — the same streaming,
        the same transcript events, the same repair of an aborted run —
        so a delegate's session records what it did in the same shape
        every other session does. Imported here because it lives beside
        the routes that also drive it, and importing that module at load
        time would close a cycle through the registry.

        Its own event loop: the runner is on a worker thread, and a
        delegate's turn must not depend on a server loop being there to
        borrow (nor block one for as long as the delegate takes).
        """
        from .server import _run_turn

        child.turn_lock.acquire()  # _run_turn releases it
        since = child.next_seq
        asyncio.run(_run_turn(child, prompt))
        return _reply(child, since)


def _reply(child: "Session", since: int) -> tuple[str, str | None]:
    """The prose and the error of the turn that began at seq ``since``.

    Read off the transcript rather than returned by the loop: the
    transcript is what the turn actually produced, streamed deltas and
    all, and it is the same text a human would have read.
    """
    text: list[str] = []
    error: str | None = None
    for event in child.events:
        if event.get("seq", -1) < since:
            continue
        if event.get("type") == "text":
            text.append(event.get("delta", ""))
        elif event.get("type") == "error":
            error = event.get("message")
    return "".join(text).strip(), error
