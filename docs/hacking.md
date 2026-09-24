# Hacking

## The frontend dev loop

Svelte 5 + Vite in `frontend/`, built into `nontainer_studio/static/` and
committed, so users never need node:

```sh
uv run nontainer-studio            # backend on :8321
cd frontend && npm install
npm run dev                        # hot reload on :5173, API proxied
npm run build                      # refresh the committed bundle
```

## The architecture map

The UI keeps a **runtime per session**
(`frontend/src/lib/runtime.svelte.js`), each following its own SSE cursor
into the server's per-session event log. The shell is a projection of the
foreground runtime — that is what makes background turns and instant
session switching work.

The server-side halves:

| module | what it holds |
|---|---|
| `nontainer_studio/server.py` | the Starlette routes, the agno-stream → event mapping, the a2ui projection |
| `nontainer_studio/sessions.py` | the registry: session open/fork/delete, synchronized rewind, publish, the app manifest, the durable transcript, the agent build |
| `nontainer_studio/delegates.py` | `StudioRunner` — the loop that drives a forked session to an answer |
| `nontainer_studio/compression.py` | tool-result compression that keeps a mid-run human message verbatim |
| `nontainer_studio/summaries.py` | the stateless second pass that names a session and describes an app |
| `nontainer_studio/providers.py` | which model backends are available, model specs, thinking and context settings |
| `nontainer_studio/dummy.py` | the scripted model the tests (and you) can drive by hand |

## HTTP routes

`{name}` is a session slug and `{token}` a publication token. Verbs
marked **human** answer 409 for a delegate: the parent agent writes its
prompts and judges its branch, so a human turn landing in the middle
would rewrite a transcript the parent is still reading. Reading verbs,
`fork` and `delete` are untouched.

| path | method | what |
|---|---|---|
| `/` | GET | the built frontend's `index.html` |
| `/api/models` | GET | providers whose key and SDK are present, with their curated model picks |
| `/api/sessions` | GET | every session the rail shows |
| `/api/sessions` | POST | create-or-return. With no `name`, mint one — identity is always a slug nobody typed |
| `/api/sessions/{name}` | GET | what one name IS; `delegate` names the parent, or is null |
| `/api/sessions/{name}` | DELETE | delete the session and everything it owns; 409 while a turn runs |
| `/api/sessions/{name}/model` | POST | **human** — switch this session's model; memory carries over. 409 while a turn runs |
| `/api/sessions/{name}/title` | POST | **human** — the human's rename; a blank title clears the override |
| `/api/sessions/{name}/chat` | POST | **human** — send a message. With a turn running it is queued instead: 202, with the note's id |
| `/api/sessions/{name}/queue/{id}` | DELETE | **human** — withdraw a queued message, while it is still pending; 404 once delivered |
| `/api/sessions/{name}/edit` | POST | **human** — rewind files, memory and transcript to before a turn, then run the edited message |
| `/api/sessions/{name}/cancel` | POST | stop the running turn gracefully; the run persists with its partial work |
| `/api/sessions/{name}/events` | GET | the transcript feed: SSE replaying from `?since=N` then following live, or `?wait=0` for a JSON snapshot |
| `/api/sessions/{name}/a2ui` | GET | the same feed projected into A2UI v0.9 (see [What owns what](design.md)) |
| `/api/sessions/{name}/upload` | POST | **human** — write `?name=` into `uploads/`; 413 past 50 MB |
| `/api/sessions/{name}/files` | GET | every file in the workspace, walked |
| `/api/sessions/{name}/file` | GET | one file's raw bytes by `?path=`, with a guessed media type |
| `/api/sessions/{name}/app` | GET | the preview pane's probe: does this session have an `app/` tree |
| `/api/sessions/{name}/publish` | POST | **human** — publish a new version of the session's app, starting it if this is the first |
| `/api/sessions/{name}/apps` | GET | this session's apps, each with how far its files have moved since the app's newest version |
| `/api/sessions/{name}/apps/{token}/changes/file` | GET | one app file's two sides: `?path=` as `?since=` holds it, and as the session holds it now |
| `/api/sessions/{name}/restore` | POST | **human** — rewind to one of this session's own publishes; no new turn is started |
| `/api/sessions/{name}/fork` | POST | branch into a new session; `{"conversation": "fresh"}` drops the memory and transcript |
| `/api/sessions/{name}/delegates` | GET | what this session delegated and what became of each — the listing behind the rail's ⑂ badge |
| `/api/sessions/{name}/delegates/{child}/keep` | POST | exempt a delegate's branch from the retention sweep; `{"kept": false}` un-keeps |
| `/api/apps` | GET | every published app |
| `/api/apps/{token}/current` | POST | repoint an app's URL at one of its versions |
| `/api/apps/{token}/description` | POST | the human's own words for an app; blank clears them |
| `/api/apps/{token}/versions/{version}/branch` | POST | fork the origin session and rewind the child to this version's commit; 404 when the origin session is gone |
| `/api/apps/{token}/versions/{version}` | DELETE | delete one version |
| `/api/apps/{token}` | DELETE | unpublish: the versions, the database and the link stop working |
| `/api/{path:path}` | any | the fallback: an app in the preview iframe using absolute URLs lands here and gets a CORS-readable teaching 404 |
| `/preview/{name}` and `/preview/{name}/{path:path}` | any | the session's live `app/` tree, dispatched through its handlers |
| `/apps/…` | any | published snapshots: read-only, concurrent, token-addressed, under the apps CSP |
| `/static/…` | GET | the built frontend's assets |

The app-side routes need no human-only rule: they are addressed by
publication token, and moving a pointer, taking an app down or deleting a
version reaches no session at all.

## The event log

One append-only log per session. The SSE feed replays it from a cursor
and then follows live, so every client — the shell, a reload, the a2ui
projection — reconstructs the same transcript from the same events. Every
event carries its sequence number as its cursor.

| type | fields | when |
|---|---|---|
| `user` | `text`, `head`, sometimes `from_queue` | a turn starts. `head` is the workspace commit BEFORE the turn — the undo anchor an `edit` checks out. `from_queue` names the queued messages this turn was started with |
| `interject` | `id`, `text` | a message you queued mid-turn has just been handed to the model. No `head`: the turn it landed in began before you said it, so it is not an edit anchor |
| `delegate` | `name`, `status`, `text` | a delegate's answer reached the model, or a note about one a restart orphaned (`status: "unanswered"`) |
| `text` | `delta` | a chunk of the agent's prose |
| `thinking` | `delta` | a chunk of reasoning, from native model thinking or agno's reasoning manager |
| `tool_start` | `name`, `args` | a tool call begins; `args` are shaped and shortened for display |
| `tool_end` | `name`, `result` | a tool call returns. A mid-run message is cut back out of the result first, so the tool box shows the tool's own output |
| `artifact` | `name`, `path`, `kind` | a `ui = {...}` value became a file; parsed from the raw tool result, so a long one does not truncate the note away |
| `notice` | `text` | turn stopped, model switched, upload written, compression started or finished, a provider error the turn is resuming from |
| `usage` | `input_tokens`, `cached_tokens` | context telemetry, one per model call; the frontend keeps only the latest |
| `error` | `message` | the run errored and its one resume did not clear it, the run loop raised, or the studio shut down on a turn it could not wait out |
| `done` | `run_id`, `head` | the turn ended. `head` is the workspace at turn end — the commit ↔ conversation mapping a rewind needs |
| `title` | `title`, plus what was stored | the session was named or renamed |
| `publish` | `token`, `version`, `title`, `url`, `head`, `tree` | a version exists. Emitted only after the fact, since it is a durable landmark you can restore to |
| `truncate` | `to` | an edit or a restore cut the transcript. The log stays append-only; projections apply the cut |

## Tests

```sh
uv sync --extra dev
uv run playwright install chromium   # once, for the browser tests
uv run pytest
```

No LLM key is needed anywhere in the suite.

- `test_server.py` drives the server plumbing — workspaces, forks,
  publish, restore, the queue, compression — against a `FakeAgent` that
  stands in for the whole agent. Subclasses of it script the specific
  behaviour a test needs (a thinking agent, an artifact agent, one that
  hangs until a gate opens).
- `test_e2e.py` runs the whole stack in a real browser — uvicorn, SSE,
  the built frontend, agno's run loop, real tools — with only the model
  scripted. It needs the committed frontend build and the Chromium
  install above, and skips cleanly without either.
- `test_delegates.py`, `test_providers.py` and `test_summaries.py` cover
  the delegation loop, provider and spec handling, and the generated
  title and description.

The dummy model reads its script out of the user message, so there is no
side channel between the test process and the server:

```
!think Hmm, let me consider this.
!tool file_write {"path": "/notes.md", "content": "hi"}
!fail provider overloaded
!text Here is your reply.
```

One model turn emits the `!tool` calls if there are any that have not run
yet — the real loop executes them and reinvokes — and otherwise emits the
`!text` reply, streamed in two deltas to exercise the streaming path. A
message with no directives echoes back.

`!fail` makes the reply call raise agno's `ModelProviderError` with the
rest of the line as its message, the way a provider failure reaches the
run. Each `!fail` line is spent on one call, in order, and the call after
the last one answers with `!text` — so the script above writes the file,
fails once, and answers when the turn resumes. The tests build the model
without the server's model-call retry, where one `!fail` ends the run;
under the server's own model, which retries a failed call twice, three
lines are what get past it.

That makes it useful interactively too: run the server with
`NONTAINER_STUDIO_MODEL=dummy` and type directives to puppet the agent
through the real stack.
