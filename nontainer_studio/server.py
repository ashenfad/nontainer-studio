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
from typing import Any, AsyncIterator

import anyio

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from nontainer.apps import build_router, request as make_request
from nontainer.apps.contract import filter_headers
from nontainer.errors import SessionIdError

from .sessions import Registry, repair_aborted_run

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 50_000_000  # upload bodies buffer in memory; cap them


# ---------------------------------------------------------------------------
# agno stream -> client events (a small, defensive mapping: unknown
# event types are skipped so agno upgrades degrade gracefully)
# ---------------------------------------------------------------------------


def _short(value: Any, limit: int = 2_000) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def _client_event(ev: Any) -> dict | None:
    kind = getattr(ev, "event", "")
    if kind == "RunContent":
        delta = getattr(ev, "content", None)
        if isinstance(delta, str) and delta:
            return {"type": "text", "delta": delta}
        return None
    if kind == "ToolCallStarted":
        tool = getattr(ev, "tool", None)
        return {
            "type": "tool_start",
            "name": getattr(tool, "tool_name", "?"),
            "args": _short(getattr(tool, "tool_args", "")),
        }
    if kind == "ToolCallCompleted":
        tool = getattr(ev, "tool", None)
        return {
            "type": "tool_end",
            "name": getattr(tool, "tool_name", "?"),
            "result": _short(getattr(tool, "result", "")),
        }
    if kind == "RunError":
        return {"type": "error", "message": _short(getattr(ev, "content", "run error"))}
    return None


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _run_turn(session: Any, message: str) -> None:
    """One agent turn, as a server-side task DECOUPLED from any HTTP
    request: events land in the session's buffer, subscribers follow
    from a cursor. Disconnects, reloads, and session switches never
    abort work. Caller holds the turn lock; released here."""
    run_id = None
    try:
        # head here = the workspace BEFORE this turn: the user event's
        # stamp is the undo anchor (restore to it = unwind this turn)
        await session.emit(
            {"type": "user", "text": message, "head": session.ws.head}
        )
        async for ev in session.agent.arun(message, stream=True, stream_events=True):
            run_id = getattr(ev, "run_id", None) or run_id
            payload = _client_event(ev)
            if payload is not None:
                await session.emit(payload)
    except Exception as e:
        await session.emit({"type": "error", "message": _short(str(e))})
        # agno stamps the stored run status=error, and its history
        # builder skips error runs — repair it so the turn's real work
        # stays in the agent's memory (see repair_aborted_run).
        await asyncio.to_thread(
            repair_aborted_run, session, run_id, _short(str(e), 300)
        )
    finally:
        # done BEFORE the lock releases: the buffer is the permanent
        # source of truth (replays reconstruct it forever), so a next
        # turn's `user` event must never precede this turn's `done`.
        # It carries the turn's agno run_id and the workspace head at
        # turn end — the checkpoint <-> conversation mapping that lets
        # restore rewind the agent's memory in sync with the files.
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
        session endpoint starts the same way, so say it once."""

        async def wrapped(request: Any) -> Any:
            session = registry.get(request.path_params["name"])
            if session is None:
                name = request.path_params["name"]
                return JSONResponse({"error": f"no session {name!r}"}, status_code=404)
            return await handler(request, session)

        return wrapped

    async def index(request: Any) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    async def list_sessions(request: Any) -> JSONResponse:
        return JSONResponse({"sessions": registry.list()})

    async def open_session(request: Any) -> JSONResponse:
        body = await request.json()
        name = (body.get("name") or "").strip()
        try:
            registry.open(name)
        except SessionIdError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "name": name})

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
        return JSONResponse({"ok": True, "since": len(session.events)})

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
                {"events": session.events[since:], "next": len(session.events)}
            )

        async def stream() -> AsyncIterator[str]:
            async for cursor, event in session.follow(since):
                yield _sse({"cursor": cursor, **event})

        return StreamingResponse(stream(), media_type="text/event-stream")

    # -- upload: browser file -> workspace write ---------------------------
    # Raw body, not multipart (the browser sends File bytes natively).
    # Each upload is a write_file: checkpointed, so restore/fork extend
    # to uploads for free. Multi-file drops are N parallel requests —
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
        dest = f"/uploads/{filename}"
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

    # -- time travel ------------------------------------------------------

    @with_session
    async def history(request: Any, session: Any) -> JSONResponse:
        entries = [
            {"id": c.id, "time": c.time, "info": c.info}
            for c in session.ws.history(limit=100)
        ]
        return JSONResponse({"history": entries, "head": session.ws.head})

    @with_session
    async def restore(request: Any, session: Any) -> JSONResponse:
        if session.busy:
            return JSONResponse(
                {"error": "can't restore while a turn is running"}, status_code=409
            )
        checkpoint = (await request.json()).get("checkpoint") or ""
        try:
            await anyio.to_thread.run_sync(registry.restore, session, checkpoint)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await session.emit(
            {
                "type": "notice",
                "text": f"restored files AND agent memory to {checkpoint[:8]} "
                "(the app db and this transcript keep their history)",
            }
        )
        return JSONResponse({"ok": True})

    async def fork(request: Any) -> JSONResponse:
        name = request.path_params["name"]
        body = await request.json()
        new_name = (body.get("name") or "").strip()
        try:
            registry.fork(name, new_name, checkpoint=body.get("checkpoint"))
        except KeyError:
            return JSONResponse({"error": f"no session {name!r}"}, status_code=404)
        except (SessionIdError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "name": new_name})

    # -- live preview: dispatch into the AUTHORING runtime ---------------
    # Mutable (unlike frozen /apps serving): the iframe shows the app as
    # the agent builds it. Dispatch holds the workspace's single-writer
    # lock, so preview requests serialize safely with agent turns.

    @with_session
    async def preview(request: Any, session: Any) -> Any:
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

    # -- publish: freeze a snapshot behind a capability token ------------

    @with_session
    async def publish(request: Any, session: Any) -> JSONResponse:
        token, commit = registry.publish(session.name)
        return JSONResponse(
            {"token": token, "url": f"/apps/{token}/", "checkpoint": commit}
        )

    @asynccontextmanager
    async def lifespan(app: Any):
        try:
            yield
        finally:
            registry.close()

    verbs = ["GET", "POST", "PUT", "DELETE", "PATCH"]
    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/sessions", list_sessions, methods=["GET"]),
            Route("/api/sessions", open_session, methods=["POST"]),
            Route("/api/sessions/{name}/chat", chat, methods=["POST"]),
            Route("/api/sessions/{name}/events", events, methods=["GET"]),
            Route("/api/sessions/{name}/upload", upload, methods=["POST"]),
            Route("/api/sessions/{name}/files", files, methods=["GET"]),
            Route("/api/sessions/{name}/file", file_raw, methods=["GET"]),
            Route("/api/sessions/{name}/history", history, methods=["GET"]),
            Route("/api/sessions/{name}/restore", restore, methods=["POST"]),
            Route("/api/sessions/{name}/fork", fork, methods=["POST"]),
            Route("/api/sessions/{name}/publish", publish, methods=["POST"]),
            Route("/preview/{name}", preview, methods=verbs),
            Route("/preview/{name}/{path:path}", preview, methods=verbs),
            # frozen snapshots: read-only, concurrent, token-addressed.
            # NONTAINER_STUDIO_CSP overrides the default policy
            # ("none" disables it entirely).
            Mount("/apps", build_router(registry.resolve, **_csp_kwargs())),
            Mount("/static", StaticFiles(directory=STATIC)),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    import uvicorn

    from .model import pick_model

    registry = Registry(
        model_factory=pick_model, store=os.getenv("NONTAINER_STUDIO_STORE")
    )
    port = int(os.getenv("NONTAINER_STUDIO_PORT", "8321"))
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
