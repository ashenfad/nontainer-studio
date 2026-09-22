# nontainer-studio

A local AI workbench over [nontainer](https://github.com/ashenfad/nontainer):
chat with an agent that works inside a **versioned workspace** — files,
sandboxed Python, a live app preview — where every turn is a commit you
can rewind, fork, or publish. The conversation lives in the same kvgit
branch as the files, so the two never disagree about what happened.

- **Edit = synchronized rewind.** Hover any of your messages and hit
  `edit`: files, the agent's memory and the transcript rewind together —
  one `checkout`, not three writes that can drift apart.
- **Background turns.** Turns run server-side, so you can switch
  sessions, reload, or close the tab mid-turn and the work carries on.
- **Live preview → publish.** Anything the agent writes under
  `/workspace/app` serves live; `publish` freezes that tree as a version
  behind a URL that keeps serving while the session moves on.
- **Queue a message while it works.** The composer stays open, and a
  message sent mid-turn reaches the agent appended to its next tool
  result rather than interrupting it.
- **Fork = a new universe.** One O(1) kvgit branch carries the files, the
  cache, the cwd **and** the conversation.
- **Delegate = a fork with an agent on it.** The agent hands a task to a
  fork of itself and collects the answer a turn or two later; nothing the
  delegate writes touches your files until the parent merges it.

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

Providers are **detected from the environment**: whichever keys are set
show up in the in-app model picker, and the keys themselves never reach
the browser. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`,
`GOOGLE_API_KEY` (needs `pip install google-genai`) and `OLLAMA_HOST`
(needs `pip install ollama`) are the five, tried in that order when
nothing is configured. Each session has its own model, switchable
mid-conversation — chat memory is keyed by session, so the new model
inherits the whole conversation. `NONTAINER_STUDIO_MODEL` sets the
default, and a `.env` beside where you launch is loaded at startup.

## Docs

- [Configuration](https://github.com/ashenfad/nontainer-studio/blob/main/docs/configuration.md)
  — every environment knob: models and providers, server and store,
  where agent code runs, what the agent is allowed to do, delegation caps.
- [Apps](https://github.com/ashenfad/nontainer-studio/blob/main/docs/apps.md)
  — the app-building loop: live preview, publish, versions, the `changes`
  tab, the unit-test tier, and the vendored libraries that make it work
  offline.
- [What owns what](https://github.com/ashenfad/nontainer-studio/blob/main/docs/design.md)
  — the three kinds of state and why each behaves as it does: the
  workspace, the app `db`, the conversation.
- [Delegation](https://github.com/ashenfad/nontainer-studio/blob/main/docs/delegation.md)
  — the `sessions` tool, how a delegate's work comes back, and the
  budget, depth and retention caps around it.
- [Hacking](https://github.com/ashenfad/nontainer-studio/blob/main/docs/hacking.md)
  — the frontend dev loop, the architecture map, the HTTP routes, the
  event log's event types, and the tests.
- [Docs index](https://github.com/ashenfad/nontainer-studio/blob/main/docs/README.md)
  — the same list, plus what is still undocumented.

## Tests

```sh
uv sync --extra dev
uv run playwright install chromium   # once, for the browser tests
uv run pytest
```

No LLM key is needed anywhere in the suite: the server tests drive a fake
agent, and the end-to-end browser tests script only the model
(`NONTAINER_STUDIO_MODEL=dummy`). See
[Hacking](https://github.com/ashenfad/nontainer-studio/blob/main/docs/hacking.md)
for what each test file covers.

## Hacking on the frontend

Svelte 5 + Vite in `frontend/`, built into `nontainer_studio/static/` and
committed so users never need node. The dev loop and the architecture map
are in
[Hacking](https://github.com/ashenfad/nontainer-studio/blob/main/docs/hacking.md).
