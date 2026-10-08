"""nontainer-studio server: a chat UI over agno + nontainer with
versioned sessions. Single-user, localhost, no auth — a workbench,
not a deployment target.

Run:  ANTHROPIC_API_KEY=... nontainer-studio
      (or: uv run nontainer-studio, or python -m nontainer_studio)
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote

import anyio
from nontainer.adapters.a2ui import turn_to_a2ui
from nontainer.apps import build_router
from nontainer.apps import request as make_request
from nontainer.apps.contract import filter_headers
from nontainer.errors import SessionIdError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .session import ReservedSessionError, SweptSessionError
from .sessions import Registry
from .turns import (
    _run_turn,
    _wake_on_answers,
)

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 50_000_000  # upload bodies buffer in memory; cap them

HTTP_VERBS = ["GET", "POST", "PUT", "DELETE", "PATCH"]


DELEGATE_SWEEP_EVERY = 3600
"""Seconds between delegate retention sweeps.

Not a setting. The TTL is the decision a human makes; how often the
reaper looks is the studio's own business, and an hour is finer than
any TTL worth spelling in hours. A pass costs a manifest read plus
whatever it deletes.
"""


async def _sweep_delegates_forever(
    registry: Registry, every: float = DELEGATE_SWEEP_EVERY
) -> None:
    """Run the retention sweep on a timer for as long as the server is
    up. The registry sweeps once at open, so this sleeps first.

    Nothing may escape the loop: a pass that fails — a branch held
    open, a manifest half-written — costs that pass and not the
    schedule, or one bad delegate turns retention off until the next
    restart.
    """
    while True:
        await asyncio.sleep(every)
        try:
            await anyio.to_thread.run_sync(registry.sweep_delegates)
        except Exception as e:  # noqa: BLE001 - the schedule outlives a bad pass
            log.warning("delegate sweep failed: %s", e)


#: The snapshots the request being served has acquired, so the wrapper
#: around the router can release them once the response is sent. Set per
#: request by :func:`holding_snapshots`; the router's resolve hook runs in
#: the same task, so it sees the list.
_HELD: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "held_snapshots", default=None
)


def _resolve_held(registry: Any) -> Callable[[str], Any]:
    """The router's resolve hook: acquire the snapshot for this request,
    so it is not closed under it (see ``Registry.acquire_snapshot``). Outside a
    request :func:`holding_snapshots` wraps, it is plain ``resolve``,
    since nothing would release what it acquired."""

    def resolve(token: str) -> Any:
        held = _HELD.get()
        if held is None:
            return registry.resolve(token)
        ws = registry.acquire_snapshot(token)
        if ws is not None:
            held.append(ws)
        return ws

    return resolve


def holding_snapshots(app: Any, registry: Any) -> Any:
    """Release, once the response is sent, every snapshot the request
    acquired. Releasing can close a snapshot that was retired while the
    request held it, and closing a dud-backed one waits for its guest,
    so it runs off the event loop."""

    async def wrapped(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        held: list = []
        reset = _HELD.set(held)
        try:
            await app(scope, receive, send)
        finally:
            _HELD.reset(reset)
            for ws in held:
                await anyio.to_thread.run_sync(registry.release_snapshot, ws)

    return wrapped


def cors_for_apps(app: Any) -> Any:
    """Wrap the published-app router so a sandboxed iframe can read it.

    Same reason the live preview route sets these headers: the preview
    iframe is sandboxed WITHOUT ``allow-same-origin``, so it is an
    OPAQUE origin and every request it makes — the jsx loader fetching
    ``app.jsx``, the app's own ``fetch('api/x')`` — is cross-origin.
    Without the header the browser hides the response from the page and
    reports a CORS error; the same URL in a top-level tab works, which
    is what makes this failure so confusing to meet. ``build_router``
    sends no CORS headers and its response-header allowlist strips any
    a handler sets, so the header has to be added out here, around the
    mount, rather than by the app or by nontainer.

    The threat framing, since ``*`` on a serving origin deserves one: a
    published app is behind a capability token, and the header lets any
    page that ALREADY HOLDS that URL read the app's responses from a
    foreign origin. That is what embedding a published app anywhere —
    our own sandboxed preview included — requires, and it grants
    nothing the URL did not already grant: without the token there is
    no request to read the answer to. What it must not become is
    ``allow-same-origin`` on the iframe, which would hand the app the
    studio's origin and with it the session API. That stays off.
    """
    from starlette.datastructures import MutableHeaders

    async def wrapped(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        if scope["method"] == "OPTIONS":
            # Preflight: build_router routes only the real verbs, so an
            # unanswered OPTIONS 405s and the browser blocks the request
            # it was asking about, whatever headers the answer carried.
            asked = Headers(scope=scope).get("access-control-request-headers", "*")
            await Response(
                status_code=204,
                headers={
                    "access-control-allow-origin": "*",
                    "access-control-allow-methods": ", ".join(HTTP_VERBS),
                    "access-control-allow-headers": asked,
                    "access-control-max-age": "600",
                },
            )(scope, receive, send)
            return

        async def with_header(message: Any) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["access-control-allow-origin"] = "*"
            await send(message)

        await app(scope, receive, with_header)

    return wrapped


# ---------------------------------------------------------------------------
# agno stream -> client events (a small, defensive mapping: unknown
# event types are skipped so agno upgrades degrade gracefully)
# ---------------------------------------------------------------------------


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# a2ui egress: project the event feed into an A2UI v0.9 message stream
# ---------------------------------------------------------------------------


class _A2uiTurns:
    """Turn-level projection of the transcript into A2UI v0.9 messages —
    one surface per turn. The event log is the internal model; a2ui is an
    EDGE format, so this stays a thin projection, never a second source of
    truth.

    Accumulate per turn: text deltas concatenate into prose, `artifact`
    events collect as (name, path) pairs. Everything else — thinking, tool
    calls, usage, notices, the `user` echo — is ignored: a2ui renders the
    agent's reply surface, not the whole conversation. On `done` the turn
    becomes one createSurface + updateComponents + updateDataModel-per-entry
    via ``turn_to_a2ui``; the surface id is derived from the done event's
    seq, so it is unique per turn AND deterministic across replays (the log
    is the only input). Empty turns (no prose, no artifacts) emit nothing.

    A `truncate` (an edit's rewind) deletes every surface this stream
    already emitted whose driving turn is at-or-after the cut — the
    v0.9-native ``deleteSurface`` is how rewound turns are voided — and
    drops any in-progress accumulation.

    Every emitted message rides the DRIVING event's cursor (the done seq, or
    the truncate seq) so a consumer resumes with ?since= exactly like the
    native feed. One instance PER CONNECTION: the emitted-surface list is
    per-stream state the delete path reads back.
    """

    def __init__(
        self,
        name: str,
        read_bytes: Callable[[str], bytes | None],
        file_url: Callable[[str], str],
    ) -> None:
        self._name = name
        self._read_bytes = read_bytes
        self._file_url = file_url
        self._prose: list[str] = []
        self._arts: list[tuple[str, str]] = []
        self._emitted: list[tuple[int, str]] = []  # (done_seq, surface_id)

    def feed(self, seq: int, event: dict) -> list[dict]:
        """Drive one event through the accumulator; return the a2ui messages
        it produces (each already carrying its cursor). Blocking — reads
        artifact bytes on `done` — so callers offload it to a thread."""
        kind = event.get("type")
        if kind == "text":
            delta = event.get("delta")
            if isinstance(delta, str):
                self._prose.append(delta)
        elif kind == "artifact":
            name, path = event.get("name"), event.get("path")
            if name and path:
                self._arts.append((name, path))
        elif kind == "done":
            return self._flush_turn(seq)
        elif kind == "truncate":
            return self._delete_rewound(seq, event.get("to", 0))
        return []

    def _flush_turn(self, done_seq: int) -> list[dict]:
        prose = "".join(self._prose)
        arts = self._arts
        self._prose, self._arts = [], []
        if not prose and not arts:
            return []  # empty turn: nothing to render
        surface_id = f"{self._name}-turn-{done_seq}"
        messages = turn_to_a2ui(
            prose, arts, self._read_bytes, self._file_url, surface_id=surface_id
        )
        self._emitted.append((done_seq, surface_id))
        return [{"cursor": done_seq, **m} for m in messages]

    def _delete_rewound(self, trunc_seq: int, to: int) -> list[dict]:
        # An edit rewinds to a user event (seq `to`); every turn from there
        # on is void. A turn's done seq is > its user seq, so `done_seq >= to`
        # selects exactly the turns at-or-after the cut.
        self._prose, self._arts = [], []
        out: list[dict] = []
        kept: list[tuple[int, str]] = []
        for done_seq, surface_id in self._emitted:
            if done_seq >= to:
                out.append(
                    {
                        "cursor": trunc_seq,
                        "version": "v0.9",
                        "deleteSurface": {"surfaceId": surface_id},
                    }
                )
            else:
                kept.append((done_seq, surface_id))
        self._emitted = kept
        return out


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


def build_app(registry: Registry) -> Starlette:
    def with_session(handler):
        """Resolve the route's {name} to a Session or 404 — every
        session endpoint starts the same way, so say it once.
        Manifest-known sessions (prior runs) open lazily: a reload
        pointing at yesterday's session must not 404 until something
        happens to POST /api/sessions."""

        async def wrapped(request: Any) -> Any:
            name = request.path_params["name"]
            session = registry.get(name)
            if session is None and name in registry.known():
                try:
                    session = await anyio.to_thread.run_sync(registry.open, name)
                except SweptSessionError as e:
                    # A delegate the retention sweep took. Its row can
                    # outlive its branch, so a reload aimed at one lands
                    # here — and the answer is what became of it, never
                    # a fresh session wearing its name.
                    return JSONResponse({"error": str(e)}, status_code=409)
            if session is None:
                return JSONResponse({"error": f"no session {name!r}"}, status_code=404)
            return await handler(request, session)

        return wrapped

    def human_driven(handler):
        """``with_session`` for a verb a human may not aim at a
        delegate — refuse with 409 instead.

        A delegate is work one agent handed another: the parent writes
        its prompts, judges its branch and integrates it. The studio
        shows a delegate so a human can READ it; a human turn landing
        in the middle of that exchange would rewrite a transcript the
        parent is still reading. Publishing counts: it commits the
        delegate's open work, puts a version behind a public URL and
        appends a landmark to the transcript. Reading verbs are
        untouched, and so are fork (take the branch as your own
        session) and delete.

        The app-side routes need no such rule: they are addressed by
        publication token, and moving a pointer, taking an app down or
        deleting a version reaches no session at all.
        """

        @with_session
        async def wrapped(request: Any, session: Any) -> Any:
            parent = await anyio.to_thread.run_sync(registry.parent_of, session.name)
            if parent is not None:
                return JSONResponse(
                    {
                        "error": (
                            f"{session.name} is a delegate of {parent}: "
                            "the parent drives it"
                        )
                    },
                    status_code=409,
                )
            return await handler(request, session)

        return wrapped

    async def index(request: Any) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    async def list_sessions(request: Any) -> JSONResponse:
        return JSONResponse({"sessions": registry.list()})

    async def open_session(request: Any) -> JSONResponse:
        """Create-or-return. With no `name`, MINT one — that's what the
        UI does ("+ New"), so identity is always a slug nobody typed.
        An explicit `name` still works (tests, scripting); it never
        becomes the session's label either way — titles do that."""
        body = await request.json()
        name = (body.get("name") or "").strip()
        try:
            if name:
                session = await anyio.to_thread.run_sync(registry.open, name)
            else:
                session = await anyio.to_thread.run_sync(registry.create)
        except (SweptSessionError, ReservedSessionError) as e:
            # 409 rather than 400: the name was well formed and was a
            # session. Its state has since been collected, or an app it
            # published still holds the name — a conflict with the
            # store's state either way, and not a typo.
            return JSONResponse({"error": str(e)}, status_code=409)
        except SessionIdError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(
            {
                "ok": True,
                "name": session.name,
                "title": registry.title_of(session.name),
            }
        )

    @with_session
    async def session_info(request: Any, session: Any) -> JSONResponse:
        """What one name IS. The rail's own list answers this for every
        session it shows; a delegate has no row there, so a shell that
        lands on `?session=<child>` after a reload has nothing else to
        ask. `delegate` is null for an ordinary session and otherwise
        names the parent that drives it."""
        delegate = await anyio.to_thread.run_sync(registry.delegate_of, session.name)
        return JSONResponse(
            {
                "name": session.name,
                "title": registry.title_of(session.name),
                "model": session.model,
                "busy": session.busy,
                "delegate": delegate,
                # Messages queued while the turn runs, in arrival order.
                # The server is the truth about them: they live on the
                # session, not in the tab that typed them, so a reload
                # (or a second tab) shows what is still waiting.
                "queued": [
                    {"id": note.id, "text": note.text}
                    for note in session.inbox.pending()
                ],
            }
        )

    @human_driven
    async def set_title(request: Any, session: Any) -> JSONResponse:
        """The human's rename. A blank title CLEARS the override, so the
        rail falls back to whatever the agent last suggested."""
        body = await request.json()
        # off-loop: set_user_title takes the registry lock, which a
        # session open can hold for the length of a workspace build —
        # blocking here would stall every SSE follower with it
        title = await anyio.to_thread.run_sync(
            registry.set_user_title, session.name, body.get("title")
        )
        return JSONResponse({"ok": True, "name": session.name, "title": title})

    @human_driven
    async def chat(request: Any, session: Any) -> Any:
        """Send a message. With a turn already running it is QUEUED
        rather than refused: the agent reads it with its next tool
        result, and the turn that is running is not interrupted — 202,
        because the work of it has not started."""
        body = await request.json()
        message = (body.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "empty message"}, status_code=400)
        # Queued or not, the human is back: delegates' answers may wake
        # the session again, on a fresh budget.
        session.human_spoke()
        if not session.turn_lock.acquire(blocking=False):
            if not session.in_turn:
                # The lock is held as a reservation rather than by a
                # turn — a publish takes it that way — and a message
                # queued there would wait for a delivery nothing is
                # about to make.
                return JSONResponse(
                    {"error": "a turn is already running"}, status_code=409
                )
            note = session.inbox.put(message)
            return JSONResponse(
                {"ok": True, "queued": note.id, "since": session.next_seq},
                status_code=202,
            )
        # keep a strong reference: the loop holds tasks weakly, and a
        # GC'd task is a silently dead turn with a stuck lock. The
        # flag is set here rather than left to the task: the task
        # starts on a later tick, and a message arriving in between is
        # one for the turn that is starting.
        session.in_turn = True
        session.turn_task = asyncio.create_task(_run_turn(session, message, registry))
        return JSONResponse({"ok": True, "since": session.next_seq})

    @human_driven
    async def unqueue(request: Any, session: Any) -> Any:
        """Take a queued message back. Only while it is still pending:
        delivery cannot be undone, so a note already on its way to the
        model answers 404 rather than pretending it was withdrawn."""
        note_id = request.path_params["id"]
        if not session.inbox.withdraw(note_id):
            return JSONResponse(
                {"error": f"no queued message {note_id!r}"}, status_code=404
            )
        return JSONResponse({"ok": True})

    @human_driven
    async def edit(request: Any, session: Any) -> Any:
        """Edit an earlier prompt: rewind files + agent memory to just
        before that turn, drop it and everything after from the visible
        transcript, and run the edited message as a fresh turn. The
        event log stays append-only — a `truncate {to}` event marks the
        cut and projections (client + _visible) apply it."""
        body = await request.json()
        message = (body.get("message") or "").strip()
        seq = body.get("seq")
        if not message:
            return JSONResponse({"error": "empty message"}, status_code=400)
        target = (
            next((e for e in session.events if e.get("seq") == seq), None)
            if isinstance(seq, int) and not isinstance(seq, bool)
            else None
        )
        if target is None or target.get("type") != "user":
            return JSONResponse(
                {"error": "seq must name a user event"}, status_code=400
            )
        if not session.turn_lock.acquire(blocking=False):
            return JSONResponse({"error": "a turn is already running"}, status_code=409)
        session.human_spoke()
        try:
            await anyio.to_thread.run_sync(registry.rewind_to_event, session, seq)
        except Exception as e:
            session.turn_lock.release()
            return JSONResponse({"error": str(e)}, status_code=400)
        await session.emit({"type": "truncate", "to": seq})
        session.in_turn = True
        session.turn_task = asyncio.create_task(_run_turn(session, message, registry))
        return JSONResponse({"ok": True, "since": session.next_seq})

    @with_session
    async def cancel(request: Any, session: Any) -> Any:
        """Stop the running turn GRACEFULLY: agno's cancel-by-run-id
        raises at the loop's next cancellation point (a mid-flight tool call
        finishes first), the run persists with its partial work, and
        the turn ends with a RunCancelled event -> 'turn stopped'
        notice. Nothing is torn down — the next prompt just works."""
        if not session.busy:
            return JSONResponse({"error": "no turn is running"}, status_code=409)
        # the run id lands with the first streamed event; a stop click
        # can race it by a beat
        for _ in range(20):
            if session.run_id is not None or not session.busy:
                break
            await asyncio.sleep(0.05)
        if session.run_id is None:
            return JSONResponse(
                {"error": "turn not started yet; try again"}, status_code=409
            )
        await session.agent.acancel_run(session.run_id)
        return JSONResponse({"ok": True})

    @with_session
    async def events(request: Any, session: Any) -> Any:
        """Transcript feed. Default: SSE — replay from ?since=N, then
        follow live (each event rides with its cursor so clients
        resubscribe exactly where they left off). With ?wait=0: an
        immediate JSON snapshot of the buffer — for pollers and tests
        (Starlette's TestClient drains responses, so it can't consume
        a never-ending stream)."""
        try:
            since = int(request.query_params.get("since", 0))
        except ValueError:
            return JSONResponse({"error": "since must be an integer"}, status_code=400)

        if request.query_params.get("wait") == "0":
            return JSONResponse(
                {
                    "events": [e for e in session.events if e["seq"] >= since],
                    "next": session.next_seq,
                }
            )

        async def stream() -> AsyncIterator[str]:
            async for cursor, event in session.follow(since):
                yield _sse({"cursor": cursor, **event})

        return StreamingResponse(stream(), media_type="text/event-stream")

    @with_session
    async def a2ui(request: Any, session: Any) -> Any:
        """A2UI v0.9 projection of the transcript, TURN-LEVEL: each turn is
        one surface (prose + its artifacts), edits delete rewound surfaces.
        Same shape as /events — SSE that replays from ?since=N then follows
        live, or a ?wait=0 JSON snapshot for pollers and tests. Each message
        rides the driving event's cursor so ?since= resumes identically.

        The projection reads artifact bytes from the workspace, so it runs
        in a thread (mirroring file_raw); an unreadable artifact degrades to
        a Text+link inside the converter, never a failure."""
        name = request.path_params["name"]
        try:
            since = int(request.query_params.get("since", 0))
        except ValueError:
            return JSONResponse({"error": "since must be an integer"}, status_code=400)

        def read_bytes(path: str) -> bytes | None:
            try:
                with session.ws.lock:
                    return session.ws.files.fs.read(path)
            except Exception:
                return None

        def file_url(path: str) -> str:
            return f"/api/sessions/{name}/file?path={quote(path)}"

        if request.query_params.get("wait") == "0":
            # Snapshot ON THE LOOP THREAD before offloading: emit() also
            # runs on the loop, but its compaction slice-replaces the tail
            # and trims the front past MAX_EVENTS — under a worker-thread
            # iteration those shifts skip/double events (a skipped `done`
            # silently folds one turn into the next). A shallow copy is
            # enough: compaction replaces event dicts, never mutates them.
            # (The native /events?wait=0 reads on the loop, so only this
            # thread-offloaded projection needs the copy.)
            events_snapshot = list(session.events)

            def project() -> list[dict]:
                # Project the WHOLE buffer (so surface tracking for
                # deleteSurface is complete), then keep only messages at-or-
                # after the cursor — the resume filter, like the events route.
                projector = _A2uiTurns(name, read_bytes, file_url)
                out: list[dict] = []
                for event in events_snapshot:
                    out.extend(projector.feed(event["seq"], event))
                return [m for m in out if m["cursor"] >= since]

            messages = await anyio.to_thread.run_sync(project)
            return JSONResponse({"messages": messages, "next": session.next_seq})

        async def stream() -> AsyncIterator[str]:
            projector = _A2uiTurns(name, read_bytes, file_url)
            async for cursor, event in session.follow(since):
                for msg in await anyio.to_thread.run_sync(
                    projector.feed, cursor, event
                ):
                    yield _sse(msg)

        return StreamingResponse(stream(), media_type="text/event-stream")

    # -- upload: browser file -> workspace write ---------------------------
    # Raw body, not multipart (the browser sends File bytes natively).
    # Each upload is a workspace file write: committed, so an edit's rewind
    # extends to uploads for free. Multi-file drops are N parallel requests —
    # the workspace lock serializes them safely. Same-name uploads
    # overwrite (idempotent re-drops).

    @human_driven
    async def upload(request: Any, session: Any) -> JSONResponse:
        filename = Path(request.query_params.get("name", "")).name  # basename only
        if not filename:
            return JSONResponse({"error": "missing ?name="}, status_code=400)
        length = int(request.headers.get("content-length") or 0)
        if length > MAX_UPLOAD:
            return JSONResponse(
                {
                    "error": f"too large ({length} bytes; cap {MAX_UPLOAD}). "
                    "For big data, mount a host directory instead."
                },
                status_code=413,
            )
        data = await request.body()
        if len(data) > MAX_UPLOAD:  # content-length can lie (chunked)
            return JSONResponse(
                {"error": f"too large ({len(data)} bytes; cap {MAX_UPLOAD})"},
                status_code=413,
            )
        dest = f"{session.ws.root}/uploads/{filename}"
        out = await anyio.to_thread.run_sync(session.ws.files.write, dest, data)
        await session.emit(
            {"type": "notice", "text": f"uploaded {dest} ({out.size:,} bytes)"}
        )
        return JSONResponse({"ok": True, "path": dest, "size": out.size})

    # -- files: tree + raw bytes (the files tab; also inline chat images)

    @with_session
    async def files(request: Any, session: Any) -> JSONResponse:
        def walk_all() -> list[str]:
            with session.ws.lock:
                paths: list[str] = []

                def walk(d: str, depth: int = 0) -> None:
                    if depth > 32:
                        return
                    for entry in sorted(session.ws.files.fs.list(d)):
                        full = f"{d.rstrip('/')}/{entry}"
                        if session.ws.files.fs.isdir(full):
                            walk(full, depth + 1)
                        else:
                            paths.append(full)

                walk("/")
                return paths

        return JSONResponse({"files": await anyio.to_thread.run_sync(walk_all)})

    @with_session
    async def file_raw(request: Any, session: Any) -> Any:
        path = request.query_params.get("path", "")

        def read() -> bytes:
            with session.ws.lock:
                return session.ws.files.fs.read(path)

        try:
            data = await anyio.to_thread.run_sync(read)
        except Exception:
            return JSONResponse({"error": f"cannot read {path!r}"}, status_code=404)
        media, _ = mimetypes.guess_type(path)
        return Response(data, media_type=media or "application/octet-stream")

    @with_session
    async def fork(request: Any, session: Any) -> JSONResponse:
        """Branch this session into a new one: files, cache, cwd and —
        unless the body says `"conversation": "fresh"` — the agent's
        memory and the visible transcript, all in one kvgit operation.
        The app db is named, not copied: the child writes to the same
        file as the parent, since live state has no history.

        409 while a turn is in flight or the workspace holds staged
        changes: a fork of half a turn would be a state no commit
        ever held."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        conversation = (body.get("conversation") or "inherit").strip()
        if conversation not in ("inherit", "fresh"):
            return JSONResponse(
                {"error": "conversation must be 'inherit' or 'fresh'"}, status_code=400
            )
        try:
            child = await anyio.to_thread.run_sync(
                partial(registry.fork, session, conversation=conversation)
            )
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse(
            {
                "ok": True,
                "name": child.name,
                "title": registry.title_of(child.name),
            }
        )

    @with_session
    async def delete_session(request: Any, session: Any) -> JSONResponse:
        if session.busy:
            return JSONResponse(
                {"error": "can't delete while a turn is running"}, status_code=409
            )
        await anyio.to_thread.run_sync(registry.delete, session)
        return JSONResponse({"ok": True})

    @with_session
    async def app_exists(request: Any, session: Any) -> JSONResponse:
        """The preview pane's probe. A JSON 200 either way — probing
        /preview/ itself means a console-logged 404 on every empty
        session (browsers log failed responses even when handled)."""

        def check() -> bool:
            with session.ws.lock:
                return bool(session.ws.files.fs.isdir(f"{session.ws.root}/app"))

        return JSONResponse({"exists": await anyio.to_thread.run_sync(check)})

    # -- live preview: dispatch into the AUTHORING runtime ---------------
    # Mutable (unlike frozen /apps serving): the iframe shows the app as
    # the agent builds it. Dispatch holds the workspace's single-writer
    # lock, so preview requests serialize safely with agent turns.

    @with_session
    async def preview(request: Any, session: Any) -> Any:
        if request.method == "OPTIONS":
            # CORS preflight: the sandboxed iframe is an opaque origin,
            # and any non-simple request from app code (a JSON POST is
            # the canonical case) preflights first. Answer it, or the
            # browser blocks the real request no matter what headers
            # the response would have carried.
            return Response(
                status_code=204,
                headers={
                    "access-control-allow-origin": "*",
                    "access-control-allow-methods": ", ".join(verbs),
                    "access-control-allow-headers": request.headers.get(
                        "access-control-request-headers", "*"
                    ),
                    "access-control-max-age": "600",
                },
            )
        path = "/" + request.path_params.get("path", "")
        url = path + (f"?{request.url.query}" if request.url.query else "")
        body = await request.body()
        req = make_request(
            request.method, url, body=body, headers=filter_headers(request.headers)
        )
        wire = await anyio.to_thread.run_sync(session.runtime.dispatch, req)
        # The preview iframe is sandboxed WITHOUT allow-same-origin (an
        # opaque origin can't reach the studio API), which makes the
        # app's own relative fetches cross-origin — hence the CORS
        # header. Real deployments should serve apps from a separate
        # origin entirely (see nontainer's docs/apps.md threat framing).
        headers = {**dict(wire.headers), "access-control-allow-origin": "*"}
        return Response(
            wire.content,
            status_code=wire.status,
            media_type=wire.content_type,
            headers=headers,
        )

    # -- models: what this server's env unlocks; per-session switching ---

    async def list_models(request: Any) -> JSONResponse:
        from . import providers

        return JSONResponse(providers.available())

    @human_driven
    async def set_model(request: Any, session: Any) -> JSONResponse:
        if session.busy:
            return JSONResponse(
                {"error": "can't switch models while a turn is running"},
                status_code=409,
            )
        spec = ((await request.json()).get("model") or "").strip()
        if not spec:
            return JSONResponse({"error": "missing model"}, status_code=400)
        try:
            await anyio.to_thread.run_sync(registry.set_model, session, spec)
        except (ValueError, SystemExit) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await session.emit(
            {"type": "notice", "text": f"model → {spec} (memory carries over)"}
        )
        return JSONResponse({"ok": True, "model": spec})

    # -- publish: a version of an app, behind a capability URL -----------
    # An app is a nontainer publication: one token, one URL, one db of
    # its own, and a growing set of versions, each an immutable commit
    # of the `app/` tree alone. The URL serves whichever version is
    # current, so publishing again moves it forward and `POST
    # /api/apps/{token}/current` moves it back.

    @human_driven
    async def publish(request: Any, session: Any) -> JSONResponse:
        """Publish a new version of the session's app, starting it if
        this is the first. `name` names the version (default: v1, v2,
        ... within the app).

        There is no picking which app: a session has one, and a second
        app is a fork's. A body still carrying an `app` key is refused
        rather than quietly publishing somewhere the caller did not
        mean."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        version = body.get("name")
        if version is not None and not isinstance(version, str):
            return JSONResponse({"error": "name must be a string"}, status_code=400)
        if "app" in body:
            return JSONResponse(
                {
                    "error": "a session publishes one app; fork the session to "
                    "start another"
                },
                status_code=400,
            )
        # ONE reservation across the version and the marker. Publishing
        # under its own lock and emitting after it would let a chat
        # request slip between them: its `user` event would sit above a
        # landmark whose commit predates the turn, and restoring to
        # that marker would rewind the files under a prompt still on
        # screen.
        if not session.turn_lock.acquire(blocking=False):
            return JSONResponse(
                {"error": "can't publish while a turn is running"}, status_code=409
            )
        try:
            published = await anyio.to_thread.run_sync(
                partial(registry._publish_locked, session, version=version)
            )
        except ValueError as e:
            session.turn_lock.release()
            return JSONResponse({"error": str(e)}, status_code=400)
        except BaseException:
            session.turn_lock.release()
            raise
        # The marker is emitted only once the version exists: it is a
        # durable landmark in the conversation (the human can open it,
        # or come back to it), so it must never describe a publish that
        # didn't happen.
        await session.emit(
            {
                "type": "publish",
                "token": published["token"],
                "version": published["version"],
                "title": published["title"],
                "url": published["url"],
                "head": published["commit"],
                "tree": published["tree"],
            }
        )
        session.turn_lock.release()
        return JSONResponse(published)

    @with_session
    async def session_apps(request: Any, session: Any) -> JSONResponse:
        """This session's apps, each with how far its files have moved
        since the app's newest version."""
        rows = await anyio.to_thread.run_sync(registry.session_apps, session)
        return JSONResponse({"apps": rows})

    @with_session
    async def app_file_change(request: Any, session: Any) -> JSONResponse:
        """One app file's two sides: `?path=` as the version named by
        `?since=` holds it (default: the app's newest) and as the
        session holds it now.

        A read, so a delegate may ask it: a human looking at what a
        delegate built needs to see the edit, and a diff commits
        nothing. 404 for an app this session did not publish, for a
        path outside its `app/` tree and for one neither side holds;
        400 for a version the app does not hold."""
        try:
            change = await anyio.to_thread.run_sync(
                partial(
                    registry.app_file_change,
                    session,
                    request.path_params["token"],
                    request.query_params.get("path", ""),
                    since=request.query_params.get("since") or None,
                )
            )
        except KeyError as e:
            return JSONResponse({"error": str(e.args[0])}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(change)

    async def list_apps(request: Any) -> JSONResponse:
        return JSONResponse(
            {"apps": await anyio.to_thread.run_sync(registry.list_apps)}
        )

    async def set_app_description(request: Any) -> JSONResponse:
        """The human's own words for an app. Blank CLEARS them, so the
        row falls back to what the last publish generated."""
        token = request.path_params["token"]
        body = await request.json()
        try:
            app = await anyio.to_thread.run_sync(
                registry.set_app_description, token, body.get("description")
            )
        except KeyError:
            return JSONResponse({"error": f"no app {token!r}"}, status_code=404)
        return JSONResponse(app)

    async def set_current(request: Any) -> JSONResponse:
        """Repoint an app's URL at one of its versions — rollback, or
        roll forward again. Nothing is rebuilt; the pointer moves and
        the cached snapshot is dropped."""
        token = request.path_params["token"]
        body = await request.json()
        version = (body.get("version") or "").strip()
        if not version:
            return JSONResponse({"error": "missing version"}, status_code=400)
        try:
            app = await anyio.to_thread.run_sync(registry.set_current, token, version)
        except KeyError:
            return JSONResponse({"error": f"no app {token!r}"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(app)

    async def unpublish(request: Any) -> JSONResponse:
        token = request.path_params["token"]
        try:
            await anyio.to_thread.run_sync(registry.unpublish, token)
        except KeyError:
            return JSONResponse({"error": f"no app {token!r}"}, status_code=404)
        return JSONResponse({"ok": True})

    async def delete_version(request: Any) -> JSONResponse:
        token = request.path_params["token"]
        version = request.path_params["version"]
        try:
            app = await anyio.to_thread.run_sync(
                registry.delete_version, token, version
            )
        except KeyError:
            return JSONResponse({"error": f"no app {token!r}"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(app)

    async def branch_version(request: Any) -> JSONResponse:
        """Fork the origin session and rewind the child to this
        version's commit: the files and the conversation as they stood
        at that publish. 404 when the origin session is gone — the app
        is still served and still readable, but there is no conversation
        left to branch."""
        token = request.path_params["token"]
        version = request.path_params["version"]
        try:
            child = await anyio.to_thread.run_sync(
                registry.branch_from_version, token, version
            )
        except KeyError:
            return JSONResponse(
                {
                    "error": f"the session that published {token!r} is gone — its "
                    "files are still served at the app's URL, but there is no "
                    "conversation left to branch from"
                },
                status_code=404,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except RuntimeError as e:  # the origin session has a turn in flight
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse(
            {"ok": True, "name": child.name, "title": registry.title_of(child.name)}
        )

    @human_driven
    async def restore(request: Any, session: Any) -> JSONResponse:
        """Rewind to one of this session's own publishes: files, agent
        memory and title go back to where that version was published, and
        the transcript is cut after the marker. The same machinery an
        edit uses — one checkout, because the conversation lives in the
        branch — with no new turn started."""
        body = await request.json()
        seq = body.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return JSONResponse(
                {"error": "seq must name a publish event"}, status_code=400
            )
        if not session.turn_lock.acquire(blocking=False):
            return JSONResponse({"error": "a turn is already running"}, status_code=409)
        try:
            cut = await anyio.to_thread.run_sync(
                registry.restore_to_publish, session, seq
            )
        except ValueError as e:
            session.turn_lock.release()
            return JSONResponse({"error": str(e)}, status_code=400)
        # the cut goes out before the lock does: a chat request that
        # won the lock in between would land its `user` event above the
        # truncate that was meant to precede it
        await session.emit({"type": "truncate", "to": cut})
        session.turn_lock.release()
        return JSONResponse({"ok": True, "since": session.next_seq})

    @with_session
    async def session_delegates(request: Any, session: Any) -> JSONResponse:
        """What this session delegated, and what became of each one —
        the listing behind the rail's ⑂ badge."""
        rows = await anyio.to_thread.run_sync(registry.delegate_rows, session.name)
        return JSONResponse({"delegates": rows})

    @with_session
    async def keep_delegate(request: Any, session: Any) -> JSONResponse:
        """Keep a delegate's branch from the retention sweep, or let it
        go again. `{"kept": false}` un-keeps."""
        child = request.path_params["child"]
        body = await request.json()
        try:
            row = await anyio.to_thread.run_sync(
                registry.keep_delegate,
                session.name,
                child,
                bool(body.get("kept", True)),
            )
        except KeyError:
            return JSONResponse(
                {"error": f"{session.name!r} has no delegate {child!r}"},
                status_code=404,
            )
        return JSONResponse({"ok": True, "delegate": row})

    async def api_fallback(request: Any) -> Response:
        """Unmatched /api/* — almost always an app in the preview
        iframe using ABSOLUTE urls, which escape the /preview/{name}/
        prefix and land here. Without CORS headers the sandboxed
        iframe (opaque origin) sees only an unexplained CORS block;
        answer preflights and 404 WITH the teaching text instead."""
        cors = {"access-control-allow-origin": "*"}
        if request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers={
                    **cors,
                    "access-control-allow-methods": ", ".join(verbs),
                    "access-control-allow-headers": request.headers.get(
                        "access-control-request-headers", "*"
                    ),
                },
            )
        # JSON so the app's own res.json() error path can read it
        return JSONResponse(
            {
                "error": "nontainer: absolute path -- this app is served "
                "under a prefix (/preview/<session>/) and must use "
                "RELATIVE urls (fetch('api/x'), not fetch('/api/x'))"
            },
            status_code=404,
            headers=cors,
        )

    @asynccontextmanager
    async def lifespan(app: Any):
        # Delegates run on this loop, where the routes that follow their
        # transcripts listen: set before any request opens a session.
        registry.serve_on(asyncio.get_running_loop())
        # The sweep needs a loop to be scheduled on, and this is the
        # only place the studio has one for the life of the server. No
        # TTL, no timer: 0 is retention off, not retention every hour
        # with nothing to take.
        sweeper = (
            # the interval is read HERE rather than bound as a
            # default: a default argument is fixed when the function is
            # defined, which is before anything can change it
            asyncio.create_task(
                _sweep_delegates_forever(registry, DELEGATE_SWEEP_EVERY)
            )
            if registry.delegate_ttl > 0
            else None
        )
        # Delegates' answers wake idle sessions, as each one lands.
        waker = asyncio.create_task(_wake_on_answers(registry))
        try:
            yield
        finally:
            waker.cancel()
            await asyncio.gather(waker, return_exceptions=True)
            if sweeper is not None:
                sweeper.cancel()
                await asyncio.gather(sweeper, return_exceptions=True)
            await registry.aclose()

    verbs = HTTP_VERBS
    preview_verbs = verbs + ["OPTIONS"]
    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/models", list_models, methods=["GET"]),
            Route("/api/sessions", list_sessions, methods=["GET"]),
            Route("/api/sessions", open_session, methods=["POST"]),
            Route("/api/sessions/{name}/model", set_model, methods=["POST"]),
            Route("/api/sessions/{name}/title", set_title, methods=["POST"]),
            Route("/api/sessions/{name}", session_info, methods=["GET"]),
            Route("/api/sessions/{name}", delete_session, methods=["DELETE"]),
            Route("/api/sessions/{name}/chat", chat, methods=["POST"]),
            Route("/api/sessions/{name}/queue/{id}", unqueue, methods=["DELETE"]),
            Route("/api/sessions/{name}/edit", edit, methods=["POST"]),
            Route("/api/sessions/{name}/cancel", cancel, methods=["POST"]),
            Route("/api/sessions/{name}/events", events, methods=["GET"]),
            Route("/api/sessions/{name}/a2ui", a2ui, methods=["GET"]),
            Route("/api/sessions/{name}/upload", upload, methods=["POST"]),
            Route("/api/sessions/{name}/files", files, methods=["GET"]),
            Route("/api/sessions/{name}/app", app_exists, methods=["GET"]),
            Route("/api/sessions/{name}/file", file_raw, methods=["GET"]),
            Route("/api/sessions/{name}/publish", publish, methods=["POST"]),
            Route("/api/sessions/{name}/apps", session_apps, methods=["GET"]),
            Route(
                "/api/sessions/{name}/apps/{token}/changes/file",
                app_file_change,
                methods=["GET"],
            ),
            Route("/api/sessions/{name}/restore", restore, methods=["POST"]),
            Route("/api/sessions/{name}/fork", fork, methods=["POST"]),
            Route("/api/sessions/{name}/delegates", session_delegates, methods=["GET"]),
            Route(
                "/api/sessions/{name}/delegates/{child}/keep",
                keep_delegate,
                methods=["POST"],
            ),
            Route("/api/apps", list_apps, methods=["GET"]),
            Route("/api/apps/{token}/current", set_current, methods=["POST"]),
            Route(
                "/api/apps/{token}/description",
                set_app_description,
                methods=["POST"],
            ),
            Route(
                "/api/apps/{token}/versions/{version}/branch",
                branch_version,
                methods=["POST"],
            ),
            Route(
                "/api/apps/{token}/versions/{version}",
                delete_version,
                methods=["DELETE"],
            ),
            Route("/api/apps/{token}", unpublish, methods=["DELETE"]),
            # after every real /api route: absolute-path fetches from
            # preview'd apps get a CORS-readable teaching 404
            Route("/api/{path:path}", api_fallback, methods=preview_verbs),
            Route("/preview/{name}", preview, methods=preview_verbs),
            Route("/preview/{name}/{path:path}", preview, methods=preview_verbs),
            # frozen snapshots: read-only, concurrent, token-addressed.
            # config= is the SAME object the sessions were built with —
            # serving a published app under a different declaration than
            # test_app verified it under is the one failure verification
            # cannot catch. NONTAINER_STUDIO_CSP overrides the derived
            # policy ("none" disables it entirely).
            Mount(
                "/apps",
                cors_for_apps(
                    holding_snapshots(
                        build_router(_resolve_held(registry), config=registry.apps),
                        registry,
                    )
                ),
            ),
            Mount("/static", StaticFiles(directory=STATIC)),
        ],
        lifespan=lifespan,
    )


def _load_dotenv() -> None:
    """A `.env` beside where you launched from (KEY=VALUE lines, # for
    comments) — real env always wins. Keeps 'my studio defaults to
    openrouter' out of your shell profile."""
    env_file = Path.cwd() / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    import uvicorn

    from . import providers

    _load_dotenv()
    registry = Registry(
        model_factory=providers.build_model,
        store=os.getenv("NONTAINER_STUDIO_STORE"),
        # resolves the env NOW: fails fast with a helpful message when
        # no provider key is present
        default_model=providers.default_spec(),
    )
    # dud-vm: park warm VM(s) in the background so the first session
    # switch after startup doesn't pay a boot (no-op on other executors).
    from .config import start_vm_prewarm

    start_vm_prewarm()
    port = int(os.getenv("NONTAINER_STUDIO_PORT", "8321"))
    # A non-default executor changes the server's security posture —
    # say so where the operator is already looking (the URL line).
    executor = os.getenv("NONTAINER_STUDIO_EXECUTOR", "").lower()
    if executor == "dud":
        print(
            "⚠ executor=dud: agent code runs UNSANDBOXED as your user "
            "(real bash/python, open egress) — own-machine posture only"
        )
    elif executor == "dud-vm":
        print(
            "executor=dud-vm: disposable microVMs, budget "
            f"DUD_VM_MAX_TOTAL={os.environ.get('DUD_VM_MAX_TOTAL', '4')}"
        )
    print(f"nontainer-studio → http://127.0.0.1:{port}")
    uvicorn.run(
        build_app(registry),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        # The transcript SSE streams never end by design, and uvicorn's
        # graceful shutdown waits for active requests indefinitely —
        # without a deadline, Ctrl-C hangs on any open browser tab.
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()
