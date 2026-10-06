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
import urllib.error
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


class _Server(str):
    """The base URL, with the store the server is running on hung off
    it.

    A str subclass so every `f"{server}/..."` in this file goes on
    reading as a URL. The store is here because some of what the
    studio does is only visible on disk — a manifest write outlives
    the job table a route reads from, and a test that asserted it
    through the same route it just called would be asking the cache
    whether the cache is right.
    """

    store: Path


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    # Waking off: the rail tests below are about an answer that waits
    # for the human, which is what it does when its budget is spent or
    # the knob is 0. With waking on, it would be delivered within a
    # second and there would be nothing waiting to show.
    yield from _studio(tmp_path_factory, NONTAINER_STUDIO_DELEGATE_WAKES="0")


@pytest.fixture(scope="module")
def waking_server(tmp_path_factory):
    yield from _studio(tmp_path_factory)


def _studio(tmp_path_factory, store=None, **extra_env):
    port = _free_port()
    store = store or tmp_path_factory.mktemp("store")
    env = {
        **os.environ,
        "NONTAINER_STUDIO_MODEL": "dummy",
        "NONTAINER_STUDIO_PORT": str(port),
        "NONTAINER_STUDIO_STORE": str(store),
        # The delegation tests below drive the agent's `sessions` tool
        # and read a delegate's branch back with `ws-git`; neither is
        # given to an agent unless the studio is told to.
        "NONTAINER_STUDIO_SESSIONS": "1",
        "NONTAINER_STUDIO_WSGIT": "1",
        **extra_env,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "nontainer_studio"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = _Server(f"http://127.0.0.1:{port}")
    base.store = store
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


JSX_APP = "import { createRoot } from 'react-dom/client';\nimport { Button } from '@mui/material';\ncreateRoot(document.getElementById('root')).render(<Button id=\"marker\">compiled in the browser</Button>);\n"

JSX_HTML = '<html><body><div id="root"></div><script type="module" src="vendor/jsx-loader.js" data-app="app.jsx"></script></body></html>'


def _send(page, message: str) -> None:
    page.fill("textarea", message)
    page.get_by_role("button", name="send").click()


def _open_work(page, group: str):
    """Open a work group down to its steps: the group whose line names
    ``group`` (its plain-words summary, e.g. "Wrote" or "Ran"), and
    every step in it. Returns the group's timeline."""
    line = page.locator(".group-line", has_text=group).last
    expect(line).to_be_visible(timeout=15000)
    line.click()
    timeline = line.locator("xpath=..").locator(".timeline")
    closed = timeline.locator(".step-line:not(.open)")
    for _ in range(closed.count()):
        closed.first.click()
    return timeline


def _title(server: str, name: str, title: str) -> None:
    """Name a session so its rail row is findable. Rows are labelled by
    TITLE now — identity is a slug that never displays — so two untitled
    sessions both read "New session" and can't be told apart by text.

    Deliberately NOT page.request: that rides the browser's network
    stack, where the SSE followers pin connections against Chromium's
    per-origin cap and this POST can starve. It only arranges server
    state, so it talks to the server directly.

    Going out-of-band is what makes it RACE, though: page.goto returns
    when the document loads, and the frontend only then calls
    ensureSession -> POST /api/sessions. A caller that titles a session
    straight after goto can beat that create and get a 404 (the CI flake
    in test_rename_escape_discards, seen on more than one branch). Poll
    through 404 rather than making each caller remember to wait for the
    rail first — any other status still fails immediately."""
    req = urllib.request.Request(
        f"{server}/api/sessions/{name}/title",
        data=json.dumps({"title": title}).encode(),
        headers={"content-type": "application/json"},
    )
    deadline = time.time() + 10
    while True:
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status == 200
                return
        except urllib.error.HTTPError as e:
            if e.code != 404 or time.time() >= deadline:
                raise
            time.sleep(0.05)  # session create still in flight


# ---------------------------------------------------------------------------


def test_turn_streams_into_transcript(page, server):
    page.goto(f"{server}/?session=e2e-chat")
    _send(
        page,
        '!tool file_write {"path": "/workspace/notes.md", "content": "hello"}\n'
        "!text Wrote your note.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Wrote your note.", timeout=15000
    )
    # the work opens down to the step, which carries the REAL tool result
    timeline = _open_work(page, "Wrote")
    expect(timeline).to_contain_text("wrote /workspace/notes.md")

    # the files tab lists the real workspace write; clicking opens the
    # shared file modal with a RENDERED view (markdown, not raw text)
    page.get_by_role("button", name="files", exact=True).click()
    expect(page.locator(".file", has_text="/workspace/notes.md")).to_be_visible(
        timeout=5000
    )
    page.locator(".file", has_text="/workspace/notes.md").click()
    expect(page.locator(".modal .markdown")).to_contain_text("hello", timeout=5000)
    page.keyboard.press("Escape")
    expect(page.locator(".modal")).to_have_count(0)


def test_ui_artifact_renders_from_server_event(page, server):
    """The full artifact path: run_python assigns `ui`, WorkspaceTools
    materializes it and appends the `[ui artifacts: ...]` note, the
    server harvests that into a first-class `artifact` event, and the
    shell renders it inline. Prose doesn't reference the path, so the
    done-time Jupyter rule appends it after the reply.

    The value here is a string naming a file the agent wrote itself —
    a POINTER to an artifact rather than a value to render, and the one
    spelling outside the rendered set that still lands. It is also the
    smallest one that reaches the shell's text renderer: what renders is
    a closed set of charts, tables, cards, pictures and html, and
    nothing writes a file for data any more.
    """
    code = (
        "open('/workspace/notes.txt', 'w').write('hello from the agent')\n"
        "ui = {'notes': '/workspace/notes.txt'}"
    )
    page.goto(f"{server}/?session=e2e-artifact")
    _send(
        page,
        "!tool run_python " + json.dumps({"code": code}) + "\n!text Made an artifact.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Made an artifact.", timeout=15000
    )
    # rendered inline as a details block named for the binding, NOT
    # merely the raw note in the tool timeline
    artifact = page.locator(".agent-msg .artifact-text")
    expect(artifact).to_be_visible(timeout=10000)
    expect(artifact.locator("summary")).to_contain_text("notes")
    expect(artifact).to_contain_text("hello from the agent")


def test_a_plain_dict_in_ui_renders_nothing_and_says_why(page, server):
    """What `ui` renders is a closed set, and there is no JSON floor
    under it: a plain dict is data, so no file is written and no
    artifact event is emitted. Announcing one would tell the agent its
    figure had arrived. What it gets instead is a note in the tool
    result naming the binding and the shapes that do render — and the
    studio shows the tool result, so the human reads the same thing.
    """
    page.goto(f"{server}/?session=e2e-ui-note")
    _send(
        page,
        "!tool run_python {\"code\": \"ui = {'stats': {'hello': 'world'}}\"}\n"
        "!text Nothing to show.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Nothing to show.", timeout=15000
    )
    expect(_open_work(page, "Ran")).to_contain_text(
        "is a plain dict, which is data and not a UI artifact", timeout=10000
    )
    # and the turn carries no artifact: nothing was written to render
    expect(page.locator(".agent-msg .artifact-text")).to_have_count(0)


def test_cards_artifact_renders_stat_and_callout(page, server):
    """A `ui` value that is a list of card dicts materializes into
    /workspace/ui/*.cards.json, and the shell renders a mixed row: a stat tile
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
    expect(_open_work(page, "Ran").locator("pre.block").first).to_be_visible()
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


def test_thinking_interleaves_into_the_work_group(page, server):
    """Thinking around tool calls folds INTO the work group (the
    think -> act narrative lives in the drill-down, as "Thought …"
    lines between the steps); a tool-free thought is a line of its
    own."""
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
    timeline = _open_work(page, "Wrote")
    # no standalone thinking line: it joined the work group
    expect(page.locator(".agent-msg > .think-block")).to_have_count(0)
    thought = timeline.locator(".think-toggle")
    expect(thought).to_contain_text("Thought")
    thought.click()
    expect(timeline).to_contain_text("Considering the request carefully.")

    # a pure thought (no tools) is a line of its own
    _send(page, "!think Just musing, no tools.\n!text Mused.")
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Mused.", timeout=15000
    )
    toggle = page.locator(".agent-msg").last.locator(".think-toggle")
    expect(toggle).to_be_visible()
    toggle.click()
    expect(page.locator(".think-text").last).to_contain_text("Just musing")


def test_a_finished_turn_keeps_its_work_lines_in_place(page, server):
    """No wrapper around a finished turn: each run of work stays the
    one quiet line it already is, in place beside the agent's prose,
    so the transcript reads prose, work, prose, work."""
    page.goto(f"{server}/?session=e2e-inplace")
    _send(
        page,
        "!think Planning.\n"
        '!tool terminal {"command": "echo hi"}\n'
        '!tool file_write {"path": "/a.txt", "content": "a"}\n'
        "!text All set.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "All set.", timeout=15000
    )
    msg = page.locator(".agent-msg").last
    expect(msg.locator(".worked")).to_have_count(0)
    line = msg.locator(".group-line")
    expect(line).to_have_count(1)  # the work, one line, already visible
    expect(line).to_contain_text("Ran 1 command")
    expect(line).to_contain_text("wrote 1 file")
    expect(msg.locator(".step-line")).to_have_count(0)  # its steps folded


def test_a_message_typed_while_the_agent_works_waits_then_lands(page, server):
    """The composer stays open while a turn runs: Enter queues the
    message, it shows as waiting, and it becomes an ordinary user
    bubble the moment the agent reads it — with no edit handle, since
    the turn it landed in began before it was said."""
    page.goto(f"{server}/?session=e2e-queue")
    _send(
        page,
        '!tool run_python {"code": "import time\\ntime.sleep(3)"}\n!text all done',
    )
    expect(page.locator(".send-btn.stop")).to_be_visible(timeout=10000)

    page.fill("textarea", "use a log scale")
    page.keyboard.press("Enter")
    expect(page.locator(".user-bubble.queued")).to_contain_text(
        "use a log scale", timeout=10000
    )

    # delivered with the tool result, and the turn carries on past it
    expect(page.locator(".user-bubble.queued")).to_have_count(0, timeout=30000)
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "all done", timeout=30000
    )
    rows = page.locator(".user-row")
    expect(rows.last).to_contain_text("use a log scale")
    rows.last.hover()
    expect(rows.last.locator(".edit")).to_have_count(0)

    # the interjection splits the agent's message in two, and the tool
    # call it landed inside opened in the first half: its result must
    # still pair with THAT call, not open a second work group and
    # leave the first running forever
    expect(page.locator(".group-line")).to_have_count(1)
    expect(page.locator(".group-line.act-live")).to_have_count(0)


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
        '!tool file_write {"path": "/workspace/app/index.html", "content": '
        '"<html><body><h1 id=marker>hi from the app</h1></body></html>"}\n'
        "!text App is up.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "App is up.", timeout=15000
    )
    # preview tab renders the live app in the sandboxed iframe
    frame = page.frame_locator("iframe[title='app preview']")
    expect(frame.locator("#marker")).to_have_text("hi from the app", timeout=15000)


SKILLS = Path(__file__).resolve().parents[1] / "skills"


def _video_frames(page):
    """The two frames the reference video runs in: the player page in
    the preview's sandboxed frame, and the composition in the player's
    own iframe inside that. Polled with page.wait_for_timeout, not
    time.sleep: the sync API only takes in frame events while a
    Playwright call is running, so a sleeping loop never sees the frame
    arrive."""
    for _ in range(200):
        for frame in page.frames:
            if frame.url.endswith("/video.html") and frame.parent_frame is not None:
                return frame.parent_frame, frame
        page.wait_for_timeout(100)
    raise AssertionError(f"no composition frame: {[f.url for f in page.frames]}")


def test_a_videos_narration_plays_in_the_preview_pane(page, browser, server):
    """Sound may start only in a frame every frame above has granted it
    to, and a video player plays its composition's narration one frame
    down from the click. Without the preview granting autoplay the
    picture played and the narration stayed silent, while the same
    video opened in its own tab had sound, whenever play was pressed
    before the composition loaded. Real autoplay rules: the browser here
    is launched with no override."""
    import io
    import urllib.request
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\0\0" * 24000 * 3)
    urllib.request.urlopen(
        urllib.request.Request(
            f"{server}/api/sessions",
            data=json.dumps({"name": "e2e-voice"}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
    ).read()
    page.goto(f"{server}/?session=e2e-voice")
    urllib.request.urlopen(
        urllib.request.Request(
            f"{server}/api/sessions/e2e-voice/upload?name=voice.wav",
            data=buf.getvalue(),
            method="POST",
        )
    ).read()
    refs = SKILLS / "making-videos" / "references"
    video = (
        (refs / "video.html")
        .read_text()
        .replace(
            '<div class="scene clip" id="scene-title"',
            '<audio class="clip" id="vo" src="audio/voice.wav" data-start="3.5"'
            ' data-duration="3" crossorigin></audio>\n    '
            '<div class="scene clip" id="scene-title"',
            1,
        )
    )
    _send(
        page,
        "!tool terminal "
        + json.dumps(
            {"command": "mkdir -p app/audio && cp uploads/voice.wav app/audio/"}
        )
        + "\n!tool file_write "
        + json.dumps(
            {
                "path": "/workspace/app/index.html",
                "content": (refs / "index.html").read_text(),
            }
        )
        + "\n!tool file_write "
        + json.dumps({"path": "/workspace/app/video.html", "content": video})
        + "\n!text Narrated video is up.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Narrated video is up.", timeout=15000
    )
    # Watched from a page that has done nothing but load the video, as a
    # human coming back to it would. Play is pressed as soon as the
    # player shows, before the composition has loaded, which is when it
    # went silent: the player holds the click until the video is ready,
    # and by then sound may start only in a frame granted autoplay.
    watcher = browser.new_page()
    # Hold the composition back until play is pressed, so the click
    # always lands before the video has loaded rather than by a race.
    held = []

    def hold(route):
        held.append(route)

    watcher.route("**/video.html", hold)
    watcher.goto(f"{server}/?session=e2e-voice")
    host = None
    for _ in range(200):
        host = next(
            (
                f
                for f in watcher.frames
                if "/preview/e2e-voice" in f.url and not f.url.endswith("/video.html")
            ),
            None,
        )
        if host is not None:
            break
        watcher.wait_for_timeout(50)
    assert host is not None, [f.url for f in watcher.frames]
    host.locator("hyperframes-player").click()
    for _ in range(200):
        if held:
            break
        watcher.wait_for_timeout(50)
    for route in held:
        route.continue_()
    watcher.unroute("**/video.html")
    # Watched from the player's side: Playwright runs a script with a
    # user gesture, and a script run in the composition before the
    # narration starts would grant it the sound this is about. The
    # narration is read once, after it should have begun.
    host.wait_for_function(
        "document.querySelector('hyperframes-player').currentTime > 5", timeout=20000
    )
    _, video_frame = _video_frames(watcher)
    voice = video_frame.evaluate(
        "(() => { const a = document.getElementById('vo');"
        " return {time: a.currentTime, paused: a.paused}; })()"
    )
    assert not voice["paused"] and voice["time"] > 0.5, voice
    watcher.close()


def test_the_reference_video_plays_in_the_sandboxed_frame(page, server):
    """The making-videos skill's reference, copied into an app and shown
    where a human watches it: the preview pane. That frame is an opaque
    origin and the player nests an iframe of its own inside it, so this
    is the arrangement a plain browser test does not see. Nothing may
    leave the studio's origin -- the player fetches the runtime from a
    CDN when a composition lacks it, which is the failure this pins."""
    refs = SKILLS / "making-videos" / "references"
    # Every frame, the studio's own page included: the shell serves its
    # fonts itself, so nothing here has a reason to leave the origin.
    outside: list[str] = []
    page.on(
        "request",
        lambda r: None if r.url.startswith(server) else outside.append(r.url),
    )
    page.goto(f"{server}/?session=e2e-video")
    _send(
        page,
        "!tool file_write "
        + json.dumps(
            {
                "path": "/workspace/app/index.html",
                "content": (refs / "index.html").read_text(),
            }
        )
        + "\n!tool file_write "
        + json.dumps(
            {
                "path": "/workspace/app/video.html",
                "content": (refs / "video.html").read_text(),
            }
        )
        + "\n!text Video is up.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Video is up.", timeout=15000
    )
    host, video = _video_frames(page)
    video.wait_for_function("window.__playerReady === true", timeout=15000)
    assert video.evaluate("window.__player.getDuration()") == 10

    # Seek through the player element's own API, as its controls do.
    player = "document.querySelector('hyperframes-player')"
    host.wait_for_function(f"{player}.duration === 10", timeout=15000)
    host.evaluate(f"{player}.seek(1.5)")
    video.wait_for_function(
        "getComputedStyle(document.querySelector('#scene-title')).visibility === 'visible'"
    )
    host.evaluate(f"{player}.seek(5)")
    video.wait_for_function(
        "getComputedStyle(document.querySelector('#scene-chart')).visibility === 'visible'"
        " && parseFloat(getComputedStyle(document.querySelector('#q4')).height) > 500"
    )
    assert (
        video.evaluate(
            "getComputedStyle(document.querySelector('#scene-title')).visibility"
        )
        == "hidden"
    )

    # And it plays: the clock moves on its own.
    host.evaluate(f"{player}.seek(0); {player}.play()")
    video.wait_for_function("window.__player.getTime() > 0.5", timeout=10000)
    assert not outside, outside


def test_publish_a_version_and_toggle_between_live_and_published(page, server):
    """The whole publish loop through the browser: publish names a
    version, the transcript gets its marker, and the toggle puts the
    app's own URL in the pane beside the live one — which then diverge,
    because that is the point of publishing."""
    page.goto(f"{server}/?session=e2e-publish")
    _title(server, "e2e-publish", "Toggle app")
    _send(
        page,
        '!tool file_write {"path": "/workspace/app/index.html", "content": '
        '"<html><body><h1 id=marker>version one</h1></body></html>"}\n'
        "!text App is up.",
    )
    frame = page.frame_locator("iframe[title='app preview']")
    served = page.frame_locator("iframe[title='published app']")
    expect(frame.locator("#marker")).to_have_text("version one", timeout=20000)

    # one click publishes, and the server picks the name
    page.locator(".pub-main").click()

    # the transcript gets a landmark and the button says what happened,
    # but the pane stays where the human left it
    expect(page.locator(".publish")).to_contain_text("published", timeout=15000)
    expect(page.locator(".publish")).to_contain_text("v1")
    expect(page.locator(".pub-main")).to_have_text("published v1", timeout=10000)
    expect(page.locator(".seg.on")).to_have_text("live")

    # the app's own URL is a click away, and serves the frozen version
    page.get_by_role("button", name="published", exact=True).click()
    expect(served.locator("#marker")).to_have_text("version one", timeout=20000)

    # the session keeps moving; the published app doesn't
    _send(
        page,
        '!tool file_write {"path": "/workspace/app/index.html", "content": '
        '"<html><body><h1 id=marker>version two</h1></body></html>"}\n'
        "!text Changed it.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Changed it.", timeout=20000
    )
    # the edit is unpublished, and the button counts it
    expect(page.locator(".pub-main")).to_have_text("publish · 1 file", timeout=15000)
    expect(served.locator("#marker")).to_have_text("version one", timeout=20000)

    page.get_by_role("button", name="live", exact=True).click()
    expect(frame.locator("#marker")).to_have_text("version two", timeout=20000)

    # publish v2, then roll the URL back to v1 from the version strip
    # under the frame: the URL is stable, so only the version in the
    # iframe's key makes the pane show the rollback
    page.locator(".pub-main").click()
    expect(page.locator(".pub-main")).to_have_text("published v2", timeout=15000)
    page.get_by_role("button", name="published", exact=True).click()
    expect(served.locator("#marker")).to_have_text("version two", timeout=20000)

    page.locator(".panel li", has_text="v1").get_by_role(
        "button", name="make current"
    ).click()
    expect(page.locator(".panel li.current")).to_contain_text("v1", timeout=10000)
    # nothing covers the app any more, so the frame beside the strip
    # shows the rollback as it happens
    expect(served.locator("#marker")).to_have_text("version one", timeout=20000)
    # the rail reads a store-wide list, the strip a per-session one —
    # both are projections of the row that just changed, so the rail
    # must not wait for its 4s poll to say so (hence the tight bound)
    rail_row = page.locator(".app-row", has_text="Toggle app")
    expect(rail_row).to_contain_text("v1 · 2 versions", timeout=2500)

    # ...and the same for a removal: v2 is no longer current, so it can go
    page.locator(".panel li", has_text="v2").get_by_role(
        "button", name="delete"
    ).click()
    page.locator(".panel li", has_text="v2").get_by_role(
        "button", name="really delete"
    ).click()
    expect(rail_row).to_contain_text("v1 · 1 version ", timeout=2500)


def test_publish_as_names_the_version(page, server):
    """The considered path: the caret opens an empty field, and what is
    typed there is the version's name instead of the server's vN."""
    page.goto(f"{server}/?session=e2e-publish-as")
    _send(
        page,
        '!tool file_write {"path": "/workspace/app/index.html", "content": '
        '"<html><body><h1 id=marker>named</h1></body></html>"}\n'
        "!text App is up.",
    )
    frame = page.frame_locator("iframe[title='app preview']")
    expect(frame.locator("#marker")).to_have_text("named", timeout=20000)

    page.get_by_role("button", name="publish as…").click()
    field = page.locator("input[aria-label='version name']")
    expect(field).to_have_value("")
    field.fill("demo")
    field.press("Enter")

    expect(page.locator(".publish")).to_contain_text("demo", timeout=15000)
    expect(page.locator(".pub-main")).to_have_text("published demo", timeout=10000)


def test_the_changes_tab_lists_and_diffs_what_is_unpublished(page, server):
    """The third side tab answers "which files": the tab label carries
    the count the publish button carries, each row names a file and
    what happened to it, an opened row is the edit itself, and
    publishing from the tab empties the list."""
    page.goto(f"{server}/?session=e2e-changes")
    _send(
        page,
        '!tool file_write {"path": "/workspace/app/index.html", "content": '
        '"<html><body><h1 id=marker>version one</h1></body></html>"}\n'
        "!text App is up.",
    )
    frame = page.frame_locator("iframe[title='app preview']")
    expect(frame.locator("#marker")).to_have_text("version one", timeout=20000)

    # nothing published: the tab says what a first publish would do,
    # counted off the file list rather than a diff
    changes_tab = page.locator(".tab", has_text="changes")
    changes_tab.click()
    expect(page.locator(".changes .head")).to_contain_text(
        "nothing published yet", timeout=15000
    )
    expect(page.locator(".changes .head")).to_contain_text("1 file behind a URL")

    page.locator(".tab", has_text="preview").click()
    page.locator(".pub-main").click()
    expect(page.locator(".pub-main")).to_have_text("published v1", timeout=15000)

    # one file rewritten, one new: two unpublished changes
    _send(
        page,
        '!tool file_write {"path": "/workspace/app/index.html", "content": '
        '"<html><body><h1 id=marker>version two</h1></body></html>"}\n'
        '!tool file_write {"path": "/workspace/app/extra.js", "content": '
        '"console.log(1)\\n"}\n'
        "!text Changed it.",
    )
    expect(changes_tab).to_have_text("changes · 2", timeout=20000)
    changes_tab.click()

    rows = page.locator(".row-head")
    expect(rows).to_have_count(2)
    expect(page.locator(".row-head", has_text="index.html")).to_contain_text("changed")
    expect(page.locator(".row-head", has_text="extra.js")).to_contain_text("added")

    # expanding fetches the two sides and renders the same diff the
    # transcript shows for an edit
    page.locator(".row-head", has_text="index.html").click()
    expect(page.locator(".diff-added").first).to_contain_text(
        "version two", timeout=15000
    )
    expect(page.locator(".diff-removed").first).to_contain_text("version one")

    # publishing from the tab is the plain one-click publish: the list
    # empties and the count leaves the label
    page.locator(".changes .head").get_by_role("button", name="publish").click()
    expect(changes_tab).to_have_text("changes", timeout=15000)
    expect(page.locator(".changes .hint")).to_contain_text("nothing unpublished")

    # a file the agent takes away is a row too, not a silence
    _send(page, '!tool terminal {"command": "rm /workspace/app/extra.js"}\n!text Gone.')
    expect(changes_tab).to_have_text("changes · 1", timeout=20000)
    expect(page.locator(".row-head", has_text="extra.js")).to_contain_text("removed")


def test_a_published_jsx_app_loads_in_the_sandboxed_frame(page, server):
    """The failure the CORS wrapper exists for, end to end. The frame is
    an opaque origin, so the jsx loader's own fetch for app.jsx is
    cross-origin; without the header on the /apps mount it is blocked
    and the page renders nothing, while the same URL in a tab works."""
    page.goto(f"{server}/?session=e2e-published-jsx")
    _send(
        page,
        "!tool file_write "
        + json.dumps({"path": "/workspace/app/app.jsx", "content": JSX_APP})
        + "\n!tool file_write "
        + json.dumps({"path": "/workspace/app/index.html", "content": JSX_HTML})
        + "\n!text JSX app is up.",
    )
    # the LIVE pane first — its own route has carried these headers all
    # along, so this is the control for the comparison below (and it
    # waits out the turn, which publishing refuses to race)
    frame = page.frame_locator("iframe[title='app preview']")
    expect(frame.locator("#marker")).to_contain_text(
        "compiled in the browser", timeout=25000
    )

    page.locator(".pub-main").click()
    expect(page.locator(".pub-main")).to_have_text("published v1", timeout=15000)
    expect(page.locator(".pub-error")).to_have_count(0)
    page.get_by_role("button", name="published", exact=True).click()

    # and now the published mount: the loader's fetch for app.jsx is
    # cross-origin from the opaque frame, so nothing renders without the
    # header the wrapper adds
    served = page.frame_locator("iframe[title='published app']")
    expect(served.locator("#marker")).to_contain_text(
        "compiled in the browser", timeout=25000
    )


def test_a_publish_marker_says_when_its_app_is_gone(page, server):
    """The marker is a history fact and stays — the publish happened —
    but it must not keep offering a link to an app that was unpublished
    or a version that was deleted. Restoring survives all three states:
    the commit is anchored in this session's history, not in the
    publication."""
    page.goto(f"{server}/?session=e2e-marker")
    _title(server, "e2e-marker", "Marker app")
    _send(
        page,
        '!tool file_write {"path": "/workspace/app/index.html", "content": '
        '"<html><body><h1 id=marker>a marker app</h1></body></html>"}\n'
        "!text App is up.",
    )
    frame = page.frame_locator("iframe[title='app preview']")
    expect(frame.locator("#marker")).to_have_text("a marker app", timeout=20000)

    # v1, then v2 — two markers, so the version-removed state has a
    # marker of its own to land on
    markers = page.locator(".publish")
    page.locator(".pub-main").click()
    expect(markers).to_have_count(1, timeout=15000)
    expect(page.locator(".pub-main")).to_have_text("published v1", timeout=10000)
    page.locator(".pub-main").click()
    expect(markers).to_have_count(2, timeout=15000)
    expect(markers.first).to_contain_text("v1")
    expect(markers.last).to_contain_text("v2")
    # both live: an open link, no removal note
    expect(markers.first.get_by_role("link", name="open ↗")).to_be_visible()
    expect(markers.last.get_by_role("link", name="open ↗")).to_be_visible()

    # delete v1 (v2 is current, so v1 can go) — only ITS marker changes
    page.get_by_role("button", name="published", exact=True).click()
    page.locator(".panel li", has_text="v1").get_by_role(
        "button", name="delete"
    ).click()
    page.locator(".panel li", has_text="v1").get_by_role(
        "button", name="really delete"
    ).click()
    expect(markers.first).to_contain_text("version removed", timeout=10000)
    expect(markers.first.get_by_role("link", name="open ↗")).to_have_count(0)
    expect(
        markers.first.get_by_role("button", name="restore to this publish")
    ).to_be_visible()
    expect(markers.last.get_by_role("link", name="open ↗")).to_be_visible()

    # unpublish from the RAIL: every marker for that app says so
    page.locator(".app-row", has_text="Marker app").click()
    danger = page.locator(".app-view .verb.danger").first
    danger.click()
    danger.click()
    expect(markers.last).to_contain_text("since removed", timeout=10000)
    expect(markers.first).to_contain_text("since removed")
    expect(page.locator(".publish a")).to_have_count(0)
    # the anchor is this session's history, so restoring is still offered
    expect(
        markers.last.get_by_role("button", name="restore to this publish")
    ).to_be_visible()


def _delete(server: str, name: str) -> None:
    """Delete a session out of band. Like ``_title``, this only arranges
    server state and deliberately avoids the browser's network stack."""
    req = urllib.request.Request(f"{server}/api/sessions/{name}", method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200


def test_the_rail_lists_apps_whose_session_is_gone(page, server):
    """An app outlives the session that made it, so the rail lists apps
    store-wide: after the session is deleted the app is reachable from
    nowhere else, and selecting it serves the app with its verbs. The
    frame is a sandboxed opaque origin, so this is also the CORS path."""
    page.goto(f"{server}/?session=e2e-rail-app")
    _title(server, "e2e-rail-app", "Rail app")
    _send(
        page,
        '!tool file_write {"path": "/workspace/app/index.html", "content": '
        '"<html><body><h1 id=marker>served from the rail</h1></body></html>"}\n'
        "!text App is up.",
    )
    frame = page.frame_locator("iframe[title='app preview']")
    expect(frame.locator("#marker")).to_have_text("served from the rail", timeout=20000)
    page.locator(".pub-main").click()
    expect(page.locator(".publish")).to_contain_text("published", timeout=15000)

    row = page.locator(".app-row", has_text="Rail app")
    expect(row).to_be_visible(timeout=15000)
    expect(row).to_contain_text("from Rail app")

    _delete(server, "e2e-rail-app")
    # the app survives its session, and the row says the origin is gone
    expect(row).to_contain_text("session deleted", timeout=15000)

    # the side pane may be collapsed — selecting an app is a request to
    # SEE it, so the row has to reveal the pane, not just highlight
    page.get_by_role("button", name="toggle preview panel").click()
    expect(page.locator("iframe[title='app preview']")).to_have_count(0)

    row.click()
    served = page.frame_locator("iframe[title='published app']")
    expect(served.locator("#marker")).to_have_text(
        "served from the rail", timeout=20000
    )
    # branching needs a conversation that no longer exists
    branch = page.locator(".app-view button", has_text="branch from")
    expect(branch).to_be_disabled()

    # unpublish arms, then commits, and the app leaves the rail
    danger = page.locator(".app-view .verb.danger").first
    danger.click()
    expect(danger).to_contain_text("really unpublish")
    expect(danger).to_contain_text("the link stop working")
    danger.click()
    expect(page.locator(".app-row", has_text="Rail app")).to_have_count(
        0, timeout=15000
    )
    expect(page.locator("iframe[title='published app']")).to_have_count(0)


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
    # the group says what the run did, in plain words
    timeline = _open_work(page, "Ran")
    expect(page.locator(".group-line").last).to_contain_text("Ran 1 command")
    expect(page.locator(".group-line").last).to_contain_text("edited 1 file")
    # terminal: its line names the command, its detail is a prompt block
    expect(timeline.locator(".step-line", has_text="cat /app.py")).to_be_visible()
    expect(timeline.locator(".terminal")).to_contain_text("$ cat /app.py")
    # file_edit: "Edited" and the path, then an old-then-new line diff
    expect(timeline.locator(".step-line", has_text="Edited")).to_contain_text("/app.py")
    expect(timeline.locator(".diff-removed")).to_contain_text("y = 2")
    expect(timeline.locator(".diff-added")).to_contain_text("y = 3")
    # write: highlighted content (hljs spans present) in the first turn
    expect(_open_work(page, "Wrote").locator(".hljs").first).to_be_visible()


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
    # ...but the step carries it, and its line says so while folded
    page.locator(".group-line", has_text="Wrote").last.click()
    expect(page.locator(".step-line").last).to_contain_text("1 image")
    page.locator(".step-line").last.click()
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
        + _json.dumps({"path": "/workspace/app/index.html", "content": html})
        + "\n!tool file_write "
        + _json.dumps({"path": "/workspace/app/api/echo.py", "content": echo})
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
    row.hover()  # the row's actions are in its hover tray
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


def test_fork_from_the_rail_switches_to_the_child(page, server):
    """The fork action branches the whole universe and the shell moves
    to it: the child carries the parent's files AND the transcript that
    describes them, under its own minted slug."""
    page.goto(f"{server}/?session=e2e-fork")
    _send(
        page,
        '!tool file_write {"path": "/workspace/forked.txt", "content": "kept"}\n'
        "!text wrote the note.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "wrote the note", timeout=15000
    )
    _title(server, "e2e-fork", "fork parent")

    row = page.locator(".row", has_text="fork parent")
    row.hover()
    row.hover()
    row.locator(".fork").click()

    # the shell switched to the minted child, whose transcript is the
    # parent's (the memory it inherited says the same thing)
    expect(page).to_have_url(re.compile(r"\?session=[a-z]+(-[a-z]+)+$"), timeout=10000)
    child = page.url.split("session=")[-1]
    assert child != "e2e-fork"
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "wrote the note", timeout=10000
    )
    with urllib.request.urlopen(
        f"{server}/api/sessions/{child}/files", timeout=10
    ) as r:
        files = json.load(r)["files"]
    assert "/workspace/forked.txt" in files


def test_the_studio_names_the_session_from_the_transcript(page, server):
    """The whole path for real: the turn ends, the studio reads the
    transcript back with a second model run, and the rail row stops
    reading "New session" — while the URL keeps the slug that is
    identity. The scripted model echoes what it is shown, so the name
    it gives begins with its own prefix."""
    page.goto(f"{server}/?session=e2e-title")
    expect(page.locator(".row.active .name")).to_have_text("New session", timeout=10000)
    _send(page, "!text Named this session.")
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Named this session.", timeout=15000
    )
    # the rail relabels off the title event (it polls too), header agrees...
    expect(page.locator(".row.active .name")).to_contain_text("dummy:", timeout=15000)
    expect(page.locator(".session-name")).to_contain_text("dummy:")
    # ...and identity never moved
    assert page.url.endswith("?session=e2e-title")


def test_rename_from_the_rail_outranks_the_generated_name(page, server):
    """Double-click the label to rename. The human's title wins from
    there on, and clearing it falls back to what the studio generated."""
    page.goto(f"{server}/?session=e2e-rename")
    _send(page, "!text ok.")
    expect(page.locator(".row.active .name")).to_contain_text("dummy:", timeout=15000)
    generated = page.locator(".row.active .name").inner_text()

    row = page.locator(".row.active")
    row.locator(".name").dblclick()
    page.fill(".rename", "My name for it")
    page.press(".rename", "Enter")
    expect(page.locator(".row.active .name")).to_have_text(
        "My name for it", timeout=10000
    )

    # another turn: the human's title still wins, and nothing regenerates
    # under it
    _send(page, "!text and again.")
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "and again.", timeout=15000
    )
    expect(page.locator(".row.active .name")).to_have_text("My name for it")

    # clearing reveals what the studio generated
    row.locator(".name").dblclick()
    page.fill(".rename", "")
    page.press(".rename", "Enter")
    expect(page.locator(".row.active .name")).to_have_text(generated, timeout=10000)


def test_rename_escape_discards(page, server):
    """Escape must not save. The input unmounts on cancel and blur still
    fires — the commit path has to know the difference."""
    page.goto(f"{server}/?session=e2e-esc")
    _title(server, "e2e-esc", "Keep me")
    expect(page.locator(".row.active .name")).to_have_text("Keep me", timeout=10000)

    page.locator(".row.active .name").dblclick()
    page.fill(".rename", "typed but abandoned")
    page.press(".rename", "Escape")
    expect(page.locator(".rename")).to_have_count(0)
    expect(page.locator(".row.active .name")).to_have_text("Keep me")
    # and it really didn't reach the server
    page.reload()
    expect(page.locator(".row.active .name")).to_have_text("Keep me", timeout=10000)


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


def test_a_delegates_answer_reaches_the_parent_next_turn(page, server):
    """Delegation end to end: the agent forks itself with the `sessions`
    tool, the delegate writes a file on its own branch and answers, the
    rail says an answer is waiting, and the parent's next turn carries it
    into the transcript with its provenance header."""
    page.goto(f"{server}/?session=e2e-delegate")
    # Named by hand: the studio names a session out of its own
    # transcript, and this one's transcript is about a scout — so the
    # row this test asserts is NOT in the rail would be matched by the
    # parent's own label.
    _title(server, "e2e-delegate", "Delegating")
    _send(
        page,
        '!tool sessions {"action": "ask", "name": "scout", '
        '"task": "!tool file_write {\\"path\\": \\"/workspace/scouted.md\\", '
        '\\"content\\": \\"found it\\"}\\n!text Found it."}\n'
        "!text Sent a scout.",
    )
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Sent a scout.", timeout=15000
    )
    # the delegate is a session of its own, and NOT a row in the rail
    # an answer waiting shows on the parent's dot, at rest
    expect(page.locator(".rail .dot.answered")).to_be_visible(timeout=20000)
    expect(page.locator(".rail .item", has_text="scout")).to_have_count(0)

    _send(page, "what did the scout say?")
    # one line: the handle its parent gave it, and the gist of its answer
    card = page.locator(".delegate-card")
    expect(card).to_be_visible(timeout=15000)
    expect(card).to_contain_text("scout")
    expect(card).to_contain_text("answered")
    expect(card).to_contain_text("Found it.")
    # and delivering it clears the rail badge
    expect(page.locator(".rail .dot.answered")).to_have_count(0, timeout=20000)
    expect(page.locator(".rail .waiting")).to_have_count(0)


def test_an_answer_wakes_the_parent_without_a_message(browser, waking_server):
    """With waking on, the delegate's answer starts the parent's next
    turn by itself: the answer card appears, the agent replies to it,
    and the human sent one message the whole time."""
    page = browser.new_page()
    try:
        page.goto(f"{waking_server}/?session=e2e-wake")
        _send(
            page,
            '!tool sessions {"action": "ask", "name": "scout", '
            '"task": "!text Found it."}\n'
            "!text Sent a scout.",
        )
        card = page.locator(".delegate-card")
        expect(card).to_be_visible(timeout=20000)
        expect(card).to_contain_text("scout")
        expect(card).to_contain_text("Found it.")
        # the woken turn's reply follows the card (the dummy echoes what
        # it was sent, which opens with the answer)
        expect(page.locator(".agent-msg .bubble").last).to_contain_text(
            "dummy: [delegate", timeout=15000
        )
        expect(page.locator(".user-bubble")).to_have_count(1)
    finally:
        page.close()


def test_the_strip_shows_a_delegate_at_work_until_its_answer_lands(page, server):
    """A parent waiting on a delegate has ended its turn. The strip above
    the composer says it is not done: the delegate's card, its last
    step, then its answer on the way, and nothing once the answer has
    reached the parent. The rail badge says the same where the session
    is not open."""
    page.goto(f"{server}/?session=e2e-strip")
    _title(server, "e2e-strip", "Striping")
    sleepy = json.dumps({"code": "import time; time.sleep(5)"})
    task = f"!tool run_python {sleepy}\n!text Slept on it."
    ask = json.dumps({"action": "ask", "name": "scout", "task": task})
    _send(page, f"!tool sessions {ask}\n!text Sent a scout.")

    card = page.locator(".delegate-strip .card", has_text="scout")
    expect(card).to_be_visible(timeout=15000)
    expect(card).to_have_class(re.compile(r"\brunning\b"))
    expect(card).to_contain_text("Ran Python", timeout=10000)
    label = page.locator(".delegate-strip .label")
    expect(label).to_have_text("⑂ 1 delegate working")
    row = page.locator(".rail .row", has_text="Striping")
    expect(row.locator(".dot.delegating")).to_be_visible(timeout=10000)
    # the work line names what was done, not "sessions ask"
    expect(page.locator(".agent-msg").last).to_contain_text("Asked 1 delegate")

    # waking is off on this server: answered, and waiting for the human
    expect(card).to_have_class(re.compile(r"\banswered\b"), timeout=20000)
    expect(label).to_have_text("⑂ 1 delegate answered")
    _send(page, "what did the scout say?")
    expect(page.locator(".delegate-strip")).to_have_count(0, timeout=15000)

    # a card is a way in: the delegate's own transcript
    _send(page, f"!tool sessions {ask.replace('scout', 'second')}\n!text Sent another.")
    second = page.locator(".delegate-strip .card", has_text="second")
    expect(second).to_be_visible(timeout=15000)
    second.click()
    expect(page.locator(".delegate-bar")).to_be_visible(timeout=10000)


def test_the_rail_lists_a_sessions_delegates_and_keeps_one(page, server):
    """A delegate is not a rail row, but its branch ages out on the
    studio's retention TTL — so the human can see what this session
    delegated, and say `keep` about one, from the rail."""
    page.goto(f"{server}/?session=e2e-keep")
    _send(
        page,
        '!tool sessions {"action": "ask", "name": "scout", '
        '"task": "!text Had a look."}\n'
        "!text Sent a scout.",
    )
    _title(server, "e2e-keep", "keeper")
    row = page.locator(".rail .row", has_text="keeper")
    expect(row.locator(".dot.answered")).to_be_visible(timeout=20000)

    row.hover()  # the count is in the hover tray
    row.locator(".waiting").click()
    listed = page.locator(".rail .delegate")
    expect(listed).to_have_count(1, timeout=10000)
    expect(listed).to_contain_text("scout")

    listed.locator("button.keep").click()
    expect(listed.locator("button.keep.on")).to_be_visible(timeout=10000)

    # the record is what outlives the job table the badge reads from,
    # so that is where a keep has to land
    record = json.loads((server.store / "sessions.json").read_text())["delegates"]
    assert record["e2e-keep.scout"]["kept"] is True


def test_the_title_has_the_row_and_the_actions_ride_over_it(page, server):
    """At rest a row is its dot and its title, the title as wide as the
    row allows; the count, fork and delete sit in a tray over the end of
    it, shown on hover and on keyboard focus, and click-through when
    hidden so the whole title switches sessions."""
    page.goto(f"{server}/?session=e2e-tray")
    _send(
        page,
        '!tool sessions {"action": "ask", "name": "scout", '
        '"task": "!text Had a look."}\n'
        "!text Sent a scout.",
    )
    _title(server, "e2e-tray", "A title long enough to need the whole row")
    row = page.locator(".rail .row", has_text="A title long")
    expect(row.locator(".dot.answered")).to_be_visible(timeout=20000)
    tray = row.locator(".tray")
    page.mouse.move(800, 600)  # off the rail
    expect(tray).to_have_css("opacity", "0")
    expect(tray).to_have_css("pointer-events", "none")
    name, whole = row.locator(".name").bounding_box(), row.bounding_box()
    # the title runs to the row's end, less its own padding
    assert name["x"] + name["width"] > whole["x"] + whole["width"] - 16

    row.hover()
    expect(tray).to_have_css("opacity", "1")
    expect(tray.locator(".waiting")).to_have_text("⑂1")
    page.mouse.move(800, 600)
    expect(tray).to_have_css("opacity", "0")

    # a click leaves the row's button focused; that is pointer focus,
    # and the tray goes once the pointer does
    row.locator(".item").click()
    page.mouse.move(800, 600)
    expect(tray).to_have_css("opacity", "0")

    # keyboard focus shows it: tab to the row's button from the one
    # before it in the rail
    row.locator(".item").focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    expect(row.locator(".item")).to_be_focused()
    expect(tray).to_have_css("opacity", "1")


def test_a_delegate_opens_read_only_and_the_crumb_leads_back(page, server):
    """The drill-down: a delegate is not a rail row, but its transcript
    is worth reading, so it opens in the ordinary chat view — with the
    parent in a breadcrumb above it and a read-only bar where the
    composer would be. The parent agent drives it; the human reads it."""
    page.goto(f"{server}/?session=e2e-drill")
    _send(
        page,
        '!tool sessions {"action": "ask", "name": "scout", '
        '"task": "!text Had a look."}\n'
        "!text Sent a scout.",
    )
    _title(server, "e2e-drill", "driller")
    row = page.locator(".rail .row", has_text="driller")
    expect(row.locator(".dot.answered")).to_be_visible(timeout=20000)

    # straight at the child, the way a reload lands: the shell has only
    # the name and has to ask the server what it is looking at
    page.goto(f"{server}/?session=e2e-drill.scout")

    bar = page.locator(".delegate-bar")
    expect(bar).to_be_visible(timeout=20000)
    expect(bar).to_contain_text("back to driller")
    expect(bar).to_contain_text("answered")
    # when the sweep may take it, beside the keep that stops it
    expect(bar.locator(".expiry")).to_contain_text("expires in 23h")
    expect(bar.locator("button.keep")).to_have_text("keep")
    # no composer, and no handle that would rewind the transcript: there
    # is nothing here for a human to say
    expect(page.locator(".input-wrap")).to_have_count(0)
    expect(page.locator("textarea")).to_have_count(0)
    expect(page.locator("button.edit")).to_have_count(0)
    # nor one that would publish: that commits the delegate's open work
    # and puts a version of it behind a public URL
    expect(page.get_by_role("button", name="publish", exact=True)).to_have_count(0)
    # its own transcript, replayed from its own event log
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Had a look.", timeout=15000
    )

    # parent title › child, and the rail keeps the ancestor highlighted
    # (there is no row for the delegate itself)
    crumbs = page.locator(".crumbs .crumb:not(.back)")
    expect(crumbs).to_have_count(2)
    expect(crumbs.first).to_have_text("driller")
    expect(crumbs.last).to_have_text("scout")
    expect(page.locator(".rail .row.active")).to_contain_text("driller")

    crumbs.first.click()
    expect(page.locator(".delegate-bar")).to_have_count(0)
    expect(page.locator("header .session-name")).to_have_text("driller")
    expect(page.locator("textarea")).to_have_count(1)
    # the parent is drivable again, publish included
    expect(page.get_by_role("button", name="publish", exact=True)).to_have_count(1)


def test_an_answer_card_is_one_line_that_opens_its_delegate(page, server):
    """An answer is one line in the parent's transcript, whose it is and
    its gist; the whole of it is in the delegate's own view, which the
    card opens, and the way back is plain. The card keeps its height in
    a transcript long enough to scroll: a flex child that clips its
    overflow was squeezed to a sliver there."""
    page.set_viewport_size({"width": 1280, "height": 700})
    page.goto(f"{server}/?session=e2e-card")
    _title(server, "e2e-card", "carded")
    long = "\n\n".join(f"Paragraph {i} of a long reply." for i in range(40))
    _send(
        page,
        '!tool sessions {"action": "ask", "name": "scout", '
        '"task": "!text Found it, in detail."}\n'
        "!text " + json.dumps(long)[1:-1],
    )
    expect(
        page.locator(".rail .row", has_text="carded").locator(".waiting")
    ).to_be_visible(timeout=20000)
    _send(page, "!text " + json.dumps(long)[1:-1])
    card = page.locator("button.delegate-card")
    expect(card).to_be_visible(timeout=15000)
    expect(card).to_contain_text("scout")
    expect(card).to_contain_text("Found it, in detail.")
    expect(card).to_be_enabled(timeout=10000)
    card.scroll_into_view_if_needed()
    assert card.bounding_box()["height"] >= 24  # one whole line, not a sliver

    card.click()
    bar = page.locator(".delegate-bar")
    expect(bar).to_be_visible(timeout=10000)
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Found it, in detail.", timeout=15000
    )
    # back, from the header or from the bar
    page.locator(".crumb.back").click()
    expect(page.locator(".delegate-bar")).to_have_count(0, timeout=10000)
    expect(page.locator("header .session-name")).to_have_text("carded")
    page.locator("button.delegate-card").click()
    bar.locator("button.back").click()
    expect(page.locator(".delegate-bar")).to_have_count(0, timeout=10000)
    expect(page.locator("textarea")).to_have_count(1)


def test_a_swept_delegates_card_unfolds_its_answer_in_place(browser, tmp_path_factory):
    """Once the retention sweep takes a delegate there is no view to
    open, and opening its name would make a new, empty session of it.
    So its card says it expired and unfolds the answer it left in the
    parent's transcript instead. The sweep runs when the studio starts,
    so a restart past a tiny TTL takes it."""
    env = {
        "NONTAINER_STUDIO_DELEGATE_TTL": "0.0002",
        "NONTAINER_STUDIO_DELEGATE_WAKES": "0",
    }
    first = _studio(tmp_path_factory, **env)
    server = next(first)
    page = browser.new_page()
    try:
        page.goto(f"{server}/?session=e2e-swept")
        _send(
            page,
            '!tool sessions {"action": "ask", "name": "scout", '
            '"task": "!text Found it before the sweep."}\n'
            "!text Sent a scout.",
        )
        expect(page.locator(".rail .waiting")).to_be_visible(timeout=20000)
        _send(page, "what did the scout say?")
        expect(page.locator("button.delegate-card")).to_be_visible(timeout=15000)
    finally:
        page.close()
        next(first, None)  # stop it

    time.sleep(1.0)  # past the TTL
    second = _studio(tmp_path_factory, store=server.store, **env)
    server = next(second)
    page = browser.new_page()
    try:
        page.goto(f"{server}/?session=e2e-swept")
        card = page.locator("details.delegate-card.gone")
        expect(card).to_be_visible(timeout=15000)
        expect(card).to_contain_text("expired")
        expect(card).to_contain_text("Found it before the sweep.")
        expect(page.locator("button.delegate-card")).to_have_count(0)
        card.locator("summary").click()
        expect(card.locator(".delegate-answer")).to_contain_text(
            "Found it before the sweep."
        )
        # and nothing made a session of the swept name
        assert "e2e-swept.scout" not in page.evaluate(
            "fetch('/api/sessions').then(r => r.text())"
        )
    finally:
        page.close()
        next(second, None)


def test_a_card_whose_delegate_went_away_does_not_open_it(page, server):
    """The sweep takes a delegate on a timer, and nothing tells an open
    transcript, so its card can still look openable. Opening a name that
    is gone would make a new, empty session of it; the card asks the
    server first, and finding the delegate gone, unfolds in place."""
    page.goto(f"{server}/?session=e2e-stale")
    _send(
        page,
        '!tool sessions {"action": "ask", "name": "scout", '
        '"task": "!text Found it, then went away."}\n'
        "!text Sent a scout.",
    )
    _title(server, "e2e-stale", "staler")
    row = page.locator(".rail .row", has_text="staler")
    expect(row.locator(".waiting")).to_be_visible(timeout=20000)
    _send(page, "what did the scout say?")
    card = page.locator("button.delegate-card")
    expect(card).to_be_enabled(timeout=15000)

    # gone behind the transcript's back, as a sweep would take it
    urllib.request.urlopen(
        urllib.request.Request(
            f"{server}/api/sessions/e2e-stale.scout", method="DELETE"
        )
    ).read()

    card.click()
    gone = page.locator("details.delegate-card.gone")
    expect(gone).to_be_visible(timeout=10000)
    expect(gone).to_contain_text("Found it, then went away.")
    expect(page.locator(".delegate-bar")).to_have_count(0)
    names = [
        r["name"]
        for r in json.loads(urllib.request.urlopen(f"{server}/api/sessions").read())[
            "sessions"
        ]
    ]
    assert "e2e-stale.scout" not in names


def test_an_edit_before_the_ask_takes_the_delegate_out_of_view(page, server):
    """Rewinding to a message from before a delegate was asked for
    unsays it, as it unsays the agent's own turns after that message:
    its card leaves the strip, the rail's badge goes, and the edited
    turn is not handed its answer."""
    page.goto(f"{server}/?session=e2e-undo")
    _title(server, "e2e-undo", "undoer")
    _send(page, "!text Nothing delegated yet.")
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Nothing delegated yet.", timeout=15000
    )
    _send(
        page,
        '!tool sessions {"action": "ask", "name": "scout", '
        '"task": "!text Found it."}\n!text Sent a scout.',
    )
    row = page.locator(".rail .row", has_text="undoer")
    expect(row.locator(".waiting")).to_be_visible(timeout=20000)
    card = page.locator(".delegate-strip .card", has_text="scout")
    expect(card).to_be_visible(timeout=15000)

    first = page.locator(".user-row").first
    first.hover()
    first.locator(".edit").click()
    page.locator(".edit-box textarea").fill("!text Edited, before any delegate.")
    page.locator(".edit-actions .send").click()
    expect(page.locator(".agent-msg .bubble").last).to_contain_text(
        "Edited, before any delegate.", timeout=15000
    )
    expect(page.locator(".delegate-strip")).to_have_count(0, timeout=10000)
    expect(row.locator(".waiting")).to_have_count(0, timeout=10000)
    expect(page.locator(".delegate-card")).to_have_count(0)


def test_the_rail_listing_opens_a_delegate(page, server):
    """The way in: the ⑂ listing's names are the drill-down's handles."""
    page.goto(f"{server}/?session=e2e-open")
    _send(
        page,
        '!tool sessions {"action": "ask", "name": "scout", '
        '"task": "!text Looked around."}\n'
        "!text Off it went.",
    )
    _title(server, "e2e-open", "opener")
    row = page.locator(".rail .row", has_text="opener")
    expect(row.locator(".dot.answered")).to_be_visible(timeout=20000)

    row.hover()  # the count is in the hover tray
    row.locator(".waiting").click()
    listed = page.locator(".rail .delegate")
    expect(listed).to_have_count(1, timeout=10000)
    listed.locator("button.delegate-name").click()

    expect(page.locator(".delegate-bar")).to_contain_text(
        "back to opener", timeout=20000
    )
    expect(page.locator(".crumbs .crumb").last).to_have_text("scout")


def test_deleting_the_parent_moves_the_view_off_its_delegate(page, server):
    """A delete takes the whole subtree, so the delegate on screen goes
    with the ancestor the rail still offers a delete for. The shell has
    to notice — and the deleted name must not survive in the URL, where
    a reload would ask for it back and get an empty session minted under
    it."""
    page.goto(f"{server}/?session=e2e-cascade")
    _send(
        page,
        '!tool sessions {"action": "ask", "name": "scout", '
        '"task": "!text Had a look."}\n'
        "!text Sent a scout.",
    )
    _title(server, "e2e-cascade", "doomed")
    row = page.locator(".rail .row", has_text="doomed")
    expect(row.locator(".dot.answered")).to_be_visible(timeout=20000)

    page.goto(f"{server}/?session=e2e-cascade.scout")
    expect(page.locator(".delegate-bar")).to_be_visible(timeout=20000)

    # the rail still shows the parent (the delegate has no row of its
    # own), and its delete is two taps
    row = page.locator(".rail .row", has_text="doomed")
    row.hover()  # the delete is in the hover tray
    row.locator("button.delete").click()
    row.locator("button.delete").click()

    expect(page.locator(".rail .row", has_text="doomed")).to_have_count(
        0, timeout=20000
    )
    # the view moved to a session that still exists
    expect(page.locator(".delegate-bar")).to_have_count(0, timeout=20000)
    expect(page.locator("textarea")).to_have_count(1)
    assert "e2e-cascade" not in page.url


def test_a_compacted_conversation_shows_a_marker_that_opens_to_the_summary(
    browser, tmp_path_factory
):
    """Past the budget, the earlier turns reach the agent as one summary.
    The transcript keeps them, and a marker where the fold happened says
    how many turns it covers and opens to what the agent now remembers.
    It is an event in the log, so a reload shows it again."""
    started = _studio(tmp_path_factory, NONTAINER_STUDIO_COMPACT_TOKENS="1000")
    server = next(started)
    page = browser.new_page()
    try:
        page.goto(f"{server}/?session=e2e-compact")
        _send(page, "!text The first reply.")
        expect(page.locator(".agent-msg").last).to_contain_text(
            "The first reply.", timeout=15000
        )
        _send(page, "!text The second reply.")
        expect(page.locator(".agent-msg").last).to_contain_text(
            "The second reply.", timeout=15000
        )

        marker = page.locator("details.compaction")
        expect(marker).to_have_count(1)
        expect(marker.locator("summary")).to_contain_text("1 earlier turn summarized")
        expect(marker.locator(".compaction-summary")).to_be_hidden()
        marker.locator("summary").click()
        expect(marker.locator(".compaction-summary")).to_be_visible()
        expect(marker.locator(".compaction-summary")).not_to_be_empty()
        # the person still sees the turn the agent now has as a summary
        expect(page.locator(".agent-msg").first).to_contain_text("The first reply.")

        page.reload()
        expect(page.locator("details.compaction")).to_have_count(1, timeout=15000)
    finally:
        page.close()
        next(started, None)
