"""Where everything was when a test gave up waiting.

A wait that times out says only that it timed out. On CI, where a hang
can't be poked at, that is not enough to tell a thread stuck in a call
from a loop blocked in one or a task waiting on something that never
comes. ``report`` prints every thread's stack and every task's on the
event loops that are running, to stderr, which pytest shows beside the
failure.
"""

import asyncio
import concurrent.futures
import gc
import io
import sys
import threading
import traceback


def _threads() -> str:
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():
        stack = "".join(traceback.format_stack(frame))
        out.append(f"--- thread {names.get(ident, ident)}\n{stack}")
    return "\n".join(out)


def _tasks(loop: asyncio.AbstractEventLoop) -> str:
    """The loop's tasks' stacks, read on the loop; a loop that doesn't
    answer is blocked, which its thread's stack above shows."""
    answer: concurrent.futures.Future[str] = concurrent.futures.Future()

    def collect() -> None:
        out = []
        for task in asyncio.all_tasks(loop):
            buf = io.StringIO()
            task.print_stack(file=buf)
            out.append(
                f"--- task {task.get_name()}: {task.get_coro()!r}\n{buf.getvalue()}"
            )
        answer.set_result("\n".join(out))

    try:
        loop.call_soon_threadsafe(collect)
        return answer.result(timeout=2)
    except concurrent.futures.TimeoutError:
        return "(the loop did not answer in 2s: it is blocked)"
    except RuntimeError as e:  # closed meanwhile
        return f"({e})"


def _sandboxes() -> str:
    """Every sandbox a local executor is running code in: its worker's
    pid and, on Linux, the worker's state and signal masks, which say
    whether an interrupt (SIGUSR1) can reach it."""
    from nontainer.executor import LocalExecutor

    out = []
    for executor in gc.get_objects():
        if not isinstance(executor, LocalExecutor):
            continue
        for sb in list(executor._running.values()):
            process = getattr(sb, "_process", None)
            pid = getattr(process, "pid", None)
            line = f"--- {type(sb).__name__} worker pid={pid}"
            if process is not None:
                line += f" alive={process.is_alive()}"
            try:
                with open(f"/proc/{pid}/status") as f:
                    keep = ("State", "SigBlk", "SigIgn", "SigCgt", "SigPnd", "ShdPnd")
                    line += "\n" + "".join(
                        row for row in f if row.split(":")[0] in keep
                    )
            except OSError:
                pass
            out.append(line)
    return "\n".join(out) or "(none running)"


def report(why: str) -> None:
    loops = [
        o
        for o in gc.get_objects()
        if isinstance(o, asyncio.AbstractEventLoop) and o.is_running()
    ]
    parts = [f"===== stuck: {why}", _threads()]
    for loop in loops:
        parts.append(f"===== tasks on {loop!r}\n{_tasks(loop)}")
    parts.append(f"===== sandboxes running code\n{_sandboxes()}")
    print("\n\n".join(parts), file=sys.stderr, flush=True)
