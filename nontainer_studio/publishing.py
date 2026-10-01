"""Publishing: an app as a lineage of versions under a stable link, what
changed since one, serving a version, and the migrations from the
shapes publishing had before.
"""

from __future__ import annotations

import contextlib
import logging
import posixpath
import time
from collections.abc import Iterable
from typing import Any

from nontainer import (
    Ref,
    Workspace,
)
from nontainer.apps import mint_token
from nontainer.apps.dispatch import PUBLISH_EXCLUDE
from nontainer.errors import WorkspaceError

from .config import _ws_kwargs
from .session import (
    Db,
    Session,
    _visible,
)
from .summaries import _clean_description
from .titles import DEFAULT_TITLE

log = logging.getLogger(__name__)


def _pub_name(entry: dict, token: str) -> str:
    """The nontainer publication name for one studio app.

    The token IS the name: ``mint_token`` draws from
    ``secrets.token_urlsafe``, whose alphabet is a subset of the
    session-id shape a publication name must match, so nothing has to
    be derived or escaped. The manifest records it anyway, because the
    mapping is the studio's half of the contract — nontainer's registry
    is generic and carries no token, no route and no db, and an app
    entry is where all three live.
    """
    return entry.get("pub") or token


def _origin_tag(pub: str, version: str) -> str:
    """The store tag for one version's ORIGIN commit.

    A version's own name is ``<pub>/<version>``, which nontainer's
    publish mints and its unpublish removes; the origin hangs under it
    so the two read as one family and neither can be mistaken for the
    other. Store tags are refs, so the name carries neither ``@`` nor
    ``:`` — a publication name and a version name carry neither
    either.
    """
    return f"{pub}/{version}/origin"


def _head_tree(ws: Workspace) -> str | None:
    """The content hash of what this workspace holds right now.

    ``ws.head`` identifies a point in history; the tree identifies what
    the files and cache ARE, so equal trees mean identical content.
    None on a workspace with no history at all."""
    head = next(iter(ws.log(limit=1)), None)
    return head.tree if head is not None else None


CHANGE_BODY_MAX = 64 * 1024
"""Bytes per side the two-sides read will carry as text.

A diff is read by a human in a pane, and past this much of it nobody
does; what a bigger file costs is the whole blob over the wire and a
renderer walking it line by line. Over the cap — or not text at all —
both sides come back empty with their sizes, and the caller shows the
file rather than the edit.
"""


def _fs_size(fs: Any, path: str) -> int | None:
    """A file's byte size off the metadata row the filesystem keeps
    beside its blob, or None where nothing is there to measure.

    ``stat`` rather than ``getsize``: the filesystem answers ``getsize``
    by reading the whole blob, while ``stat`` reads the row written
    with it. A listing is recomputed on every version tick, and a
    changed asset of some gigabytes must cost it a row lookup and not
    the asset. None rather than an error because the live tree moves
    under a listing — a path found at the committed head can be gone
    from the staged one by the time it is measured, and a missing size
    is not worth failing the row.
    """
    try:
        return fs.stat(path).size
    except Exception:
        return None


def _side(fs: Any, path: str) -> tuple[bool, int | None, bytes | None]:
    """One side of a diff: whether the file is there, its size, and its
    bytes — the bytes only when the size is known and within
    :data:`CHANGE_BODY_MAX`.

    The cap is checked on metadata before any read. A changed asset of
    some gigabytes would otherwise be read whole, on both sides, to
    produce a response that omits both bodies; the answer for it is
    its size, and the size is free. A file whose size cannot be read is
    treated as too big to read blind.
    """
    if not fs.isfile(path):
        return False, None, None
    size = _fs_size(fs, path)
    if size is None or size > CHANGE_BODY_MAX:
        return True, size, None
    return True, size, fs.read(path)


def _as_text(data: bytes | None) -> str | None:
    """One side of a diff as text, or None where it must not be shown
    as one: over :data:`CHANGE_BODY_MAX`, holding a NUL, or not valid
    UTF-8. Strict decoding on purpose — a lossy one would render a
    parquet file as mojibake a human might try to read.
    """
    if data is None or len(data) > CHANGE_BODY_MAX or b"\x00" in data:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return None


class PublishingMixin:
    """Apps and their versions, as part of ``Registry``.

    A mixin over the registry's state (``_lock``, ``_store``,
    ``_sessions``, ``_published``, ``_retired``, ``_holds``) and its
    manifest and session methods, which ``Registry`` brings.
    """

    def _migrate_published(self) -> None:
        """Bring pre-app publications forward, once, at startup.

        A publish used to fork an anchor branch and serve it over the
        session's live db, so the token named a BRANCH. An app's
        versions are nontainer publications now — so each old entry
        becomes an app holding one version, ``v1``, published from the
        anchor branch's head, over the very db it was already serving
        over: the app's row names the origin session's file, which is
        what the anchor shape did and what keeps that file from being
        swept once the session goes.

        An entry whose anchor branch is gone is dropped. A token that
        names no state serves nothing, and leaving it in the manifest
        only means a 404 that looks like a bug.
        """
        with self._lock:
            manifest = self._manifest()
            legacy = manifest.get("published") or {}
            if not legacy:
                return
            for token, old in legacy.items():
                entry = self._migrate_one(token, old, manifest)
                if entry is None:
                    log.warning(
                        "publish: dropped %s — its snapshot branch %r is gone",
                        token,
                        old.get("branch"),
                    )
                else:
                    manifest["apps"][token] = entry
                    log.info(
                        "publish: migrated %s to an app at v1 (was branch %r)",
                        token,
                        old.get("branch"),
                    )
            manifest["published"] = {}
            self._save_manifest(manifest)

    def _migrate_one(self, token: str, old: dict, manifest: dict) -> dict | None:
        """One anchor branch -> one app with a ``v1``, or None if the
        branch is gone. Caller holds ``_lock``."""
        branch = old.get("branch")
        checkpoint = old.get("checkpoint")
        origin = old.get("session")
        if not branch:
            return None
        title = self.title_of(origin, manifest) if origin else DEFAULT_TITLE
        commit = tree = ref = None
        ws = self._store.open(branch)
        try:
            # Opening a branch CREATES it, so "is it still there" cannot
            # be asked by opening. Ask by content instead: an anchor
            # never moved after the fork that made it, so its head is
            # the commit the manifest recorded under `checkpoint` —
            # while a branch this very call invented has a baseline head
            # of its own.
            if checkpoint and ws.head == checkpoint:
                published = self._store.publish(
                    ws,
                    token,
                    version="v1",
                    info={"token": token, "session": origin, "title": title},
                )
                ref = str(published.version("v1").ref)
                commit = ws.head
                tree = _head_tree(ws)
        except (ValueError, WorkspaceError) as e:
            # An anchor holding no `app/` tree is an entry whose URL
            # could only ever have 404ed; it is dropped like a missing
            # branch rather than recorded as an app that serves nothing.
            # nontainer calls that one the caller's mistake (a
            # `ValueError`) and a store it cannot publish from its own
            # (a `WorkspaceError`), and neither is worth failing a
            # startup migration over: the entry is legacy either way.
            log.warning("publish: %s cannot be published from %r: %s", token, branch, e)
        finally:
            ws.close()
        # Either way the anchor branch goes: it was the old serving
        # mechanism, and the publication outlives it.
        self._delete_branches({branch})
        if ref is None:
            return None
        # The anchor shape served over the origin session's live db, so
        # the migrated app names that same file. Nothing is copied: a
        # migration is not the moment to invent a second store. An
        # entry whose session is gone gets a db of its own, empty.
        db = self._db_of(origin, manifest) or self._mint_db_path(manifest)
        return {
            "token": token,
            "pub": token,
            "session": origin,
            "title": title,
            "created": time.time(),
            "db": db,
            "current": "v1",
            "versions": {
                "v1": {
                    "ref": ref,
                    "commit": commit,
                    "tree": tree,
                    "created": time.time(),
                }
            },
        }

    def _migrate_publications(self) -> None:
        """Re-derive tagged versions as publications, once, at startup.

        A version used to be a store-scoped tag over the origin
        session's branch, so a capability URL froze the whole session
        tree behind it — the notes, the uploads, the skills, the
        conversation record. A publication holds the files under
        ``app/`` and the rows that describe them, on a branch of its
        own, so re-derive each recorded version from the commit its tag
        names, keep the names, the order and the pointer, and drop the
        tag.

        Versions never move, so this runs once per install: an app
        whose every version row already names a publication ref is
        already migrated and is not looked at again. But "once" is the
        happy path, not a guarantee — the process can stop between the
        publish and the manifest write — so this is written to be
        RESUMABLE. Each app's row is saved before its legacy tags are
        dropped, so a crash leaves the tags that can re-derive it; and
        a version already published under its own name is adopted
        rather than published again, since a retry would otherwise
        collide with the work the interrupted run had finished.
        """
        with self._lock:
            manifest = self._manifest()
            stale = {
                token: entry
                for token, entry in manifest["apps"].items()
                if any(
                    not row.get("ref") for row in (entry.get("versions") or {}).values()
                )
            }
            if not stale:
                return
            for token, entry in stale.items():
                migrated = self._migrate_app(token, entry)
                if migrated is None:
                    manifest["apps"].pop(token, None)
                    log.warning(
                        "publish: dropped %s — none of its tagged versions "
                        "could be published",
                        token,
                    )
                else:
                    manifest["apps"][token] = migrated
                    log.info(
                        "publish: migrated %s to publication %r at %s (current %s)",
                        token,
                        migrated["pub"],
                        ", ".join(migrated["versions"]),
                        migrated["current"],
                    )
                # Save, THEN drop the tags — per app, not once at the
                # end. A tag is the only thing a version that never
                # reached the manifest can be re-derived from, so it
                # outlives the row that replaces it; a crash the other
                # way round leaves a version with neither a row nor a
                # tag, and an app that cannot be recovered by anything.
                self._save_manifest(manifest)
                self._delete_tags(
                    row["tag"]
                    for row in (entry.get("versions") or {}).values()
                    if row.get("tag")
                )

    def _migrate_app(self, token: str, entry: dict) -> dict | None:
        """One tag-shaped app entry -> the same app over a publication,
        or None if not one of its versions could be re-derived. Caller
        holds ``_lock``."""
        pub = _pub_name(entry, token)
        rows = entry.get("versions") or {}
        # Publish order, so the versions land in the order they were
        # made — nontainer keeps the lineage, and a name published out
        # of order would still read as if it had come later.
        ordered = sorted(rows.items(), key=lambda kv: kv[1].get("created", 0))
        migrated: dict[str, dict] = {}
        for version, row in ordered:
            if row.get("ref"):
                migrated[version] = row
                continue
            ref = self._republish(pub, version, entry, row)
            if ref is None:
                continue
            migrated[version] = {k: v for k, v in row.items() if k != "tag"} | {
                "ref": ref
            }
        if not migrated:
            return None
        current = entry.get("current")
        if current not in migrated:
            # The version the URL pointed at could not be re-derived.
            # The newest one that could is the closest thing to what
            # was being served, and a publication must point somewhere.
            current = max(migrated, key=lambda v: migrated[v].get("created", 0))
        published = self._store.publication(pub)
        if published is not None and published.current != current:
            # Skipped when it already agrees, so a resumed migration
            # that got this far last time asks for nothing.
            self._store.set_current(pub, current)
        return dict(entry, pub=pub, current=current, versions=migrated)

    def _republish(self, pub: str, version: str, entry: dict, row: dict) -> str | None:
        """Publish one tagged version again as a version of ``pub``;
        returns its ref, or None if the tagged state holds no app.

        The source is a FROZEN workspace over the tagged commit, which
        is what publishing derives from. It is opened through the
        origin SESSION where that branch is still there, so the version
        records the session it actually came from; through the tag
        itself where the session is gone, and then ``published_from``
        names whichever branch the store-scoped read borrowed instead.
        The studio's own ``session`` key carries the origin either way,
        which is what the rail and ``branch_from_version`` read.

        A version already published under this name is ADOPTED rather
        than published again. It is the same content by construction —
        the same session commit through the same paths — and it can
        only be there because an earlier run of this migration
        published it and stopped before the manifest write. Publishing
        again would be refused (versions are immutable), and treating
        that refusal as a failure is what would drop the app.

        No execution settings are passed: nothing runs here, only
        reads, so this takes nontainer's default executor rather than
        booting a selected backend once per version.
        """
        already = self._store.publication(pub)
        landed = already.version(version) if already is not None else None
        if landed is not None:
            log.info(
                "publish: adopted %s/%s from an interrupted migration",
                pub,
                version,
            )
            return str(landed.ref)
        origin = entry.get("session")
        commit = row.get("commit")
        tag = row.get("tag")
        source = None
        if origin and commit and origin in self.known():
            try:
                source = self._store.resolve(f"{origin}@{commit}")
            except Exception:
                source = None
        if source is None and tag:
            try:
                source = self._store.tags.at(tag)
            except Exception:
                source = None
        if source is None:
            log.warning(
                "publish: dropped %s/%s — neither its session nor its tag still opens",
                entry.get("token"),
                version,
            )
            return None
        try:
            published = self._store.publish(
                source,
                pub,
                version=version,
                info={
                    "token": entry.get("token"),
                    "session": origin,
                    "title": entry.get("title") or DEFAULT_TITLE,
                },
            )
        except Exception as e:
            log.warning(
                "publish: dropped %s/%s — it cannot be published: %s",
                entry.get("token"),
                version,
                e,
            )
            return None
        finally:
            source.close()
        return str(published.version(version).ref)

    def _app_db(self, entry: dict) -> Db:
        """The handle to the db this app's row names.

        One handle for the file, so every version of the app, the
        session that published it and any fork of that session are all
        talking to one store through one lock."""
        return self._db_handle(entry["db"])

    @staticmethod
    def _version_name(asked: str | None, entry: dict | None) -> str:
        """The name this version gets: the caller's, or the next free
        ``vN`` in the app.

        The default counts the app's VERSIONS rather than its
        ``v``-numbers, so a named version pushes the next number along
        instead of being overwritten by it. nontainer's own default
        counts only ``v``-numbers, so studio names every version
        explicitly rather than letting the store pick one.

        A name the caller asked for is passed on as typed (bar the
        whitespace a text field collects). nontainer refuses one a tag
        cannot carry and one this lineage already holds, both with a
        ``ValueError`` the route answers 400 to, and its grammar is the
        stricter of the two — so a second spelling of the rule here
        could only ever disagree with the one that decides."""
        taken = set((entry or {}).get("versions", {}))
        if asked is None:
            n = len(taken) + 1
            while f"v{n}" in taken:
                n += 1
            return f"v{n}"
        return asked.strip()

    @staticmethod
    def _last_published(entry: dict) -> float:
        """When this app last got a version — how apps are ordered,
        since an app's own birthday says only when the lineage
        started."""
        versions = entry.get("versions") or {}
        return max(
            (v.get("created", 0) for v in versions.values()),
            default=entry.get("created", 0),
        )

    def _session_app(self, name: str, apps: dict) -> tuple[str, dict]:
        """This session's app: the token it publishes under and that
        app's entry, or a fresh token and ``{}`` where it has none yet.

        An app belongs to the session named in its row and to no other,
        so a fork — a different name from the first commit on — starts
        an app of its own at its first publish, and never lands a
        version in the app its parent keeps extending. Where an install
        holds several apps for one session the newest by publish is the
        one extended; nothing new creates that case."""
        mine = [e for e in apps.values() if e.get("session") == name]
        if mine:
            newest = max(mine, key=self._last_published)
            return newest["token"], newest
        return mint_token(), {}

    def publish(self, name: str, *, version: str | None = None) -> dict:
        """Publish the session's current state as a new version of an app.

        The version is a nontainer publication version — the `app/`
        tree on a branch of its own — so it is durable and
        session-independent from the moment it exists; the app's URL
        then points at it (``current``), which is what makes publishing
        a new version the everyday verb and rolling back a pointer move
        (:meth:`set_current`).

        The session has one app, so this extends it, or starts it on
        the first publish; a second app is a fork's. ``version`` names
        the version; the default is ``v1``, ``v2``, ... within the app.

        A session holding nothing under ``<root>/app`` is refused: a
        version is that tree, and an empty one is a URL that could only
        ever 404.

        A between-turns verb, like fork: a turn in flight owns the
        workspace, and a version published over half a turn would hold
        a state no commit ever held.
        """
        session = self._sessions.get(name)
        if session is None:
            raise KeyError(name)
        if not session.turn_lock.acquire(blocking=False):
            raise RuntimeError("can't publish while a turn is running")
        try:
            return self._publish_locked(session, version=version)
        finally:
            session.turn_lock.release()

    def _publish_locked(self, session: Session, *, version: str | None = None) -> dict:
        """:meth:`publish` with the session already reserved.

        The reservation has to outlive the call for the caller that
        EMITS the marker: a chat request winning the turn lock between
        the version and the marker would put its `user` event above a
        landmark whose commit predates it, and restoring to that
        marker would then rewind the files under a prompt still on
        screen. So the route holds one reservation across both."""
        name = session.name
        # Before the lock, because it is a model run: what an app is
        # FOR is read off the conversation that built it, and holding
        # the registry lock across that would stall every session open
        # behind a provider call.
        description = self._describe(session)
        with self._lock:
            manifest = self._manifest()
            token, entry = self._session_app(name, manifest["apps"])
            version = self._version_name(version, entry)
            title = self.title_of(name, manifest)
            pub = _pub_name(entry, token)
            # Publishing names a commit and refuses staged work, so the
            # session's is landed here first — what the version holds
            # is then what the human was looking at. Safe under the
            # caller's turn-lock reservation and nowhere else: that is
            # what says no half-turn is sitting in the buffer.
            if session.ws.uncommitted:
                session.ws.commit(info={"tool": "publish", "app": token})
            commit = session.ws.head
            # nontainer serializes its publication registry under a
            # per-path threading.Lock, plus an `flock` on
            # `publications.lock` where fcntl exists so a second PROCESS
            # cannot pick the same version number. The studio is one
            # process and holds `_lock` across this whole
            # read-decide-write, so there is nothing to add here.
            #
            # The version lands without the pointer, which then moves
            # only once the manifest row naming it is on disk: every
            # state in between still serves what the URL served before.
            # nontainer points a lineage's FIRST version at itself
            # whatever this says, because a publication must point
            # somewhere. A caller's mistake here — a name a tag cannot
            # carry, one this lineage already holds, a session with
            # nothing under `app/` — is a ValueError and leaves as one,
            # so the route answers 400; a WorkspaceError is the store's
            # own state and keeps its 500.
            published = self._store.publish(
                session.ws,
                pub,
                version=version,
                current=False,
                # Studio's own provenance. `tool`, `name`, `version`
                # and `published_from` are nontainer's to write into
                # the commit and are refused here rather than
                # overridden, which is why `version` is not among them.
                info={"token": token, "session": name, "title": title},
            )
            try:
                if not entry:
                    entry = {
                        "token": token,
                        "pub": pub,
                        "session": name,
                        "created": time.time(),
                        # The session's file, named and not copied: the
                        # app is a deployment against that store, and
                        # this row is also what keeps the file once the
                        # session is gone.
                        "db": self._db_of(name, manifest),
                        "versions": {},
                    }
                entry = dict(entry, versions=dict(entry["versions"]))
                # the app wears the session's title as of this
                # publish: renaming the session and publishing again
                # should rename the app, not leave it under a name
                # nobody uses any more
                entry["title"] = title
                # Same rule for the description, and one tier down: the
                # human's own words for an app outrank every later
                # publish, and a generation that failed leaves whatever
                # the app already said rather than blanking it.
                if description is not None:
                    entry["description"] = {
                        **(entry.get("description") or {}),
                        "agent": description,
                    }
                row = published.version(version)
                entry["versions"][version] = {
                    # Two commits, because they answer two questions.
                    # `ref` is the publication's own derived commit —
                    # the app, and what the URL serves. `commit` is the
                    # SESSION's, which is what a publish marker
                    # restores to and what `changed_since` measures the
                    # live workspace against.
                    "ref": str(row.ref),
                    "commit": commit,
                    "tree": _head_tree(session.ws),
                    "created": row.created,
                    # The name under which that session commit can be
                    # reached once the session is gone. Absent where
                    # the store would not take the name, and read as
                    # "this version has no origin to start from"
                    # wherever it is read.
                    **self._tag_origin(pub, version, name, commit),
                }
                entry["current"] = version
                manifest["apps"][token] = entry
                self._save_manifest(manifest)
            except BaseException:
                # A version with no manifest entry is a URL nothing can
                # reach and a branch nothing will ever collect, so it
                # goes back down. Nothing points at it — a publication
                # refuses to drop the version it points at while others
                # remain, and this one never took the pointer.
                self._unpublish_version(pub, version)
                # The origin tag goes with it: a GC root for a version
                # nothing names is history pinned forever by a publish
                # that did not happen.
                self._delete_tags([_origin_tag(pub, version)])
                raise
            if published.current != version:
                # The row is on disk, so the store can be pointed at
                # what it names. Skipped for the version that opened
                # the lineage, which took the pointer as it landed.
                self._point_store_at(pub, token, version)
            self._drop_snapshots(token)
            return {
                "token": token,
                "url": f"/apps/{token}/",
                "version": version,
                "title": title,
                "commit": commit,
                "tree": entry["versions"][version]["tree"],
                "origin": entry["versions"][version].get("origin"),
            }

    def resolve(self, token: str) -> Workspace | None:
        """The ``build_router`` resolve hook: the frozen workspace at
        this app's CURRENT version.

        The URL belongs to the app, not to a version, so what it serves
        moves when the pointer moves — hence the cache is keyed by
        token AND version, and repointing simply drops the old entry.

        Reopening after a restart needs nothing from the origin session,
        which may be long gone: a publication version lives on a branch
        of its own and the db is the app's own file. So the studio
        keeps no branch of its own for an app to be served through, and
        an app whose session was deleted opens exactly as one whose
        session is still there does.

        A commit holds the tree and nothing else, so the live objects
        the app's handlers call are supplied at the open rather than
        inherited: `python` carries the app's own `db`, and the
        executor factory carries the selected backend. Without them a
        served handler would answer every request with a NameError on
        `db`.
        """
        served = self._published.get(token)
        if served is not None:
            return served[1]
        with self._lock:
            served = self._published.get(token)
            if served is not None:
                return served[1]
            entry = self._manifest()["apps"].get(token)
            if entry is None:
                return None
            version = entry.get("current")
            info = (entry.get("versions") or {}).get(version)
            if info is None:
                return None
            publication = self._store.publication(_pub_name(entry, token))
            if publication is None:
                return None
            # The version by name, not the registry's own `current`:
            # what the rail shows and what the URL serves have to be
            # one answer, and the manifest is where the studio keeps
            # it. Nothing here reads which version nontainer points at,
            # which is what makes the manifest the authority — an app
            # serves the version its row names even where the store's
            # pointer has been left behind. The open re-reads
            # nontainer's registry either way, so a version unpublished
            # since is refused rather than served.
            snapshot = publication.open(
                version,
                python=self._python_config(self._app_db(entry)),
                **_ws_kwargs(),
            )
            self._published[token] = (version, snapshot)
            return snapshot

    def acquire_snapshot(self, token: str) -> Workspace | None:
        """:meth:`resolve`, counting one more request as holding the
        snapshot until it calls :meth:`release_snapshot`. The count is taken
        under the same lock a drop takes, so no snapshot can be closed
        between being handed out and being counted."""
        with self._lock:
            ws = self.resolve(token)
            if ws is not None:
                self._holds[id(ws)] = self._holds.get(id(ws), 0) + 1
            return ws

    def release_snapshot(self, ws: Workspace) -> None:
        """A request is done with a snapshot it acquired. The last one
        out of a snapshot that was dropped while held closes it, outside
        the lock: closing a dud-backed snapshot waits for its guest."""
        with self._lock:
            key = id(ws)
            left = self._holds.get(key, 0) - 1
            if left > 0:
                self._holds[key] = left
                return
            self._holds.pop(key, None)
            retired = self._retired.pop(key, None)
        if retired is not None:
            retired.close()

    def _drop_snapshots(self, token: str, version: str | None = None) -> None:
        """Forget an app's cached snapshot — unconditionally, or only if
        it is serving ``version``. The next request rebuilds it from the
        manifest. Caller holds ``_lock``.

        Out of routing first, then closed: nontainer's order for evicting
        a served snapshot. A closed snapshot runs no new handlers (on a
        dud rung a request that reaches one raises), and a request can
        look the snapshot up just before this and dispatch just after.
        So a snapshot a request still holds is retired rather than
        closed, and the last request out closes it (:meth:`release_snapshot`).
        That also keeps a dud close, which waits for an in-flight
        handler, from running under this lock."""
        served = self._published.get(token)
        if served is None or (version is not None and served[0] != version):
            return
        ws = self._published.pop(token)[1]
        if self._holds.get(id(ws)):
            self._retired[id(ws)] = ws
        else:
            ws.close()

    def list_apps(self) -> list[dict]:
        """Every app on this store, most recently published first."""
        rows = [self._app_row(e) for e in self._manifest()["apps"].values()]
        rows.sort(key=lambda r: -r["published"])
        return rows

    def _describe(self, session: Session) -> str | None:
        """What this session built, in a sentence or two, for whoever
        meets the app without the conversation behind it — a human
        scanning what they have published, or an agent weighing whether
        to start from its origin tag.

        Best-effort by design: an app that publishes is worth more than
        a description of it, so a model that will not answer costs a
        log line and the app keeps whatever it already said.
        """
        from . import summaries

        spec = self._summary_spec(session)
        transcript = summaries.transcript_text(session)
        # Nothing said, nothing to say about it: a session can publish
        # an app somebody built by hand, or on a transcript the window
        # has scrolled past.
        if spec is None or not transcript.strip():
            return None
        try:
            return summaries.generate_description(spec, transcript)
        except Exception as e:  # noqa: BLE001 - never worth a publish
            log.info("apps: %s went undescribed (%s)", session.name, e)
            return None

    def set_app_description(self, token: str, text: str | None) -> dict:
        """The human's own words for an app; returns its row.
        ``None``/blank CLEARS them, falling back to what the last
        publish generated. ``KeyError`` for a token nothing published.

        The same two tiers a title has, for the same reason: what a
        person wrote about their own app outranks what a model read off
        a transcript, and the generated one is still stored underneath
        so clearing theirs reveals it rather than emptying the row.
        """
        with self._lock:
            manifest = self._manifest()
            entry = manifest["apps"].get(token)
            if entry is None:
                raise KeyError(token)
            entry = dict(
                entry,
                description={
                    **(entry.get("description") or {}),
                    "user": _clean_description(text),
                },
            )
            manifest["apps"][token] = entry
            self._save_manifest(manifest)
            return self._app_row(entry)

    @staticmethod
    def _app_row(entry: dict) -> dict:
        """One app, as the API says it. Versions come back as a LIST in
        publish order — the order they are read in — rather than the
        manifest's name-keyed map."""
        versions = entry.get("versions") or {}
        description = entry.get("description") or {}
        return {
            "token": entry["token"],
            "title": entry.get("title") or DEFAULT_TITLE,
            # RESOLVED, like the title: the human's own words win, the
            # generated ones fill the gap, and "" means the app has no
            # description at all.
            "description": description.get("user") or description.get("agent") or "",
            "session": entry.get("session"),
            "created": entry.get("created", 0),
            "published": PublishingMixin._last_published(entry),
            "current": entry.get("current"),
            "url": f"/apps/{entry['token']}/",
            "versions": [
                {
                    "name": name,
                    "commit": v.get("commit"),
                    "tree": v.get("tree"),
                    "created": v.get("created", 0),
                    # None where the version has no name on its origin
                    # commit, which is the answer "there is nothing to
                    # start from here".
                    "origin": v.get("origin"),
                }
                for name, v in sorted(
                    versions.items(), key=lambda kv: kv[1].get("created", 0)
                )
            ],
        }

    def session_apps(self, session: Session) -> list[dict]:
        """This session's apps, each carrying what its live workspace
        holds that the app's newest version doesn't."""
        rows = [r for r in self.list_apps() if r["session"] == session.name]
        for row in rows:
            row["changed_since"] = self._changed_since(session, row)
        return rows

    @staticmethod
    def _newest_version(row: dict) -> dict | None:
        """The app's most recently published version, or None for an app
        that holds none. An app row lists its versions in publish
        order, so it is the last of them."""
        versions = row.get("versions") or []
        return versions[-1] if versions else None

    def _changed_since(self, session: Session, row: dict) -> dict:
        """The distance between a session's app files and the NEWEST
        version of its app — two answers, because they are two
        questions.

        The newest version rather than the one the URL serves, because
        this is the unsaved-work question: a session whose URL was
        rolled back to an older version and then left alone has nothing
        unsaved, and the pointer being behind is a separate fact that
        the version list shows on its own.

        ``since`` names that version (None where the app has none yet).
        ``count`` / ``paths`` / ``files`` are the CONTENT question:
        files under ``<root>/app`` whose bytes differ from it. A file
        re-saved with the bytes it already had is not in them, and
        neither is anything under the directories a publish leaves out
        (``app/logs/``, ``app/screenshots/``): those are the authoring
        loop's, so an edit there is never an unpublished change.
        ``files`` carries the same paths in the same order with a
        ``status`` (``added`` / ``modified`` / ``removed``) and a
        ``size`` each, so a reader can render the list without asking
        for any file.

        ``up_to_date`` is the WRITE question: kvgit stamps every write
        with when it happened, so the tree moves on any write at all,
        anywhere in the workspace. False with ``count`` 0 means "the
        session has moved on, but the app is the same app" — which is
        the common case after a turn that only touched notes, and also
        what a session reads as once it has been rewound back onto a
        published state: putting the content back is itself a write.
        """
        newest = self._newest_version(row)
        if newest is None or not newest.get("commit"):
            return {
                "since": None,
                "count": 0,
                "paths": [],
                "files": [],
                "up_to_date": True,
            }
        # The diff ends at the COMMITTED head while the live preview
        # serves the staged tree, so for the length of one tool call a
        # write can be on screen and not yet counted here. The commit
        # that closes every tool call closes the gap too; there is
        # nothing to chase.
        diff = session.ws.changed_since(newest["commit"])
        prefix = f"{session.ws.root}/app/"
        # nontainer's own list, so the count excludes exactly what a
        # publish leaves out.
        unpublished = tuple(f"{session.ws.root}/{p}" for p in PUBLISH_EXCLUDE)
        paths = sorted(
            p
            for p in (diff.added | diff.removed | diff.modified)
            if p.startswith(prefix) and not p.startswith(unpublished)
        )
        return {
            "since": newest["name"],
            "count": len(paths),
            "paths": paths,
            "files": self._change_rows(
                session, row["token"], newest["name"], paths, diff
            ),
            "up_to_date": _head_tree(session.ws) == newest.get("tree"),
        }

    def _change_rows(
        self,
        session: Session,
        token: str,
        version: str,
        paths: list[str],
        diff: Any,
    ) -> list[dict]:
        """``{path, status, size}`` per changed path, in path order.

        ``size`` is measured on the side that HAS the file: the live
        tree, or the published version for a path the session deleted.
        The published tree is opened only when there is a deletion to
        measure, because this list is recomputed on every version tick
        and an app that merely grew must cost no open at all.
        """
        removed = [p for p in paths if p in diff.removed]
        old_sizes = self._version_sizes(token, version, removed)
        rows = []
        with session.ws.lock:
            live = session.ws.files.fs
            for path in paths:
                if path in diff.removed:
                    rows.append(
                        {
                            "path": path,
                            "status": "removed",
                            "size": old_sizes.get(path),
                        }
                    )
                    continue
                rows.append(
                    {
                        "path": path,
                        "status": "added" if path in diff.added else "modified",
                        "size": _fs_size(live, path),
                    }
                )
        return rows

    @contextlib.contextmanager
    def _version_tree(self, token: str, version: str) -> Any:
        """A frozen workspace over one published version of one app,
        closed when the block ends.

        No execution settings are passed: this reads files and runs
        nothing, so it takes nontainer's default executor rather than
        booting the selected backend — on a rung where an executor is a
        machine, a diff must never start one. Nothing is cached either:
        the open is per read, so a ``set_current`` or an ``unpublish``
        landing mid-read closes nothing out from under the app's own
        served snapshot.

        Yields None where the manifest or the store no longer names
        the app at all; a version the store cannot open raises, since
        the manifest promising one the store lost is a fault and not
        an answer.
        """
        entry = self._manifest()["apps"].get(token)
        publication = (
            self._store.publication(_pub_name(entry, token))
            if entry is not None
            else None
        )
        tree = publication.open(version) if publication is not None else None
        try:
            yield tree
        finally:
            if tree is not None:
                tree.close()

    def _version_sizes(
        self, token: str, version: str, paths: list[str]
    ) -> dict[str, int | None]:
        """Byte sizes of some paths as one published version holds them.

        Empty for an empty ask, so a listing with no deletions in it
        pays no open — and empty for a version that will not open,
        because a missing size is a row that says less, where a raise
        here would be a session with no app list at all.
        """
        if not paths:
            return {}
        try:
            with self._version_tree(token, version) as tree:
                if tree is None:
                    return {}
                return {path: _fs_size(tree.files.fs, path) for path in paths}
        except Exception:
            log.warning("changes: %s of app %s would not open", version, token)
            return {}

    def app_file_change(
        self, session: Session, token: str, path: str, since: str | None = None
    ) -> dict:
        """One app file's two sides: as a published version holds it,
        and as the session holds it now.

        ``since`` names the version to compare against and defaults to
        the newest, which is the same baseline the session's app row
        counts from — so the everyday "what have I not published" and a
        deliberate "what changed between two versions" are one read.

        ``status`` is recomputed here from the two sides rather than
        taken from the caller: a row expanded some seconds after the
        list it came from was drawn must say what the file IS, not what
        it was. Bodies are text or nothing at all (see
        :data:`CHANGE_BODY_MAX`), and ``size`` is the side that has the
        file, so a caller with no bodies still has something to show.

        KeyError names what could not be found — an app this session
        did not publish, a path outside its ``app/`` tree, a file
        neither side holds. ValueError is a version the app does not
        hold.
        """
        entry = self._manifest()["apps"].get(token)
        if entry is None or entry.get("session") != session.name:
            raise KeyError(f"{session.name} has published no app {token!r}")
        versions = entry.get("versions") or {}
        if not versions:
            raise KeyError(f"app {token!r} has no versions")
        if since is None:
            since = max(versions, key=lambda name: versions[name].get("created", 0))
        elif since not in versions:
            raise ValueError(f"this app has no version {since!r}")
        # Normalized before the check, so no spelling of `..` reaches
        # past the app tree: what is published is `app/` and what this
        # answers for is `app/`.
        path = posixpath.normpath(path)
        prefix = f"{session.ws.root}/app/"
        if not path.startswith(prefix):
            raise KeyError(f"{path!r} is not an app file")
        with self._version_tree(token, since) as tree:
            if tree is None:
                raise KeyError(f"the store no longer holds app {token!r}")
            had, old_size, old = _side(tree.files.fs, path)
        with session.ws.lock:
            has, new_size, new = _side(session.ws.files.fs, path)
        if not had and not has:
            raise KeyError(f"neither {since} nor the session holds {path!r}")
        if not has:
            status, size = "removed", old_size
        elif not had:
            status, size = "added", new_size
        elif old is not None and new is not None:
            status = "unchanged" if old == new else "modified"
            size = new_size
        else:
            # Past the cap a side is not read, so the bytes are not
            # compared. A row is offered because the content differs,
            # and equal sizes are no evidence that the bytes match, so
            # an unread pair reads as modified rather than unchanged.
            status, size = "modified", new_size
        old_text, new_text = _as_text(old), _as_text(new)
        # Either side unreadable makes BOTH sides empty: half a diff
        # renders as the whole file having been written or deleted,
        # which is a worse answer than none.
        binary = (had and old_text is None) or (has and new_text is None)
        return {
            "path": path,
            "since": since,
            "status": status,
            "old": None if binary else old_text,
            "new": None if binary else new_text,
            "size": size,
            "binary": binary,
        }

    def set_current(self, token: str, version: str) -> dict:
        """Repoint an app's URL at one of its versions — a rollback or a
        roll forward. Versions never move; the pointer does, which is
        the whole reason the URL is the app's and not a version's.

        A CODE move only. Rows the versions' handlers wrote live in the
        app's db, which is outside every version and rolls back with
        none of them."""
        with self._lock:
            manifest = self._manifest()
            entry = manifest["apps"].get(token)
            if entry is None:
                raise KeyError(token)
            if version not in (entry.get("versions") or {}):
                raise ValueError(f"this app has no version {version!r}")
            self._store.set_current(_pub_name(entry, token), version)
            entry["current"] = version
            self._save_manifest(manifest)
            self._drop_snapshots(token)
            return self._app_row(entry)

    def unpublish(self, token: str) -> None:
        """Take an app down: every version, its cached snapshots and
        its manifest entry.

        Not its db. The entry was a NAME for a file the session that
        published it is very likely still writing to, so dropping the
        row drops the app's claim and nothing else; a file no row names
        any more is collected by :meth:`sweep_dbs`.

        The origin TAGS are the studio's to remove: nontainer's
        unpublish takes down only what its own publish wrote, and the
        name on each version's origin commit is not that. Dropping them
        here is what releases the origin session's pinned history, so
        an app taken down stops keeping one.

        The origin session is untouched — an app was never the session's
        state, only a version of its `app/` tree over the same store."""
        with self._lock:
            # nontainer keeps the version it points at while others
            # remain, so a pointer left behind an earlier publish would
            # decide the order below is wrong and leave that version
            # standing.
            self._reconcile_pointers()
            manifest = self._manifest()
            entry = manifest["apps"].pop(token, None)
            if entry is None:
                raise KeyError(token)
            self._drop_snapshots(token)
            self._forget_db(entry["db"])
            pub = _pub_name(entry, token)
            versions = list(entry.get("versions") or {})
            current = entry.get("current")
            # The current version goes LAST: nontainer refuses to
            # remove the one a publication points at while others
            # remain (something is being served off it and there is no
            # obvious successor), and allows it as the last one, where
            # it takes the publication's record with it.
            for name in [v for v in versions if v != current] + [
                v for v in versions if v == current
            ]:
                self._unpublish_version(pub, name)
            self._save_manifest(manifest)
            # The row first, the tags after: a stop in between leaves a
            # name nothing reaches, where the other order would leave a
            # row naming a commit that has been let go.
            self._delete_tags(self._origin_tags(entry))

    def delete_version(self, token: str, version: str) -> dict:
        """Remove one version of an app.

        Not the one the URL serves (it would point at nothing) and not
        the last one — taking an app down is ``unpublish``, and a verb
        that big should be the one the caller named.

        Its origin tag goes with it, which is what releases the origin
        session's history up to that publish: the tag is a GC root, and
        the version it was kept for is gone."""
        with self._lock:
            # The version being deleted is not the one this app serves,
            # but a pointer left behind an earlier publish may still
            # name it — and nontainer keeps the version it points at
            # while others remain, so the delete would drop the row and
            # nothing else.
            self._reconcile_pointers()
            manifest = self._manifest()
            entry = manifest["apps"].get(token)
            if entry is None:
                raise KeyError(token)
            versions = entry.get("versions") or {}
            if version not in versions:
                raise ValueError(f"this app has no version {version!r}")
            if version == entry.get("current"):
                raise ValueError(
                    f"{version!r} is what the app's URL serves — point it at "
                    "another version first"
                )
            if len(versions) == 1:
                raise ValueError(
                    "this is the app's only version — unpublish the app instead"
                )
            self._drop_snapshots(token, version)
            origin = versions.pop(version).get("origin")
            self._unpublish_version(_pub_name(entry, token), version)
            self._save_manifest(manifest)
            # The row first, the tag after: see ``unpublish``.
            self._delete_tags([origin] if origin else [])
            return self._app_row(entry)

    def _tag_origin(self, pub: str, version: str, session: str, commit: str) -> dict:
        """Name a version's origin commit store-scoped: ``{"origin":
        <tag>}``, or an empty dict where the store would not take the
        name.

        A publication holds the derived `app/` tree, which is the app
        and not the session that wrote it. The ORIGIN is the whole
        workspace at that publish — notes, uploads, the conversation —
        so a name on it is what lets the state be mounted, taken from
        or forked into a delegate after the session it came from is
        deleted. A store tag is also a GC root, so the origin session's
        history up to that commit is kept for as long as the version
        is; it is released when the version is removed.

        Best-effort, and the shape of what it returns is why: the
        version has landed and the human's verb is done, so a name the
        store will not take costs the app a starting point and nothing
        else. The row then carries no origin, which every reader
        already has to handle.
        """
        tag = _origin_tag(pub, version)
        try:
            self._store.tags.add(Ref(session, commit), tag)
        except Exception as e:
            log.warning("publish: %s has no origin tag: %s", tag, e)
            return {}
        return {"origin": tag}

    @staticmethod
    def _origin_tags(entry: dict) -> list[str]:
        """The origin tags an app's versions record. A version that
        carries none contributes nothing — there is no name to drop and
        no commit pinned to release."""
        return [
            row["origin"]
            for row in (entry.get("versions") or {}).values()
            if row.get("origin")
        ]

    def _point_store_at(self, pub: str, token: str, version: str) -> None:
        """Point nontainer's registry at the version this app's
        manifest row names.

        Best-effort, because the manifest is the authority for which
        version an app serves: ``resolve`` opens the version the row
        names and never asks the registry which is current, so a
        pointer that will not move leaves the app serving exactly what
        it is recorded as serving. What the registry's pointer decides
        is which version ``unpublish`` refuses to drop while others
        remain — bookkeeping worth a log and worth reconciling later,
        and never worth failing a publish that has already landed.
        """
        try:
            self._store.set_current(pub, version)
        except Exception as e:
            log.warning(
                "publish: %s serves %s, which the store will not point at: %s",
                token,
                version,
                e,
            )

    def _reconcile_pointers(self) -> None:
        """Move every disagreeing store pointer onto the version its
        app's manifest row names.

        The two are written in that order — the row first, the pointer
        after — so a pointer that never moved (a raise, a machine that
        went away in the window) leaves the store naming the version
        before the one being served. The manifest is the authority, so
        reconciling is one-directional and idempotent: apps where the
        two already agree cost a comparison.

        Run at open and before a deletion, which is where a stale
        pointer bites: nontainer refuses to unpublish the version its
        registry points at while others remain, so a pointer left
        behind would keep the version the studio means to delete and
        drop nothing.

        A row naming a version the store does not hold is the MANIFEST
        being wrong, and no pointer move fixes that — it is logged and
        left, since a row a human can still read beats a guess written
        over it.
        """
        with self._lock:
            apps = self._manifest()["apps"]
            if not apps:
                return
            registry = self._store.publications()
            for token, entry in apps.items():
                current = entry.get("current")
                record = registry.get(_pub_name(entry, token))
                if not current or record is None or record.current == current:
                    continue
                self._point_store_at(_pub_name(entry, token), token, current)

    def _unpublish_version(self, pub: str, version: str) -> None:
        """Remove one published version: its tag, its branch, its record
        in nontainer's registry.

        Best-effort. A version that will not go is one no manifest row
        reaches any more, and refusing to finish the manifest write
        over it would leave the two halves disagreeing in the other
        direction — a row pointing at something the studio believes it
        deleted.
        """
        try:
            self._store.unpublish(pub, version)
        except Exception:
            log.warning("publish: could not unpublish %s/%s", pub, version)

    def _delete_tags(self, names: Iterable[str]) -> None:
        """Drop store-scoped tags by name.

        No session owns these names, so the store says it directly and
        no workspace is opened at all — which also means no executor is
        built, and a dud rung never boots a machine to delete a name.

        Best-effort per name: a tag that will not go is a name nothing
        reaches any more, and the manifest entry it belonged to is
        already gone.
        """
        for name in names:
            try:
                self._store.tags.delete(name)
            except Exception:
                log.warning("publish: could not delete tag %s", name)

    def branch_from_version(self, token: str, version: str) -> tuple[Session, int]:
        """Fork the origin session and rewind the child to that
        version's commit, so it opens with the files AND the
        conversation as they stood at that publish.

        Raises ``KeyError`` when the origin session is gone: the app's
        files are still served from the publication, but a conversation
        cannot be branched out of a branch that no longer exists.
        """
        entry = self._manifest()["apps"].get(token)
        if entry is None:
            raise KeyError(token)
        info = (entry.get("versions") or {}).get(version)
        if info is None:
            raise ValueError(f"this app has no version {version!r}")
        origin = entry.get("session")
        if origin not in self.known():
            raise KeyError(origin)
        session = self.open(origin)
        # The origin is read, never moved: ``at`` branches the child
        # from the version's commit and writes its identity on top,
        # so the parent keeps its head. Still reserved for the call — a
        # turn landing a commit under a fork of its own branch is the
        # half-turn state the fork guard exists for.
        if not session.turn_lock.acquire(blocking=False):
            raise RuntimeError("can't branch while a turn is running")
        try:
            child = self._fork_locked(
                session,
                conversation="inherit",
                at=info["commit"],
                transcript=self._transcript_at_publish(session, token, version),
            )
        finally:
            session.turn_lock.release()
        return child

    @staticmethod
    def _transcript_at_publish(
        session: Session, token: str, version: str
    ) -> list[dict] | None:
        """The session's transcript as it stood at that publish, or None
        if the marker is not in the event window (then the caller falls
        back to the whole log).

        Built by PROJECTING the events up to and including the marker,
        not by cutting the log after it: a truncate applies only to what
        is still visible, so a later edit that already hid the marker
        would leave the child's transcript stopping short of the state
        it holds. Truncates after the marker are none of the child's
        business — they unsay turns the child never inherited."""
        marker = next(
            (
                e
                for e in reversed(session.events)
                if e.get("type") == "publish"
                and e.get("token") == token
                and e.get("version") == version
            ),
            None,
        )
        if marker is None:
            return None
        seq = marker.get("seq", 0)
        prefix = [e for e in session.events if e.get("seq", 0) <= seq]
        return [event for _, event in _visible(prefix)]

    def restore_to_publish(self, session: Session, seq: int) -> int:
        """Rewind a session to one of its own publishes: the files and
        the conversation go back to where they stood when that version
        was published.

        The same synchronized rewind an edit does — one ``checkout``,
        because the conversation lives in the branch with the files —
        anchored on the marker's commit instead of a user event's
        pre-turn head. Returns the seq the transcript is cut AFTER: the
        marker survives its own rewind, since the version it names is
        still there and is still what you came back to.

        The commit is always reachable, however far the branch has
        wandered since: a rewind CHECKS OUT rather than moves the head,
        appending a commit whose content equals the target, so nothing
        the session ever held stops being an ancestor of where it is
        now. The version the marker names is a publication of its own
        and outlives the session either way.
        """
        event = next((e for e in session.events if e.get("seq") == seq), None)
        head = event.get("head") if event else None
        if event is None or event.get("type") != "publish" or not head:
            raise ValueError(f"event {seq} is not a publish marker")
        self._rewind(session, seq, head)
        return seq + 1
