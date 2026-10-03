"""The registry's own records: the manifest of sessions, apps, models,
titles and delegates, and the per-session app db files it names.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from .session import Db

log = logging.getLogger(__name__)


class ManifestMixin:
    """The manifest and the app db handles, as part of ``Registry``.

    A mixin over the registry's state (``_lock``, ``_store``, ``_dbs``,
    ``_sessions``, ``_published``), which ``Registry.__init__`` sets up.
    """

    def _manifest_path(self) -> Path:
        return self._store.path / "sessions.json"

    def _manifest(self) -> dict:
        """{"sessions": {name: {db}}, "apps": {token: app}, "published":
        {token: {branch, session, checkpoint}}, "models": {name: spec},
        "titles": {name: {user, agent, at_seq, turns}},
        "created": {name: epoch},
        "delegates": {child: {parent, touched, kept}}} — tolerant of the
        older formats that wrote ``sessions`` as a bare list and
        ``delegates`` as ``{child: parent}``, and of any key simply
        being absent.

        A session ROW names the db file that session opens, and an app
        entry names the one it serves over. Nothing else decides a db
        path: see :meth:`_mint_db_path`.

        ``delegates`` is who forked whom (see :meth:`open_delegate`),
        plus what retention needs to be honest across a restart: when
        anybody last dealt with that delegate, and whether somebody
        asked to keep it (see :meth:`sweep_delegates`). It is a RECORD
        and not a naming rule: a session is somebody's delegate because
        the studio wrote it down when it opened one, never because of
        what its name looks like.

        A title entry carries both tiers plus, where one was generated,
        the transcript cursor it was read from — ``at_seq`` and the
        ``turns`` the transcript held then, which is what the cadence
        measures the next generation against (see :meth:`retitle`).

        ``apps`` maps a capability token to the app's publication name,
        db, description and versions (see :meth:`publish`);
        ``published`` is the anchor-branch shape that preceded it and
        is empty after :meth:`_migrate_published` has run once."""
        try:
            data = json.loads(self._manifest_path().read_text())
        except Exception:
            data = {}
        return self._as_manifest(data)

    def _manifest_strict(self) -> dict | None:
        """:meth:`_manifest`, but None when the file is there and could
        not be read or understood.

        ``_manifest`` turns any fault into an empty manifest, which is
        the right answer for every reader that would otherwise fail a
        request over a transient one. It is the wrong answer for a
        caller that DELETES what the manifest does not mention, and
        :meth:`sweep_dbs` is the only such caller.

        A file that is simply absent is not a fault: that is a store
        nobody has used yet, and it reads as the empty manifest it is.
        """
        try:
            data = json.loads(self._manifest_path().read_text())
        except FileNotFoundError:
            data = {}
        except Exception:
            return None
        if not isinstance(data, (dict, list)):
            return None
        return self._as_manifest(data)

    @staticmethod
    def _as_manifest(data: Any) -> dict:
        """Whatever the file held, in the manifest's shape."""
        if isinstance(data, list):  # v1: just session names
            data = {"sessions": data}
        if not isinstance(data, dict):
            data = {}
        sessions = data.get("sessions") or {}
        if isinstance(sessions, list):  # a list of names, no db written down
            sessions = {name: {} for name in sessions}
        return {
            "sessions": sessions,
            "apps": data.get("apps", {}),
            "published": data.get("published", {}),
            "models": data.get("models", {}),
            "titles": data.get("titles", {}),
            "created": data.get("created", {}),
            "delegates": ManifestMixin._as_delegates(
                data.get("delegates", {}), data.get("created", {})
            ),
        }

    @staticmethod
    def _as_delegates(record: Any, created: Any) -> dict[str, dict]:
        """The delegates record in its shape: ``{child: {"parent",
        "touched", "kept"}}``, whatever the file held, plus ``asked``
        (when it was asked for) and ``undone`` (an edit unsaid it) where
        a record carries them.

        It began as ``{child: parent}``, which says who forked whom and
        nothing about retention. Read one of those and the delegate
        ages from its session's BIRTHDAY: that is the only evidence on
        disk of when it was dealt with, and normalizing to "just now"
        instead would move the goalposts every sweep measures against,
        every time the file is read. A child with no birthday either
        reads as untouched, which is the honest answer for a record
        that says nothing — and the one that lets the sweep collect it.

        Normalized on the way IN, so every reader sees one shape and
        the old one is written back the first time anything saves.
        """
        if not isinstance(record, dict):
            return {}
        births = created if isinstance(created, dict) else {}
        out: dict[str, dict] = {}
        for child, entry in record.items():
            if isinstance(entry, str):  # v1: the parent, and nothing else
                entry = {"parent": entry}
            if not isinstance(entry, dict) or not isinstance(entry.get("parent"), str):
                continue
            try:
                touched = float(entry.get("touched", births.get(child, 0.0)) or 0.0)
            except (TypeError, ValueError):
                touched = 0.0
            kept = entry.get("kept")
            out[child] = {
                "parent": entry["parent"],
                "touched": touched,
                # Three answers, not two: `None` is nobody has said, and
                # it is what lets the live job's flag speak for a
                # delegate the studio was never told about (see
                # `_freshest`). An older record that carries no `kept`
                # at all is one of those.
                "kept": kept if kept is None else bool(kept),
            }
            asked = entry.get("asked")
            if isinstance(asked, (int, float)) and not isinstance(asked, bool):
                out[child]["asked"] = float(asked)
            if entry.get("undone") is True:
                out[child]["undone"] = True
        return out

    def _load_manifest(self) -> set[str]:
        return set(self._manifest()["sessions"])

    def _save_manifest(self, manifest: dict) -> None:
        """Atomic (tmp + rename): a concurrent reader mid-write would
        parse partial JSON, see an empty manifest, and 404 a session
        that exists — a transient, maddening-to-reproduce failure."""
        path = self._manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=1))
        tmp.replace(path)

    def _record(
        self, name: str, model: str | None = None, db: str | None = None
    ) -> None:
        """Add to the durable session manifest (caller holds _lock).

        ``db`` is the store-relative path of the file this session's
        ``db`` host object opens. Every row carries one — its own, or
        the one it took from the session it was forked from — because
        a path is never derived from a name.
        """
        manifest = self._manifest()
        row = dict(manifest["sessions"].get(name) or {})
        if db is not None:
            row["db"] = db
        manifest["sessions"] = dict(sorted({**manifest["sessions"], name: row}.items()))
        if model is not None:
            manifest["models"][name] = model
        # birthday, stamped once: slugs carry no order, so this is the
        # only thing that can sort the rail sensibly (see `list`)
        manifest["created"].setdefault(name, time.time())
        self._save_manifest(manifest)

    def _unrecord(self, name: str) -> None:
        """Remove a reservation from the manifest (caller holds _lock)
        — ``_record``'s mirror, for a minted name whose open never
        succeeded. Clears titles/created too, same as ``delete``: a
        later mint drawing this slug must not inherit a ghost's
        birthday."""
        manifest = self._manifest()
        manifest["sessions"].pop(name, None)
        manifest["models"].pop(name, None)
        manifest["titles"].pop(name, None)
        manifest["created"].pop(name, None)
        manifest["delegates"].pop(name, None)
        self._save_manifest(manifest)

    @staticmethod
    def _db_of(name: str, manifest: dict) -> str | None:
        """The db file session ``name`` opens, store-relative, or None
        for a name that is not a session."""
        return (manifest["sessions"].get(name) or {}).get("db")

    def _mint_db_path(self, manifest: dict) -> str:
        """A db file for a session starting empty (caller holds
        ``_lock``): ``dbs/<id>.sqlite`` under an id nothing else holds.

        The id is random rather than the session's name, so that
        deleting a session frees its slug without freeing the store a
        fork or an app may still be serving over.
        """
        while True:
            rel = f"dbs/{secrets.token_hex(8)}.sqlite"
            if (self._store.path / rel).exists():
                continue
            if any(row.get("db") == rel for row in manifest["sessions"].values()):
                continue
            if any(e.get("db") == rel for e in manifest["apps"].values()):
                continue
            return rel

    def _db_handle(self, rel: str) -> Db:
        """The one open handle to the db file at ``rel`` (caller holds
        ``_lock``).

        ONE handle, not one connection each. ``Db`` serializes its own
        calls under a lock, so sessions sharing an object queue behind
        each other and every write lands; two connections to one file
        would queue in SQLite instead, where the default rollback
        journal takes an exclusive lock for a write and hands the loser
        a ``database is locked`` after the busy timeout. A turn, or a
        request to a published app, is not a place to discover that.
        """
        db = self._dbs.get(rel)
        if db is None:
            db = Db(self._store.path / rel)
            self._dbs[rel] = db
        return db

    def _db_held(self, rel: str) -> bool:
        """Whether a live session or a cached publication snapshot is
        running over the handle to ``rel``. Caller holds ``_lock``."""
        manifest = self._manifest()
        if any(self._db_of(n, manifest) == rel for n in self._sessions):
            return True
        apps = manifest["apps"]
        return any((apps.get(t) or {}).get("db") == rel for t in self._published)

    def _forget_db(self, rel: str) -> None:
        """Close and drop the handle to ``rel`` when nothing live is
        running over it (caller holds ``_lock``). The FILE stays.

        A delegate's db is its parent's file, so closing on the way out
        of every session would be a delegate finishing its work by
        breaking the connection the session that asked is mid-
        conversation with.
        """
        if self._db_held(rel):
            return
        db = self._dbs.pop(rel, None)
        if db is not None:
            db.close()

    def sweep_dbs(self) -> list[str]:
        """Delete every db file no session row and no app entry names,
        and return what went.

        Nothing else in the studio deletes a db. A file may be a fork's
        store, a delegate's, or the one a published app is serving
        over, so a session's deletion drops its row and stops there —
        and this reads the whole manifest once instead of four verbs
        each counting referrers correctly. Runs when the registry
        opens, and can be called outright.

        A file this registry still holds a handle to is logged and left
        for the next sweep. Unlinking under an open connection fails
        nothing: the writes simply go nowhere.

        This is the one caller that reads the manifest STRICTLY.
        Everywhere else an unreadable or half-written file is answered
        with an empty one, because a 404 on one session beats a 500 on
        the whole studio; here that answer would mean "no row names
        anything" and take every store on the install. So a read that
        did not come off refuses, and so does a manifest that parses
        but names nothing while db files exist — a store that emptied
        itself and a truncated write look identical from here, and only
        one of them is worth acting on.
        """
        with self._lock:
            root = self._store.path
            files = sorted(root.glob("dbs/*.sqlite")) + sorted(
                root.glob("dbs/apps/*.sqlite")
            )
            manifest = self._manifest_strict()
            if manifest is None:
                log.warning(
                    "dbs: sweep skipped — %s could not be read; leaving %d file(s)",
                    self._manifest_path().name,
                    len(files),
                )
                return []
            named = {row.get("db") for row in manifest["sessions"].values()}
            named |= {entry.get("db") for entry in manifest["apps"].values()}
            if files and not named:
                log.warning(
                    "dbs: sweep skipped — the manifest names no db while %d "
                    "file(s) exist; leaving %s",
                    len(files),
                    ", ".join(f.relative_to(root).as_posix() for f in files),
                )
                return []
            swept = []
            for path in files:
                rel = path.relative_to(root).as_posix()
                if rel in named:
                    continue
                if rel in self._dbs:
                    log.info("dbs: %s is named by nothing but open; leaving it", rel)
                    continue
                path.unlink(missing_ok=True)
                swept.append(rel)
            if swept:
                log.info("dbs: swept %s", ", ".join(swept))
            return swept

    def _migrate_db_rows(self) -> None:
        """Write down where each session's db already is, once, at
        startup.

        A row from before db ids has no ``db`` field, and its file is at
        ``dbs/<name>.sqlite`` because the name WAS the path. Filling
        that in is the whole migration: no file moves and no copy is
        merged, and afterwards there is one shape — every row names its
        file, and a name never decides a path again.
        """
        with self._lock:
            manifest = self._manifest()
            filled = 0
            for name, row in manifest["sessions"].items():
                if not row.get("db"):
                    row["db"] = f"dbs/{name}.sqlite"
                    filled += 1
            if filled:
                self._save_manifest(manifest)
                log.info("dbs: wrote down the file for %d session row(s)", filled)

    @staticmethod
    def _names_apps_keep(manifest: dict) -> set[str]:
        """The session names published apps still name as their origin.

        A deleted session's app row keeps its name, and the name is not
        free while it does: a new session under it would extend that
        app. Read off the manifest's app rows rather than kept as a
        list of its own, so unpublishing releases a name by removing
        the one row that held it."""
        return {e["session"] for e in manifest["apps"].values() if e.get("session")}
