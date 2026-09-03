# nontainer-studio

A local AI workbench over [nontainer](https://github.com/ashenfad/nontainer):
chat with an agent that works inside a **versioned workspace** — files,
sandboxed Python, a live app preview — where every turn is a checkpoint
you can rewind, fork, or publish.

- **Edit = synchronized time travel.** Hover any of your messages and
  hit `edit`: the files, the agent's memory, and the transcript rewind
  together, and the revised prompt runs from there — everything below
  is replaced, and no post-rewind gaslighting where the agent remembers
  work the files no longer show. They rewind together because they are
  one thing: the conversation lives in the same versioned branch as the
  files, so the rewind is a single `restore`.
- **Background sessions.** Turns run server-side, decoupled from the
  browser. Switch sessions, reload, or close the tab mid-turn; the work
  continues and the rail dots show what's running (pulsing) and what
  finished while you were away (green).
- **Live preview → publish.** Anything the agent writes under
  `/workspace/app` serves live in the preview pane as it takes shape.
  `publish` freezes the current state as a **version** of an **app**: a
  capability URL that keeps serving while your session keeps moving.
  Publishing again adds `v2` under the same URL, and the pointer moves
  back as easily as forward. An app owns its `db` from its first
  version, and its versions are store-scoped tags — so a session can be
  deleted without taking its apps down.
- **Rich replies.** The agent can drop plots, tables, images, and HTML
  into its answers via `ui = {...}` — rendered inline, themed by the
  shell.
- **Fork = a new universe.** Branch a session in one O(1) kvgit
  operation that carries the files, the cache, the cwd **and** the
  conversation. `inherit` (the rail's ⑂) opens the child exactly where
  the parent stands — same transcript, same memory, its own universe
  from there; `fresh` keeps the files and starts the chat over. The app
  db is copied either way, since live state has no history.

Demo, not product: single-user, localhost, no auth.

## Run it

```sh
git clone https://github.com/ashenfad/nontainer-studio
cd nontainer-studio
uv sync
ANTHROPIC_API_KEY=... uv run nontainer-studio
# → http://127.0.0.1:8321
```

The frontend is a committed build — no node needed to run.

### Models & providers

Providers are **detected from the environment** — whichever keys are set
show up in the in-app model picker (keys never reach the browser):
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`,
`GOOGLE_API_KEY` (needs `pip install google-genai`), `OLLAMA_HOST`
(needs `pip install ollama`).

Each session has its own model, switchable mid-conversation — chat
memory is keyed by session, so the new model inherits the whole
conversation. The default comes from `NONTAINER_STUDIO_MODEL`:
`provider:model` (`openrouter:deepseek/deepseek-v4-flash`), a bare
provider (`openrouter` — its default model), or `dummy` (the scripted
test model). Unset, it's the first available of anthropic → openai →
openrouter → google → ollama.

OpenRouter specs take an optional `@slug[/quant]` tag to pin the
upstream provider: `openrouter:qwen/qwen3.6-35b-a3b@wandb/fp8` routes
to Weights & Biases at fp8, no fallbacks. Works anywhere a spec does —
the env default or the picker's custom field.

A `.env` file next to where you launch is loaded at startup (real env
wins) — see `.env.example`.

Other knobs: `NONTAINER_STUDIO_PORT`, `NONTAINER_STUDIO_STORE`
(defaults to `~/.nontainer-studio`), `NONTAINER_STUDIO_CSP` (override
the published-app CSP; `none` disables), `NONTAINER_STUDIO_SKILLS`
(directory of starter skills seeded into new sessions; defaults to the
repo's `skills/`), `NONTAINER_STUDIO_APP_ASSETS` (directory of browser
libraries served to agent-authored apps at `vendor/`; defaults to the
repo's `nontainer_studio/appassets/` — see **Works offline** below),
`NONTAINER_STUDIO_COMPRESS_TOKENS` (context-
compression watermark), `NONTAINER_STUDIO_ISOLATION` (`process` by
default — agent code runs in a worker process of its own so a
segfault/OOM in C-extension guts costs the turn, not the server; the
workspace files, cache, and `db` stay host-side, bridged over RPC.
`kernel` adds syscall/network lockdown; `none` runs in-process), and
`NONTAINER_STUDIO_VIEW_WORKERS` (default 0 — how many app-handler
workers to keep warm per view. Studio preloads the granted data stack
into sandtrap's forkserver broker, which puts a pristine worker at
roughly 12ms, so 0 buys clean per-request process state for about the
price of reusing one. Raise it only if a published app serves real
concurrency).

### Works offline

The libraries an agent's app uses — **MUI** (with React and JSX),
**plotly** and **tailwind** — are vendored into
`nontainer_studio/appassets/` and served from the app's own origin at
`vendor/`, so an app renders with no internet at all. That matters for a
locally-hosted model on an air-gapped machine, where a CDN
`<script src>` is a blank page.

JSX is compiled in the browser (sucrase, 201 KB) rather than by a build
step the agent would have to run — it writes `app.jsx`, and stack traces
still point at its own lines. Components are MUI because that is where a
model's training mass is: it writes `<Button variant="contained">` from
memory.

The bytes are committed, like the frontend build, so nothing is fetched at
install or run time. They stay out of the workspace — no session, fork, or
published snapshot carries a copy — and the agent is told what it has in
the terminal tool's description rather than being left to guess.

`./scripts/fetch-appassets.sh` regenerates them (pinned versions and
checksums in `nontainer_studio/appassets/README.md`). Swap the whole
directory with `NONTAINER_STUDIO_APP_ASSETS` — and if you do, update
`FRONTEND_NOTES` in `nontainer_studio/sessions.py` so the agent is told
about *your* libraries. The bytes and the sentence describing them are one
decision.

### Where agent code runs

By default the agent's Python and shell run **in-process**, gated by
[sandtrap](https://github.com/ashenfad/sandtrap) — a walled garden for
cooperative code, tuned by `NONTAINER_STUDIO_ISOLATION` above.

`NONTAINER_STUDIO_EXECUTOR` swaps that for a real machine via
[dud](https://github.com/ashenfad/dud) (needs the `dud` extra and
Python 3.11+):

| value | what runs the code | isolation |
|---|---|---|
| unset (default) | in-process sandbox | sandtrap's gates |
| `dud-vm` | a disposable microVM — vfkit on macOS, firecracker on Linux/KVM | real |
| `dud` | a host process — real bash, real files | **none** |

```sh
uv sync --extra dud
NONTAINER_STUDIO_EXECUTOR=dud-vm uv run nontainer-studio
```

`dud-vm` boots a `python:slim` guest matched to your interpreter, with
the data stack layered in; the first run builds and caches the image
(~40s), later runs and restarts reuse it. Warm VMs are pooled —
`NONTAINER_STUDIO_VM_WARM` (default 1) sets how many to pre-boot at
startup, `DUD_VM_MAX_TOTAL` (default 4) caps running VMs, and
`NONTAINER_STUDIO_VM_MEDIUM` overrides the rootfs medium (`auto`
picks erofs for big images — smaller RAM, faster boot).

Take `=dud` seriously: it's real bash and real files with **no
containment at all**, running as your user with your network. It buys
fidelity for development, not a boundary — the server warns at startup.

What changes under either: the terminal is real bash (GNU tools,
command substitution) rather than the emulated shell, so the apps
loop's `curl` builtin doesn't exist — the agent is told as much and
uses `test_app` and the preview instead.

## What owns what

Three kinds of state, on purpose:

| state | durability | restore | fork | publish |
|---|---|---|---|---|
| **workspace** (files, cache, cwd) | kvgit branch per session | rewinds | branches (O(1)) | a version — a store-scoped tag, frozen and read-only |
| **app `db`** (live SQLite host object) | file per session | untouched — external state has no history | copied | copied ONCE, at the app's first version; the app owns it from then on |
| **conversation** | agno's session in the same kvgit branch (+ a jsonl transcript) | rewinds with the files — one `restore`, not two writes that can disagree; an `edit` trims the visible transcript too | `inherit` or `fresh` | a marker in the transcript you can restore to, or branch from |

An **app** is a publication lineage: one URL, one `db`, and a growing
list of versions. The URL serves whichever version is *current*, so
publishing moves it forward and `make current` moves it back — the link
you handed someone never changes. Versions are store-scoped nontainer
tags, the scope that outlives a branch, so nothing an app serves depends
on the session that built it still existing.

The `db` copy is what makes that true of the *state* as well as the
code. A published app's users write rows; so does the session's live
preview; sharing one file would mean deleting the session took the app's
data with it, and every rewind of the conversation happened underneath
strangers. So the app takes a copy at its first version and owns it —
and a later version of that app therefore meets whatever schema the
previous one left (`CREATE TABLE IF NOT EXISTS`, tolerant reads; the
agent is told).

agno's cross-session tables — user memories, metrics — sit at
`store/agno` and never version: a memory spans conversations, so it
must not rewind with any one branch. Conversations from before the
move into the branch (the old `store/chat.sqlite`) are not carried
over; those sessions keep their files and start with an empty memory.

### a2ui egress

`GET /api/sessions/{name}/a2ui` projects the transcript into an
[A2UI](https://a2ui.org) v0.9 message stream — an edge format for
declarative agent UIs, never the internal model (the event log stays
that). The projection is **turn-level**: each turn becomes one surface
(its prose interleaved with its artifacts), an `edit`'s rewind emits
`deleteSurface` to void the turns it cut. Same shape as `/events` — SSE
that replays from `?since=N` then follows live, or a `?wait=0` JSON
snapshot — and every message rides the driving event's cursor so
consumers resume identically. Plotly specs ride an extension `Chart`
component (the consumer brings the renderer); the rest map to basic-
catalog `Text`/`Image`/`Card`/`Row`/`Column`.

## Hacking on the frontend

Svelte 5 + Vite in `frontend/`, built into `nontainer_studio/static/`
(committed, so users never need node):

```sh
uv run nontainer-studio            # backend on :8321
cd frontend && npm install
npm run dev                        # hot reload on :5173, API proxied
npm run build                      # refresh the committed bundle
```

The architecture note worth knowing: the UI keeps a **runtime per
session** (`frontend/src/lib/runtime.svelte.js`), each following its own
SSE cursor into the server's per-session event log. The shell is just a
projection of the foreground runtime — that's what makes background
turns and instant session switching work. The server-side halves live in
`nontainer_studio/server.py` (routes, agno-stream → event mapping) and
`nontainer_studio/sessions.py` (registry, synchronized restore, publish,
durable transcript).

## Tests

```sh
uv sync --extra dev
uv run pytest
```

No LLM key needed anywhere in the suite:

- `test_server.py` drives the server plumbing (workspaces, forks,
  publish, restore) with a fake agent.
- `test_e2e.py` runs the whole stack in a real browser — uvicorn, SSE,
  the built frontend, agno's run loop, real tools — with only the model
  scripted (`NONTAINER_STUDIO_MODEL=dummy`; see `nontainer_studio/dummy.py`
  for the `!tool` / `!text` directive DSL). Needs the committed frontend
  build and `playwright install chromium`; skips cleanly otherwise.

The dummy model is also handy interactively: run the server with
`NONTAINER_STUDIO_MODEL=dummy` and type directives to puppet the agent.
