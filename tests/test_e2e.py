"""Browser E2E: a real server + a real browser + a scripted LLM.

The whole stack runs for real — uvicorn, SSE, the Svelte bundle, agno's
run loop, WorkspaceTools, the workspace — except the model, which is
the DummyModel (NONTAINER_STUDIO_MODEL=dummy) scripted by !tool / !text
directives embedded in the messages the tests type.

Needs the committed frontend build and playwright's chromium
(`playwright install chromium`); both skip cleanly when absent.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "nontainer_studio" / "static"

pytestmark = pytest.mark.skipif(
    not (STATIC / "index.html").exists(),
    reason="frontend not built (cd frontend && npm run build)",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    env = {
        **os.environ,
        "NONTAINER_STUDIO_MODEL": "dummy",
        "NONTAINER_STUDIO_PORT": str(port),
        "NONTAINER_STUDIO_STORE": str(tmp_path_factory.mktemp("store")),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "nontainer_studio"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.15)
    else:
        proc.terminate()
        raise RuntimeError("server did not come up")
    yield base
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except PlaywrightError:
            pytest.skip("chromium not installed (playwright install chromium)")
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    page = browser.new_page()
    yield page
    page.close()


def _send(page, message: str) -> None:
    page.fill("textarea", message)
    page.get_by_role("button", name="send").click()


# ---------------------------------------------------------------------------


def test_turn_streams_into_transcript(page, server):
    page.goto(f"{server}/?session=e2e-chat")
    _send(
        page,
        '!tool file_write {"path": "/notes.md", "content": "hello"}\n'
        "!text Wrote your note.",
    )
    # activity chip for the real tool call, then the streamed reply
    expect(page.locator(".chip", has_text="file_write")).to_be_visible(timeout=15000)
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Wrote your note.", timeout=15000
    )
    # chip expands to the timeline with the REAL tool result
    page.locator(".chip", has_text="file_write").click()
    expect(page.locator(".timeline")).to_contain_text("wrote /notes.md")

    # the files tab lists the real workspace write
    page.get_by_role("button", name="files", exact=True).click()
    expect(page.locator(".file", has_text="/notes.md")).to_be_visible(timeout=5000)
    page.locator(".file", has_text="/notes.md").click()
    expect(page.locator(".view pre")).to_contain_text("hello")


def test_undo_rewinds_files_and_shows_notice(page, server):
    page.goto(f"{server}/?session=e2e-undo")
    _send(page, '!tool file_write {"path": "/a.txt", "content": "A"}\n!text one done')
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "one done", timeout=15000
    )
    _send(page, '!tool file_write {"path": "/b.txt", "content": "B"}\n!text two done')
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "two done", timeout=15000
    )

    # undo the SECOND turn: hover its user row, click undo
    rows = page.locator(".user-row")
    rows.last.hover()
    rows.last.locator(".undo").click()

    # restore notice lands in the transcript (files + memory rewound)
    expect(page.locator(".notice", has_text="restored files AND agent memory")).to_be_visible(
        timeout=10000
    )

    # files tab: a.txt survives, b.txt is gone
    page.get_by_role("button", name="files", exact=True).click()
    expect(page.locator(".file", has_text="/a.txt")).to_be_visible(timeout=5000)
    expect(page.locator(".file", has_text="/b.txt")).to_have_count(0)


def test_preview_serves_the_agents_app(page, server):
    page.goto(f"{server}/?session=e2e-app")
    _send(
        page,
        '!tool file_write {"path": "/app/index.html", "content": '
        '"<html><body><h1 id=marker>hi from the app</h1></body></html>"}\n'
        "!text App is up.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "App is up.", timeout=15000
    )
    # preview tab renders the live app in the sandboxed iframe
    frame = page.frame_locator("iframe[title='app preview']")
    expect(frame.locator("#marker")).to_have_text("hi from the app", timeout=15000)


def test_background_turn_survives_session_switch(page, server):
    page.goto(f"{server}/?session=e2e-bg1")
    _send(page, "!text first session reply")
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "first session reply", timeout=15000
    )
    # switch away via the rail's new-session box, then back
    page.fill(".new input", "e2e-bg2")
    page.press(".new input", "Enter")
    expect(page.locator(".item.active", has_text="e2e-bg2")).to_be_visible(
        timeout=10000
    )
    page.locator(".item", has_text="e2e-bg1").click()
    # the transcript replays from the server-side event log
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "first session reply", timeout=10000
    )
