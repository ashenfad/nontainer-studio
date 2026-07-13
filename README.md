# nontainer-studio

A local AI workbench over [nontainer](https://github.com/ashenfad/nontainer):
chat with an agent that works inside a **versioned workspace** — files,
sandboxed Python, a live app preview — where every turn is a checkpoint
you can rewind, fork, or publish.

- **Edit = synchronized time travel.** Hover any of your messages and
  hit `edit`: the files, the agent's memory, and the transcript rewind
  together, and the revised prompt runs from there — everything below
  is replaced, and no post-rewind gaslighting where the agent remembers
  work the files no longer show.
- **Background sessions.** Turns run server-side, decoupled from the
  browser. Switch sessions, reload, or close the tab mid-turn; the work
  continues and the rail dots show what's running (pulsing) and what
  finished while you were away (green).
- **Live preview → publish.** Anything the agent writes under `/app`
  serves live in the preview pane as it takes shape. `publish` freezes
  the current state behind a capability URL — frozen code over the
  session's live `db`, while your session keeps moving.
- **Rich replies.** The agent can drop plots, tables, images, and HTML
  into its answers via `ui = {...}` — rendered inline, themed by the
  shell.
- **Fork = a new universe.** Branch a session from any checkpoint:
  O(1) workspace fork, copied app db, fresh conversation.

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
the published-app CSP; `none` disables), `NONTAINER_STUDIO_ISOLATION`
(`process` by default — agent code runs in a forked worker so a
segfault/OOM in C-extension guts costs the turn, not the server; the
workspace files, cache, and `db` stay host-side, bridged over RPC.
`kernel` adds syscall/network lockdown; `none` runs in-process).

> **Note:** nontainer isn't on PyPI yet — `pyproject.toml` currently
> points at a sibling `../nontainer` checkout.

## What owns what

Three kinds of state, on purpose:

| state | durability | restore | fork | publish |
|---|---|---|---|---|
| **workspace** (files, cache, cwd) | kvgit branch per session | rewinds | branches (O(1)) | frozen snapshot |
| **app `db`** (live SQLite host object) | file per session | untouched — external state has no history | copied | **shared** — frozen code, live state |
| **conversation** | agno db + jsonl transcript | agent memory rewinds in sync; an `edit` trims the visible transcript too, a history-tab restore keeps its record | fresh chat | — |

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
