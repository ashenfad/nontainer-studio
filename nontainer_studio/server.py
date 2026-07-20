"""nontainer-studio server: a chat UI over agno + nontainer with
versioned sessions. Single-user, localhost, no auth — a workbench,
not a deployment target.

Run:  ANTHROPIC_API_KEY=... nontainer-studio
      (or: uv run nontainer-studio, or python -m nontainer_studio)
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote

import anyio
from nontainer.adapters.a2ui import turn_to_a2ui
from nontainer.adapters.render import artifact_kind, parse_artifacts_note
from nontainer.apps import build_router
from nontainer.apps import request as make_request
from nontainer.apps.contract import filter_headers
from nontainer.errors import SessionIdError
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .sessions import Registry, _clean_title, repair_aborted_run

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 50_000_000  # upload bodies buffer in memory; cap them


# ---------------------------------------------------------------------------
# agno stream -> client events (a small, defensive mapping: unknown
# event types are skipped so agno upgrades degrade gracefully)
# ---------------------------------------------------------------------------


def _short(value: Any, limit: int = 2_000) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def _short_middle(value: Any, limit: int = 2_000) -> str:
    """Cap by cutting the MIDDLE — for tracebacks, where the last line
    (the exception) is the one that matters."""
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return text[:head] + "\n…[truncated]…\n" + text[-tail:]


def _tool_args(tool: Any) -> Any:
    """Structured args when possible (the client renders tool calls
    per-type: highlighted code, file diffs, terminal commands), a
    capped string otherwise. Values are capped generously — file
    contents ARE the rendering."""
    args = getattr(tool, "tool_args", None)
    if isinstance(args, dict):
        shaped = {
            k: _short(v, 16_000) if isinstance(v, str) else v for k, v in args.items()
        }
        try:
            json.dumps(shaped)
            return shaped
        except (TypeError, ValueError):
            pass
    return _short(args if args is not None else "")


def _client_events(ev: Any) -> list[dict]:
    kind = getattr(ev, "event", "")
    if kind == "RunContent":
        # native model thinking rides RunContent as a per-chunk
        # reasoning_content delta (Claude thinking, OpenRouter
        # reasoning, Gemini thoughts, Responses summaries) — a chunk
        # can carry thinking, prose, or both
        out = []
        think = getattr(ev, "reasoning_content", None)
        if isinstance(think, str) and think:
            out.append({"type": "thinking", "delta": think})
        delta = getattr(ev, "content", None)
        if isinstance(delta, str) and delta:
            out.append({"type": "text", "delta": delta})
        return out
    if kind == "ReasoningContentDelta":
        # agno's reasoning-manager stream (reasoning=True agents) —
        # same client treatment as native thinking
        think = getattr(ev, "reasoning_content", None)
        if isinstance(think, str) and think:
            return [{"type": "thinking", "delta": think}]
        return []
    if kind == "ToolCallStarted":
        tool = getattr(ev, "tool", None)
        return [
            {
                "type": "tool_start",
                "name": getattr(tool, "tool_name", "?"),
                "args": _tool_args(tool),
            }
        ]
    if kind == "ToolCallCompleted":
        tool = getattr(ev, "tool", None)
        result = getattr(tool, "result", "")
        events: list[dict] = [
            {
                "type": "tool_end",
                "name": getattr(tool, "tool_name", "?"),
                "result": _short(result),
            }
        ]
        # First-class artifact events: parse the RAW (uncapped) result —
        # the `[ui artifacts: ...]` note rides at the string's tail, so a
        # long tool result would truncate it away if we parsed _short().
        # The note stays inside tool_end.result (the model-facing
        # affordance); these events are additive, and the client dedupes
        # by path against its legacy note-regex fallback.
        if isinstance(result, str):
            for name, path in parse_artifacts_note(result):
                events.append(
                    {
                        "type": "artifact",
                        "name": name,
                        "path": path,
                        "kind": artifact_kind(path),
                    }
                )
        # The title the agent just gave, as a first-class event. The TOOL
        # already wrote it to the manifest (it can't emit — it's sync code
        # inside agno's run, and emit is loop-bound), so this is not the
        # write path: it is the temporal record, which buys two things the
        # manifest can't. It marks WHEN the session got that name, so an
        # edit's rewind can put the title back the way the conversation
        # was; and it lets the shell relabel now instead of on the next
        # rail poll. Carries the agent's SUGGESTION (clamped as stored) —
        # a human title may outrank it, so the client re-reads the
        # resolved label rather than trusting this text.
        if getattr(tool, "tool_name", None) == "recommend_title":
            args = _tool_args(tool)
            asked = args.get("title") if isinstance(args, dict) else None
            titled = _clean_title(asked)
            if titled:
                events.append({"type": "title", "title": titled})
        return events
    if kind == "RunCancelled":
        return [{"type": "notice", "text": "turn stopped"}]
    if kind == "CompressionStarted":
        return [
            {
                "type": "notice",
                "text": "context high-water mark — compressing older tool results",
            }
        ]
    if kind == "CompressionCompleted":
        n = getattr(ev, "tool_results_compressed", None)
        orig = getattr(ev, "original_size", None)
        comp = getattr(ev, "compressed_size", None)
        detail = f"{n} tool results" if n else "tool results"
        if orig and comp:
            detail += f" ({orig:,} → {comp:,} chars)"
        return [{"type": "notice", "text": f"compressed {detail}"}]
    if kind == "ModelRequestCompleted":
        # context-usage telemetry for the UI (one per model call; the
        # frontend keeps only the latest)
        tokens = getattr(ev, "input_tokens", None)
        if tokens:
            return [
                {
                    "type": "usage",
                    "input_tokens": tokens,
                    "cached_tokens": getattr(ev, "cache_read_tokens", None) or 0,
                }
            ]
        return []
    if kind == "RunError":
        return [
            {
                "type": "error",
                "message": _short_middle(getattr(ev, "content", "run error")),
            }
        ]
    return []


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


async def _run_turn(session: Any, message: str) -> None:
    """One agent turn, as a server-side task DECOUPLED from any HTTP
    request: events land in the session's buffer, subscribers follow
    from a cursor. Disconnects, reloads, and session switches never
    abort work. Caller holds the turn lock; released here."""
    run_id = None
    cancelled = False
    errored = None
    try:
        # head here = the workspace BEFORE this turn: the user event's
        # stamp is the undo anchor (restore to it = unwind this turn)
        await session.emit({"type": "user", "text": message, "head": session.ws.head})
        async for ev in session.agent.arun(message, stream=True, stream_events=True):
            run_id = getattr(ev, "run_id", None) or run_id
            session.run_id = run_id  # the stop button's cancel handle
            kind = getattr(ev, "event", "")
            cancelled = cancelled or kind == "RunCancelled"
            if kind == "RunError":
                # provider failure after agno's retries are exhausted:
                # the stream ends CLEANLY (no exception), so this flag
                # is the only signal the turn died
                errored = getattr(ev, "content", None) or "provider error"
            for payload in _client_events(ev):
                await session.emit(payload)
        if cancelled:
            # agno stores the run status=cancelled and its history
            # builder skips those — repair keeps the partial work in
            # the agent's memory (see repair_aborted_run)
            await asyncio.to_thread(
                repair_aborted_run, session, run_id, "stopped by the user"
            )
        elif errored is not None:
            # same skip-on-replay problem for status=error runs — the
            # equal-grouse amnesia: without repair, "please continue"
            # replans from scratch while the workspace holds the work
            await asyncio.to_thread(
                repair_aborted_run, session, run_id, _short_middle(str(errored), 300)
            )
    except Exception as e:
        await session.emit({"type": "error", "message": _short_middle(str(e))})
        # agno stamps the stored run status=error, and its history
        # builder skips error runs — repair it so the turn's real work
        # stays in the agent's memory (see repair_aborted_run).
        await asyncio.to_thread(
            repair_aborted_run, session, run_id, _short_middle(str(e), 300)
        )
    finally:
        # done BEFORE the lock releases: the buffer is the permanent
        # source of truth (replays reconstruct it forever), so a next
        # turn's `user` event must never precede this turn's `done`.
        # It carries the turn's agno run_id and the workspace head at
        # turn end — the checkpoint <-> conversation mapping that lets
        # restore rewind the agent's memory in sync with the files.
        session.run_id = None
        await session.emit({"type": "done", "run_id": run_id, "head": session.ws.head})
        session.turn_lock.release()


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


def _csp_kwargs() -> dict:
    csp = os.getenv("NONTAINER_STUDIO_CSP")
    if csp is None:
        return {}  # library default
    return {"csp": None if csp.lower() == "none" else csp}


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
                session = await anyio.to_thread.run_sync(registry.open, name)
            if session is None:
                return JSONResponse({"error": f"no session {name!r}"}, status_code=404)
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

    @with_session
    async def chat(request: Any, session: Any) -> Any:
        body = await request.json()
        message = (body.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "empty message"}, status_code=400)
        if not session.turn_lock.acquire(blocking=False):
            return JSONResponse({"error": "a turn is already running"}, status_code=409)
        # keep a strong reference: the loop holds tasks weakly, and a
        # GC'd task is a silently dead turn with a stuck lock
        session.turn_task = asyncio.create_task(_run_turn(session, message))
        return JSONResponse({"ok": True, "since": session.next_seq})

    @with_session
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
        try:
            await anyio.to_thread.run_sync(registry.rewind_to_event, session, seq)
        except Exception as e:
            session.turn_lock.release()
            return JSONResponse({"error": str(e)}, status_code=400)
        await session.emit({"type": "truncate", "to": seq})
        session.turn_task = asyncio.create_task(_run_turn(session, message))
        return JSONResponse({"ok": True, "since": session.next_seq})

    @with_session
    async def cancel(request: Any, session: Any) -> Any:
        """Stop the running turn GRACEFULLY: agno's cancel-by-run-id
        raises at the loop's next checkpoint (a mid-flight tool call
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
                    return session.ws.fs.read(path)
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
    # Each upload is a write_file: checkpointed, so an edit's rewind
    # extends to uploads for free. Multi-file drops are N parallel requests —
    # the workspace lock serializes them safely. Same-name uploads
    # overwrite (idempotent re-drops).

    @with_session
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
        out = await anyio.to_thread.run_sync(session.ws.write_file, dest, data)
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
                    for entry in sorted(session.ws.fs.list(d)):
                        full = f"{d.rstrip('/')}/{entry}"
                        if session.ws.fs.isdir(full):
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
                return session.ws.fs.read(path)

        try:
            data = await anyio.to_thread.run_sync(read)
        except Exception:
            return JSONResponse({"error": f"cannot read {path!r}"}, status_code=404)
        media, _ = mimetypes.guess_type(path)
        return Response(data, media_type=media or "application/octet-stream")

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
                return bool(session.ws.fs.isdir(f"{session.ws.root}/app"))

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

    @with_session
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

    # -- publish: freeze a snapshot behind a capability token ------------

    @with_session
    async def publish(request: Any, session: Any) -> JSONResponse:
        token, commit = registry.publish(session.name)
        return JSONResponse(
            {"token": token, "url": f"/apps/{token}/", "checkpoint": commit}
        )

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
        try:
            yield
        finally:
            registry.close()

    verbs = ["GET", "POST", "PUT", "DELETE", "PATCH"]
    preview_verbs = verbs + ["OPTIONS"]
    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/models", list_models, methods=["GET"]),
            Route("/api/sessions", list_sessions, methods=["GET"]),
            Route("/api/sessions", open_session, methods=["POST"]),
            Route("/api/sessions/{name}/model", set_model, methods=["POST"]),
            Route("/api/sessions/{name}/title", set_title, methods=["POST"]),
            Route("/api/sessions/{name}", delete_session, methods=["DELETE"]),
            Route("/api/sessions/{name}/chat", chat, methods=["POST"]),
            Route("/api/sessions/{name}/edit", edit, methods=["POST"]),
            Route("/api/sessions/{name}/cancel", cancel, methods=["POST"]),
            Route("/api/sessions/{name}/events", events, methods=["GET"]),
            Route("/api/sessions/{name}/a2ui", a2ui, methods=["GET"]),
            Route("/api/sessions/{name}/upload", upload, methods=["POST"]),
            Route("/api/sessions/{name}/files", files, methods=["GET"]),
            Route("/api/sessions/{name}/app", app_exists, methods=["GET"]),
            Route("/api/sessions/{name}/file", file_raw, methods=["GET"]),
            Route("/api/sessions/{name}/publish", publish, methods=["POST"]),
            # after every real /api route: absolute-path fetches from
            # preview'd apps get a CORS-readable teaching 404
            Route("/api/{path:path}", api_fallback, methods=preview_verbs),
            Route("/preview/{name}", preview, methods=preview_verbs),
            Route("/preview/{name}/{path:path}", preview, methods=preview_verbs),
            # frozen snapshots: read-only, concurrent, token-addressed.
            # NONTAINER_STUDIO_CSP overrides the default policy
            # ("none" disables it entirely).
            Mount("/apps", build_router(registry.resolve, **_csp_kwargs())),
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
    from .sessions import start_vm_prewarm

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
