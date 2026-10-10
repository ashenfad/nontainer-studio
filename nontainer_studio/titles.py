"""What a session is called: the title the human set, or one a model
wrote from the transcript, and when that one is due again.
"""

from __future__ import annotations

import logging

from .session import Session

log = logging.getLogger(__name__)

DEFAULT_TITLE = "New session"


TITLE_MAX = 60  # the rail is ~200px; anything longer is ellipsis anyway


TITLE_TURNS = 5
"""User turns between one generated title and the next. A session's
subject moves, and the name in the rail should move with it — but a
name that changed every turn would be something the human has to
re-read to find the session they left, and every generation is a model
call. Five turns is far enough apart to be cheap and near enough that
a session that became something else is not listed under what it was.
"""


def _title_cursor(event: dict) -> tuple[int, int] | None:
    """The transcript cursor a title event was generated from, or None
    for one recorded before the cursor rode along — which is a name
    nothing can date, and says so rather than inventing a position."""
    at_seq, turns = event.get("at_seq"), event.get("turns")
    if isinstance(at_seq, int) and isinstance(turns, int):
        return at_seq, turns
    return None


def _clean_title(title: object) -> str | None:
    """Free text -> a rail label, or None for "no title".

    The agent writes this via a tool, so it is untrusted shape: collapse
    every run of whitespace (a newline would break the row), bound the
    length, and treat blank as absent so a cleared user title reveals the
    agent's instead of shadowing it with "".
    """
    if not isinstance(title, str):
        return None
    text = " ".join(title.split())
    return text[:TITLE_MAX] or None


class TitlesMixin:
    """Session titles, as part of ``Registry``.

    A mixin over the registry's state (``_lock``, ``_default_model``) and
    its manifest methods, which ``Registry`` brings.
    """

    def title_of(self, name: str, manifest: dict | None = None) -> str:
        """The rail label. The human's own title always wins; the agent's
        fills the gap; neither means the session hasn't been named yet.
        Pass ``manifest`` to resolve a batch without re-reading the file
        (and to stay lock-free while a caller holds ``_lock``)."""
        entry = (manifest or self._manifest())["titles"].get(name) or {}
        return entry.get("user") or entry.get("agent") or DEFAULT_TITLE

    def set_user_title(self, name: str, title: str | None) -> str:
        """The human's override — outranks the agent forever. ``None``/
        blank CLEARS it, falling back to whatever the agent last said."""
        return self._set_title(name, "user", title)

    def set_agent_title(self, name: str, title: str | None) -> str:
        """The generated name. Always stored, even when a user title is
        hiding it: clearing theirs should reveal the latest name for the
        session, not a stale one."""
        return self._set_title(name, "agent", title)

    def _set_title(self, name: str, tier: str, title: str | None) -> str:
        """Write one tier of a session's title.

        Takes ``_lock``: a title is written from a worker thread, and
        this is a read-modify-write of the whole manifest.
        """
        with self._lock:
            manifest = self._manifest()
            entry = dict(manifest["titles"].get(name) or {})
            entry[tier] = _clean_title(title)
            manifest["titles"][name] = entry
            self._save_manifest(manifest)
            return self.title_of(name, manifest)  # manifest passed: no re-lock

    def _store_generated_title(
        self, name: str, title: str, at_seq: int, turns: int
    ) -> bool:
        """Write a generated name and the transcript cursor it was read
        from — the seq the transcript stood at, and the user turns it
        held — which is what the cadence measures the next generation
        against. Returns whether it wrote.

        Naming runs OVERLAP. A turn releases the turn lock before the
        session is named, so a second turn's run can start while the
        first's model call is still out, and the two can finish in
        either order. The cursor is what tells them apart: a stored one
        ahead of this call's belongs to a name read from a later
        transcript, and writing an older name over it would step the
        rail back to something the session has already moved past. The
        read and the write are one critical section, so two runs cannot
        both decide they are the newer.
        """
        with self._lock:
            manifest = self._manifest()
            entry = dict(manifest["titles"].get(name) or {})
            if int(entry.get("at_seq") or 0) > at_seq:
                return False
            entry["agent"] = _clean_title(title)
            entry["at_seq"], entry["turns"] = at_seq, turns
            manifest["titles"][name] = entry
            self._save_manifest(manifest)
            return True

    def _restore_generated_title(
        self, name: str, title: str | None, cursor: tuple[int, int] | None
    ) -> None:
        """Put the generated name back as a rewind found it, cursor and
        all: the cadence counts from where that name was READ, so a name
        restored without its cursor would leave the session waiting out
        an interval measured against a transcript that no longer exists.

        A ``None`` cursor CLEARS it, which reads as "named, nobody wrote
        down from where" — and that is the honest answer for a title
        event recorded before the cursor rode along. The next real
        exchange names the session again rather than guessing.
        """
        with self._lock:
            manifest = self._manifest()
            entry = dict(manifest["titles"].get(name) or {})
            entry["agent"] = _clean_title(title)
            if cursor is None:
                entry.pop("at_seq", None)
                entry.pop("turns", None)
            else:
                entry["at_seq"], entry["turns"] = cursor
            manifest["titles"][name] = entry
            self._save_manifest(manifest)

    def retitle(self, session: Session) -> dict | None:
        """Name the session from its own transcript, when the cadence
        says a name is due. Returns what was stored — the generated name
        under ``agent``, with the transcript cursor it was read from —
        or ``None`` when nothing was stored. What was stored is not
        necessarily what SHOWS: a human title outranks it, and
        :meth:`title_of` is what answers that.

        The studio's answer, not the agent's: a second, tiny model run
        over the transcript this registry already holds, made after the
        turn it reads and owing that turn nothing. A failure is logged
        and costs the previous title nothing.

        Blocking — a model call and a manifest write — so callers run
        it off the event loop, and never while holding the turn lock.
        """
        from . import summaries

        spec = self._summary_spec(session)
        manifest = self._manifest()
        turns = self._turn_count(session)
        if spec is None or not self._title_due(session, manifest, turns):
            return None
        # Read BEFORE the run and stamped after it: the cursor says
        # which transcript the stored name was read from, so a turn
        # that lands while the model is answering is one this title
        # does not claim to have seen.
        at = session.next_seq
        transcript = summaries.transcript_text(session)
        try:
            title = summaries.generate_title(spec, transcript)
        except Exception as e:  # noqa: BLE001 - a name is never worth a turn
            log.info("titles: %s went unnamed (%s)", session.name, e)
            return None
        if title is None:
            return None
        if not self._store_generated_title(session.name, title, at, turns):
            # A naming run that started later has already landed: this
            # answer describes a transcript that has been superseded,
            # so it is dropped rather than stored or announced.
            log.info(
                "titles: %s was named again while this ran; dropping %r",
                session.name,
                title,
            )
            return None
        return {"agent": title, "at_seq": at, "turns": turns}

    def _summary_spec(self, session: Session) -> str | None:
        """The model a generated title or description is read by:
        ``NONTAINER_STUDIO_SUMMARY_MODEL``, else this session's own
        model, else the registry's default.

        ``None`` means there is no model configured at all, and then
        nothing is generated — a registry built without a default model
        is one whose embedder never chose a provider, and a summary is
        not the thing to go looking for one on its behalf. The server
        resolves the default at startup and fails fast without one, so
        this is None only where the studio itself would have nothing to
        run a turn on either.
        """
        from . import summaries

        return summaries.summary_spec(session.model or self._default_model)

    def _title_due(self, session: Session, manifest: dict, turns: int) -> bool:
        """Whether to generate a title for ``session`` right now.

        The first one lands after the first turn that was a real
        exchange — a message and an answer with words in it. After
        that, every ``TITLE_TURNS`` user turns: a session whose subject
        moved gets the name it has now, and one that did not costs a
        small run once in five turns.

        A human title changes nothing here. Theirs is the name that
        shows, and the generated one is kept current underneath it, so
        clearing theirs reveals a name for the session as it now stands
        rather than the one it was given before they renamed it.

        A delegate is never named: it is labelled by the handle its
        parent gave it, and the rail lists no row for a name to go in.
        """
        if self.is_delegate(session.name, manifest):
            return False
        entry = manifest["titles"].get(session.name) or {}
        if not entry.get("agent") or entry.get("turns") is None:
            # Never named, or named at a point in the transcript nobody
            # wrote down — a rewind past the cursor, or a manifest from
            # before names carried one. Read the name again at the next
            # real exchange rather than date it by guesswork.
            return self._real_exchange(session)
        return turns - int(entry["turns"]) >= TITLE_TURNS

    @classmethod
    def _turn_count(cls, session: Session) -> int:
        """How many messages the human has sent, as the transcript
        stands — through the projection, so an edit's rewind lowers it
        the way it lowers everything else."""
        return sum(
            1 for _, e in cls._visible(session.snapshot()) if e.get("type") == "user"
        )

    @classmethod
    def _real_exchange(cls, session: Session) -> bool:
        """Whether the transcript holds a message and an answer to it.

        A turn that errored, was stopped, or spent itself on tool calls
        has produced nothing to name the session after — and a name
        read off one of those is the name the session keeps for the
        next five turns.
        """
        asked = False
        for _, event in cls._visible(session.snapshot()):
            kind = event.get("type")
            if kind == "user":
                asked = True
            elif asked and kind == "text" and (event.get("delta") or "").strip():
                return True
        return False
