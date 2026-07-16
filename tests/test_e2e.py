"""Browser E2E: a real server + a real browser + a scripted LLM.

The whole stack runs for real — uvicorn, SSE, the Svelte bundle, agno's
run loop, WorkspaceTools, the workspace — except the model, which is
the DummyModel (NONTAINER_STUDIO_MODEL=dummy) scripted by !tool / !text
directives embedded in the messages the tests type.

Needs the committed frontend build and playwright's chromium
(`playwright install chromium`); both skip cleanly when absent.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
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


def _title(server: str, name: str, title: str) -> None:
    """Name a session so its rail row is findable. Rows are labelled by
    TITLE now — identity is a slug that never displays — so two untitled
    sessions both read "New session" and can't be told apart by text.

    Deliberately NOT page.request: that rides the browser's network
    stack, where the SSE followers pin connections against Chromium's
    per-origin cap and this POST can starve. It only arranges server
    state, so it talks to the server directly."""
    req = urllib.request.Request(
        f"{server}/api/sessions/{name}/title",
        data=json.dumps({"title": title}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200


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

    # the files tab lists the real workspace write; clicking opens the
    # shared file modal with a RENDERED view (markdown, not raw text)
    page.get_by_role("button", name="files", exact=True).click()
    expect(page.locator(".file", has_text="/notes.md")).to_be_visible(timeout=5000)
    page.locator(".file", has_text="/notes.md").click()
    expect(page.locator(".modal .markdown")).to_contain_text("hello", timeout=5000)
    page.keyboard.press("Escape")
    expect(page.locator(".modal")).to_have_count(0)


def test_ui_artifact_renders_from_server_event(page, server):
    """The full artifact path: run_python assigns `ui`, WorkspaceTools
    materializes it into /ui and appends the `[ui artifacts: ...]` note,
    the server harvests that into a first-class `artifact` event, and
    the shell renders it (here the json floor as a details block). Prose
    doesn't reference the path, so the done-time Jupyter rule appends it
    after the reply."""
    page.goto(f"{server}/?session=e2e-artifact")
    _send(
        page,
        "!tool run_python {\"code\": \"ui = {'stats': {'hello': 'world'}}\"}\n"
        "!text Made an artifact.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Made an artifact.", timeout=15000
    )
    # the artifact rendered inline (json floor -> a details block named
    # for the binding), NOT merely the raw note in the tool timeline
    artifact = page.locator(".agent-msg .artifact-text")
    expect(artifact).to_be_visible(timeout=10000)
    expect(artifact.locator("summary")).to_contain_text("stats")
    expect(artifact).to_contain_text("hello")


def test_cards_artifact_renders_stat_and_callout(page, server):
    """A `ui` value that is a list of card dicts materializes into
    /ui/*.cards.json, and the shell renders a mixed row: a stat tile
    (muted label, prominent value, muted sublabel) and a callout card
    (tone-tinted icon + title + markdown body) — not raw JSON. Prose
    doesn't name the path, so the done-time rule appends it. Sentiment is
    never inferred from a value's sign — hence no delta accent classes."""
    page.goto(f"{server}/?session=e2e-cards")
    _send(
        page,
        '!tool run_python {"code": "ui = {\'kpis\': ['
        "{'label': 'Revenue', 'value': 1284, 'sublabel': 'up 3.2% MoM'}, "
        "{'type': 'callout', 'title': 'Churn rising', "
        "'body': 'Cancellations up **12%** this week.', 'tone': 'warning'}"
        ']}"}\n'
        "!text Here are the numbers.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Here are the numbers.", timeout=15000
    )
    cards = page.locator(".agent-msg .cards")
    expect(cards).to_be_visible(timeout=10000)

    # stat tile: label + comma-formatted value + muted sublabel
    tile = cards.locator(".tile")
    expect(tile).to_have_count(1)
    expect(tile.locator(".label")).to_have_text("Revenue")
    expect(tile.locator(".value")).to_contain_text("1,284")
    expect(tile.locator(".sublabel")).to_contain_text("up 3.2% MoM")

    # callout card: warning tone tints the icon; title + markdown body render
    callout = cards.locator(".callout.tone-warning")
    expect(callout).to_have_count(1)
    expect(callout.locator(".callout-title")).to_have_text("Churn rising")
    expect(callout.locator(".callout-body")).to_contain_text(
        "Cancellations up 12% this week."
    )
    expect(callout.locator(".callout-body strong")).to_contain_text("12%")

    # sign inference is gone: no delta accent classes exist anymore
    expect(cards.locator(".delta")).to_have_count(0)


def test_missing_artifact_shows_error_not_silence(page, server):
    """A prose ref to an artifact file that doesn't exist (the
    rewound-artifact case): file_raw 404s with a JSON error body, which
    r.json() would happily parse — the r.ok check must surface the muted
    error line instead of silently rendering nothing (PR #3 review)."""
    page.goto(f"{server}/?session=e2e-gone")
    _send(page, "!text See ![gone](/ui/gone.cards.json) for the numbers.")
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "for the numbers.", timeout=15000
    )
    expect(page.locator(".cards-error")).to_contain_text("HTTP 404")


def test_stop_button_cancels_the_turn(page, server):
    """The send button morphs to stop while busy; clicking it cancels
    via agno (real cancel machinery — only the model is scripted), the
    'turn stopped' notice lands, and the scripted reply never does."""
    page.goto(f"{server}/?session=e2e-stop")
    _send(
        page,
        '!tool run_python {"code": "import time\\ntime.sleep(8)"}\n'
        "!text finished anyway",
    )
    stop = page.locator(".send-btn.stop")
    expect(stop).to_be_visible(timeout=5000)
    stop.click()
    expect(page.locator(".notice", has_text="turn stopped")).to_be_visible(
        timeout=20000
    )
    expect(
        page.locator(".agent-msg .bubble", has_text="finished anyway")
    ).to_have_count(0)
    # composer back to send: the session is usable again
    expect(page.locator(".send-btn.stop")).to_have_count(0, timeout=10000)


def test_long_tool_lines_scroll_instead_of_widening_layout(page, server):
    """A single long unwrapped line in a tool block must scroll inside
    its pre — not inflate the chat pane and squeeze the preview away
    (flex min-width:auto regression)."""
    page.goto(f"{server}/?session=e2e-wide")
    long_cmd = "echo " + " ".join(f"--flag-{i}=value" for i in range(80))
    _send(page, f'!tool terminal {{"command": "{long_cmd}"}}\n!text ran it')
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "ran it", timeout=15000
    )
    page.locator(".chip", has_text="terminal").click()
    expect(page.locator(".timeline")).to_be_visible()
    metrics = page.evaluate(
        """() => ({
            doc: document.documentElement.scrollWidth,
            win: window.innerWidth,
            pre: document.querySelector('.timeline pre.block').scrollWidth,
            preBox: document.querySelector('.timeline pre.block').clientWidth,
        })"""
    )
    assert metrics["doc"] <= metrics["win"], f"layout widened: {metrics}"
    assert metrics["pre"] > metrics["preBox"], f"pre should scroll: {metrics}"


def test_thinking_interleaves_into_the_work_chip(page, server):
    """Thinking around tool calls folds INTO the activity chip (the
    think -> act narrative lives in the drill-down); a tool-free
    thought keeps its standalone toggle block."""
    page.goto(f"{server}/?session=e2e-think")
    _send(
        page,
        "!think Considering the request carefully.\n"
        '!tool file_write {"path": "/t.txt", "content": "x"}\n'
        "!text Done pondering.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Done pondering.", timeout=15000
    )
    # no standalone thinking block: it joined the work group
    expect(page.locator(".think-toggle")).to_have_count(0)
    page.locator(".chip", has_text="file_write").click()
    timeline = page.locator(".timeline")
    expect(timeline).to_contain_text("thinking")
    expect(timeline).to_contain_text("Considering the request carefully.")

    # a pure thought (no tools) stays a standalone foldable block
    _send(page, "!think Just musing, no tools.\n!text Mused.")
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Mused.", timeout=15000
    )
    toggle = page.locator(".think-toggle").first
    expect(toggle).to_be_visible()
    toggle.click()
    expect(page.locator(".think-text").last).to_contain_text("Just musing")


def test_edit_rewinds_files_and_truncates_transcript(page, server):
    page.goto(f"{server}/?session=e2e-edit")
    _send(page, '!tool file_write {"path": "/a.txt", "content": "A"}\n!text one done')
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "one done", timeout=15000
    )
    _send(page, '!tool file_write {"path": "/b.txt", "content": "B"}\n!text two done')
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "two done", timeout=15000
    )

    # edit the SECOND prompt: hover its user row, click edit, replace it
    rows = page.locator(".user-row")
    rows.last.hover()
    rows.last.locator(".edit").click()
    box = page.locator(".edit-box textarea")
    box.fill('!tool file_write {"path": "/c.txt", "content": "C"}\n!text two revised')
    page.locator(".edit-actions .send").click()

    # the old turn is gone from the transcript; the edited turn replaces it
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "two revised", timeout=15000
    )
    expect(page.locator(".agent-msg .bubble", has_text="two done")).to_have_count(0)
    expect(page.locator(".user-row")).to_have_count(2)

    # files tab: a.txt survives, b.txt rewound away, c.txt from the redo
    page.get_by_role("button", name="files", exact=True).click()
    expect(page.locator(".file", has_text="/a.txt")).to_be_visible(timeout=5000)
    expect(page.locator(".file", has_text="/c.txt")).to_be_visible(timeout=5000)
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


def test_tool_steps_render_by_type(page, server):
    """The activity drill-down renders per tool: terminal commands as
    a prompt block, file edits as a computed line diff."""
    page.goto(f"{server}/?session=e2e-steps")
    _send(
        page,
        '!tool file_write {"path": "/app.py", "content": "x = 1\\ny = 2\\n"}\n'
        "!text seeded",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "seeded", timeout=15000
    )
    _send(
        page,
        '!tool terminal {"command": "cat /app.py"}\n'
        '!tool file_edit {"path": "/app.py", "old_string": "y = 2", "new_string": "y = 3"}\n'
        "!text edited",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "edited", timeout=15000
    )
    page.locator(".chip", has_text="terminal").click()
    timeline = page.locator(".timeline")
    # terminal: prompt-prefixed command block
    expect(timeline.locator(".terminal")).to_contain_text("$ cat /app.py")
    # file_edit: old-then-new line diff with the path in the label
    expect(timeline.locator(".step-name", has_text="edit — /app.py")).to_be_visible()
    expect(timeline.locator(".diff-removed")).to_contain_text("y = 2")
    expect(timeline.locator(".diff-added")).to_contain_text("y = 3")
    # write: highlighted content (hljs spans present) in the first turn
    prev_chip = page.locator(".chip", has_text="file_write").first
    prev_chip.click()
    expect(page.locator(".timeline .hljs").first).to_be_visible()


def test_chat_markdown_link_opens_file_modal(page, server):
    """The agent linking a workspace path in prose makes it clickable:
    the shared FileModal opens with the per-type render."""
    page.goto(f"{server}/?session=e2e-modal")
    _send(
        page,
        '!tool file_write {"path": "/report.md", "content": "# Findings\\n\\nAll good."}\n'
        "!text Wrote it up — see [the report](/report.md).",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Wrote it up", timeout=15000
    )
    page.locator(".agent-msg .bubble a", has_text="the report").click()
    expect(page.locator(".modal .markdown h1")).to_have_text("Findings", timeout=5000)
    expect(page.locator(".modal .path")).to_have_text("/report.md")
    page.keyboard.press("Escape")


def test_tool_result_images_stay_in_the_timeline(page, server):
    """A screenshot/plot path in a tool RESULT renders inside the
    activity drill-down, not the transcript — inline placement is
    earned by the agent referencing the image in prose."""
    page.goto(f"{server}/?session=e2e-shots")
    _send(
        page,
        '!tool file_write {"path": "/shots/shot-1.png", "content": "not-a-real-png"}\n'
        "!text Took a screenshot for verification.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Took a screenshot", timeout=15000
    )
    # nothing rendered inline in the transcript...
    expect(page.locator(".agent-msg .artifact-img")).to_have_count(0)
    # ...but the expanded tool timeline carries it
    page.locator(".chip", has_text="file_write").click()
    expect(page.locator(".timeline .step-img")).to_have_count(1, timeout=5000)


def test_preview_app_json_post_passes_cors_preflight(page, server):
    """App code fetching its own api with a JSON body triggers a real
    CORS preflight from the sandboxed (opaque-origin) iframe — the
    case a plain header-on-response can't cover."""
    import json as _json

    html = (
        "<html><body><div id=out>waiting</div><script>"
        "fetch('api/echo',{method:'POST',"
        "headers:{'content-type':'application/json'},"
        "body:JSON.stringify({v:'pong'})})"
        ".then(r=>r.json()).then(d=>{"
        "document.getElementById('out').textContent='echo:'+d.v})"
        ".catch(e=>{document.getElementById('out').textContent='ERR '+e})"
        "</script></body></html>"
    )
    echo = "def post(req):\n    return {'v': 'pong'}\n"
    message = (
        "!tool file_write "
        + _json.dumps({"path": "/app/index.html", "content": html})
        + "\n!tool file_write "
        + _json.dumps({"path": "/app/api/echo.py", "content": echo})
        + "\n!text cors app up"
    )
    page.goto(f"{server}/?session=e2e-cors")
    _send(page, message)
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "cors app up", timeout=15000
    )
    frame = page.frame_locator("iframe[title='app preview']")
    expect(frame.locator("#out")).to_have_text("echo:pong", timeout=15000)


def test_delete_session_from_rail(page, server):
    page.goto(f"{server}/?session=e2e-del1")
    _send(page, "!text del1 alive")
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "del1 alive", timeout=15000
    )
    _title(server, "e2e-del1", "del1")

    # "+ New" mints the session and switches to it; the slug rides the
    # URL — wait for it, or we'd read the PREVIOUS session's name
    page.click(".new-btn")
    expect(page).to_have_url(re.compile(r"\?session=[a-z]+(-[a-z]+)+$"), timeout=10000)
    _title(server, page.url.split("session=")[-1], "del2")
    expect(page.locator(".row.active", has_text="del2")).to_be_visible(timeout=10000)

    # two-tap delete on the ACTIVE session: × arms, 'sure?' confirms
    row = page.locator(".row", has_text="del2")
    row.hover()
    row.locator(".delete").click()
    expect(row.locator(".delete")).to_have_text("sure?")
    row.locator(".delete").click()

    # the row disappears and the shell falls back to SOME surviving
    # session (the rail is shared across this module's tests, so which
    # one isn't ours to assume)
    expect(page.locator(".row", has_text="del2")).to_have_count(0, timeout=10000)
    expect(page.locator(".row.active")).to_be_visible(timeout=10000)

    # the sibling session was untouched: its transcript replays intact
    page.locator(".row", has_text="del1").locator(".item").click()
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "del1 alive", timeout=10000
    )


def test_new_session_button_mints_an_untitled_slug(page, server):
    """ "+ New" asks the SERVER for a session: identity is a minted slug
    nobody typed (so the agent may title it freely), and the rail shows
    the untitled default until something names it."""
    page.goto(f"{server}/?session=e2e-mint")
    expect(page.locator(".row.active")).to_be_visible(timeout=10000)

    page.click(".new-btn")
    # switchTo uses replaceState, so poll the URL rather than wait for a
    # navigation that never fires. e2e-mint can't match: it has a digit.
    expect(page).to_have_url(re.compile(r"\?session=[a-z]+(-[a-z]+)+$"), timeout=10000)
    expect(page.locator(".row.active .name")).to_have_text("New session", timeout=10000)


def test_background_turn_survives_session_switch(page, server):
    page.goto(f"{server}/?session=e2e-bg1")
    _send(page, "!text first session reply")
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "first session reply", timeout=15000
    )
    # switch away via the rail's "+ New" button, then back
    _title(server, "e2e-bg1", "bg1")
    page.click(".new-btn")
    expect(page.locator(".row.active")).to_be_visible(timeout=10000)
    page.locator(".item", has_text="bg1").click()
    # the transcript replays from the server-side event log
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "first session reply", timeout=10000
    )
