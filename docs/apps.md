# Building apps

The app loop from the human's side. What a handler must look like — the
verb functions, the request and response shapes, `test_app` — is
nontainer's, and is documented in
[nontainer's apps guide](https://github.com/ashenfad/nontainer/blob/main/docs/apps.md).

## Live preview

Anything the agent writes under `/workspace/app` serves live in the
preview pane as it takes shape. Nothing is built and nothing is
published to see it: the pane follows the agent's writes, and a handler
runs on the same declaration a published one will run on.

Below the request tier the agent has `ws-pytest` and `ws-vitest` — one
Python function or one frontend module under test, so a failing assertion
names the broken piece where a blank page does not. The `building-apps`
skill seeded into each session is what teaches the loop.

## Publish

`publish` freezes `/workspace/app` as a **version** of an **app**: a
capability URL that keeps serving while your session keeps moving.
Publishing again adds `v2` under the same URL, and the pointer moves back
as easily as forward with **make current** in the version list — the link
you handed someone never changes.

A session has one app; a second app is a fork's. The publish button
refuses while a turn is running, since a version assembled out of half a
turn is a state no commit ever held.

What is not yet in a version is never a guess. The button counts the app
files that differ from the app's **newest** version — `publish · 3 files`
— and the `changes` tab lists them and diffs any one of them against it.
When the link is serving an older version, the tab says so and offers to
diff against the version being served instead.

A version is `/workspace/app` and nothing else: the notes, the uploads
and the conversation stay behind. An app keeps serving over the session's
live `db`, so a session can be deleted without taking its app down. The
reasoning behind all of that is in [What owns what](design.md).

## Works offline

The libraries an agent's app uses are vendored into
`nontainer_studio/appassets/` and served from the app's own origin at
`vendor/`, so an app renders with no internet at all. That matters for a
locally-hosted model on an air-gapped machine, where a CDN
`<script src>` is a blank page.

What is in there: **MUI** (Material UI with `@mui/x-data-grid` and a
curated set of ~66 Material icons), **React**, **plotly**, **tailwind**,
a JSX loader of ours, and this shell's palette as a MUI theme — about
6.8 MB, of which plotly is 4.7. Every file, its source and its checksum
are listed in `nontainer_studio/appassets/README.md`.

The served policy says the same thing the vendoring does: an app's
scripts may load from its own origin and nowhere else, under `test_app`
and when published alike. The config declares no script hosts at all, so
a stray CDN tag fails where the agent can see it rather than working in
the preview and failing offline.

JSX is compiled in the browser (sucrase, 201 KB) rather than by a build
step the agent would have to run — it writes `app.jsx`, and stack traces
still point at its own lines. Components are MUI because that is where a
model's training mass is: it writes `<Button variant="contained">` from
memory, and imports bare names (`@mui/material`, `react`) exactly as in
any React project.

The bytes are committed, like the frontend build, so nothing is fetched
at install or run time. They stay out of the workspace — no session, fork
or published version carries a copy — and the agent is told what it has
in the terminal tool's description rather than being left to guess.

`./scripts/fetch-appassets.sh` regenerates them (it needs node; users
never do). Swap the whole directory with `NONTAINER_STUDIO_APP_ASSETS` —
and if you do, update `FRONTEND_NOTES` in `nontainer_studio/sessions.py`
so the agent is told about *your* libraries. The bytes and the sentence
describing them are one decision.

## Under a dud executor

With `NONTAINER_STUDIO_EXECUTOR` set to `dud` or `dud-vm` (see
[Configuration](configuration.md)), the terminal is real bash — GNU
tools, command substitution — rather than the emulated shell.

The workspace's own verbs come along. `ws-curl`, `ws-git`, `ws-pytest`
and `ws-vitest` are relayed out of the guest and answered on the host, so
the apps loop, the session's git and the unit-test tier read the same on
every rung. They are spelled `ws-` for that reason: real `curl` is on the
guest's PATH and would reach the network, where `ws-curl` reaches the
app.

The live preview and `test_app` both drive dispatch host-side, so they
run under dud unchanged, and the handler pattern the apps guide
recommends — state in `cache` or an external store — crosses the
boundary cleanly. Absolute paths are the one thing that differs by
rung: `dud-vm` mounts the workspace at `/workspace`, so they mean what
they mean in the local sandbox, while `dud` keeps the workspace in a
temp directory of the subprocess's own, so an agent on that rung should
work in relative paths from its cwd.
