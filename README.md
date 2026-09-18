# nontainer-studio

A local AI workbench over [nontainer](https://github.com/ashenfad/nontainer):
chat with an agent that works inside a **versioned workspace** — files,
sandboxed Python, a live app preview — where every turn is a commit
you can rewind, fork, or publish.

- **Edit = synchronized time travel.** Hover any of your messages and
  hit `edit`: the files, the agent's memory, and the transcript rewind
  together, and the revised prompt runs from there — everything below
  is replaced, and no post-rewind gaslighting where the agent remembers
  work the files no longer show. They rewind together because they are
  one thing: the conversation lives in the same versioned branch as the
  files, so the rewind is a single `checkout`. History is append-only:
  what you rewound off is still in the branch, so an undo can be undone.
- **Background sessions.** Turns run server-side, decoupled from the
  browser. Switch sessions, reload, or close the tab mid-turn; the work
  continues and the rail dots show what's running (pulsing) and what
  finished while you were away (green).
- **Live preview → publish.** Anything the agent writes under
  `/workspace/app` serves live in the preview pane as it takes shape.
  `publish` freezes that tree as a **version** of an **app**: a
  capability URL that keeps serving while your session keeps moving.
  Publishing again adds `v2` under the same URL, and the pointer moves
  back as easily as forward. A session has one app; a second app is a
  fork's. A version is `/workspace/app` and nothing else — the notes,
  the uploads and the conversation stay behind — and an app keeps
  serving over the session's live `db`, so a session can be deleted
  without taking its app down. Below the request tier the agent has
  `ws-pytest` and `ws-vitest` — one function or one frontend module
  under test, so a failing assertion names the broken piece where a
  blank page doesn't.
- **Rich replies.** The agent can drop plots, tables, images, and HTML
  into its answers via `ui = {...}` — rendered inline, themed by the
  shell.
- **Fork = a new universe.** Branch a session in one O(1) kvgit
  operation that carries the files, the cache, the cwd **and** the
  conversation. `inherit` (the rail's ⑂) opens the child exactly where
  the parent stands — same transcript, same memory, its own universe
  from there; `fresh` keeps the files and starts the chat over. The app
  db is not copied either way: it is a handle to an external store,
  and both universes write to the one file.
- **Delegate = a fork with an agent on it.** The agent can hand a task
  to a fork of itself and collect the answer a turn or two later. The
  delegate works on a branch of its own, so nothing it writes touches
  your files until the parent merges it — in the terminal, by name.

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

`NONTAINER_STUDIO_SUMMARY_MODEL` picks the model the studio names
sessions and describes published apps with — a second, tiny run over
the transcript, on the session's own model unless this says otherwise.
Naming a conversation is a job a small, cheap model does as well as the
one doing the building.

Other knobs: `NONTAINER_STUDIO_PORT`, `NONTAINER_STUDIO_STORE`
(defaults to `~/.nontainer-studio`), `NONTAINER_STUDIO_CSP` (override
the published-app CSP; `none` disables), `NONTAINER_STUDIO_SKILLS`
(directory of starter skills seeded into new sessions; defaults to the
repo's `skills/`, and a skill about a feature a knob withholds is
seeded only when that knob is on), `NONTAINER_STUDIO_APP_ASSETS` (directory of browser
libraries served to agent-authored apps at `vendor/`; defaults to the
repo's `nontainer_studio/appassets/` — see **Works offline** below),
`NONTAINER_STUDIO_COMPRESS_TOKENS` (context-
compression watermark), `NONTAINER_STUDIO_DELEGATE_TTL` (hours a
delegate's branch is kept after anyone last dealt with it; 24 by
default, `0` turns the sweep off — see **Delegation** below),
`NONTAINER_STUDIO_WSGIT` (give the agent the `ws-git` terminal verb —
**off by default for now**, while the app-building path is polished;
the human's rewind, fork, publish and restore are host-side and work
either way), `NONTAINER_STUDIO_SESSIONS` (give the agent the `sessions`
tool, so it can delegate and list published apps — **off by default for
now**, for the same reason; the delegates rail, the drill-down and the
retention sweep stay wired),
`NONTAINER_STUDIO_ISOLATION` (`process` by default — agent code runs in
a worker process of its own so a segfault/OOM in C-extension guts costs
the turn, not the server; the
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
`<script src>` is a blank page. The served policy says the same: an
app's scripts may load from its own origin and nowhere else, under
test_app and when published, so a stray CDN tag fails where the agent
can see it rather than working in the preview and failing offline.

JSX is compiled in the browser (sucrase, 201 KB) rather than by a build
step the agent would have to run — it writes `app.jsx`, and stack traces
still point at its own lines. Components are MUI because that is where a
model's training mass is: it writes `<Button variant="contained">` from
memory.

The bytes are committed, like the frontend build, so nothing is fetched at
install or run time. They stay out of the workspace — no session, fork, or
published version carries a copy — and the agent is told what it has in
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
command substitution) rather than the emulated shell. The workspace's
own verbs come along — `ws-curl`, `ws-git`, `ws-pytest` and `ws-vitest`
are relayed out of the guest and answered on the host, so the apps
loop, the session's git and the unit-test tier read the same on every
rung. They are spelled `ws-` for that reason: real `curl` is on the
guest's PATH and would reach the network, and `ws-curl` reaches the
app.

## What owns what

Three kinds of state, on purpose:

| state | durability | restore | fork | publish |
|---|---|---|---|---|
| **workspace** (files, cache, cwd) | kvgit branch per session | rewinds | branches (O(1)) | `app/` only — a publication version, frozen and read-only |
| **app `db`** (live SQLite host object) | one file under an id of its own, named by every row that shares it | untouched — external state has no history | named, not copied (a delegate's too) | named; every version of the app serves over it |
| **conversation** | agno's session in the same kvgit branch (+ a jsonl transcript) | rewinds with the files — one `checkout`, not two writes that can disagree; an `edit` trims the visible transcript too | `inherit` or `fresh` | a marker in the transcript you can restore to, or branch from |

An **app** is a nontainer **publication**: one URL, one `db`, and a
growing list of versions. The URL serves whichever version is *current*,
so publishing moves it forward and `make current` moves it back — the
link you handed someone never changes. One app per session: the entry
names the session it came from, so forking is how a second app starts.

A version is a derived commit holding the files under `/workspace/app`
and the filesystem rows that describe them, on a branch of its own that
belongs to no session. Two things follow. The capability URL hands out
the app and not the conversation — a handler under it can read its whole
tree, and so can anyone the link reaches, so the notes, the uploads, the
skills and the transcript are not in it. And the version outlives its
session by construction: where it came from is recorded as a soft
reference, so deleting the session leaves every version of it exactly as
it was. The corollary is that `cache` does not travel — it is workspace
state, not a file — so precompute into a file under `app/`, or use `db`.

nontainer's publication registry is generic (a name, its versions, which
one is current). The capability token, the route and the `db` are the
studio's, kept in its own manifest and keyed by token; the publication
is named for the token, which is what ties the two tables together.

The `db` is the exception, and deliberately. It is a handle to an
external store — the production database a real agent acts on — and
nobody clones that when they open a branch, so a fork, a delegate and
a published app all *reference* the file the session was using. A
published app's users write rows; so does the session's live preview;
they are the same rows, the way a deployment and its author share one
database. A later version of the app therefore meets whatever schema
the previous one left (`CREATE TABLE IF NOT EXISTS`, tolerant reads;
the agent is told).

Two rules hold the whole of it up.

**A db has an id of its own.** It is minted when a session first needs
one — `dbs/<hex>.sqlite` — and never spelled from a session name: a
slug is handed back the moment its session is deleted, while the file
lives on in a fork's row or a publication's, and a path spelled from a
name would hand the next holder of that slug somebody else's rows.
Every manifest row, session and app alike, names the file it opens, so
nothing anywhere derives a path from a name.

**Deleting a session never deletes a db.** `sweep_dbs()` removes every
file no session row and no app entry names; it runs when the registry
opens and can be called outright. That is one place reading the
manifest, rather than delete, release, delegate cleanup and unpublish
each counting referrers correctly. Installs from before ids hold files
named for their sessions and app copies at `dbs/apps/<token>.sqlite`;
opening the registry writes those paths into the rows and moves
nothing — a copy already made is a fact, and merging its rows into
another store is not the studio's to do. Where isolation is genuinely
wanted, a read-only handle is the seam, and it is not built until a
case needs it.

agno's cross-session tables — user memories, metrics — sit at
`store/agno` and never version: a memory spans conversations, so it
must not rewind with any one branch. Conversations from before the
move into the branch (the old `store/chat.sqlite`) are not carried
over; those sessions keep their files and start with an empty memory.

**Sessions name themselves.** After the first real exchange the studio
runs a second, stateless model pass over the transcript and stores what
comes back as the session's title; it re-reads it every five of your
messages, so a session that became something else is not listed under
what it was. The agent is never asked for this — a tool it may or may
not call costs a turn's attention and is missing exactly where it is
most wanted, on the session nobody named. Rename from the rail
(double-click the label) and your title wins from then on: yours is
what the rail shows, while the generated one goes on being read
underneath it, so clearing yours reveals a name for the session as it
now stands rather than the one it had when you renamed it. Delegates
are not named at all — they are labelled by the handle their parent
gave them. Publishing generates a second thing from the same
transcript: a sentence or two on what the app IS, kept on the app
rather than on any one version, which the rail shows under the app row
and the `sessions` tool's `published` listing hands to an agent
weighing where to start. It follows the same two tiers, so
your own words outrank it.

### Delegation

Behind `NONTAINER_STUDIO_SESSIONS`, which is **off by default for
now** — everything below is wired and tested, and the knob is what
hands the agent the tool.

The agent can delegate. One tool, `sessions`, with an action argument:

```
sessions ask     task=... [name=] [paths=] [inherit=] [wait=]
sessions list    your jobs: name, status, what you asked for
sessions result  name=... — the answer, once the job is done
sessions cancel / keep
```

A delegate is a fork with an agent on it. `ask` forks this session
under a name scoped to it (`analyst.sleepy-otter`), and the studio
assembles that branch the way it assembles any session — same model,
same tools, same python config, and the same app db, because a
delegate works against the store its parent is looking at, the way a
real subagent does. Then it runs the task as that session's turn,
exactly as a human's turn runs.

**Nothing comes back on its own.** The delegate's files are on its own
branch, and the parent takes them itself, in the terminal:

```sh
ws-git diff <name>                  # read what it did
ws-git merge <name>                 # take all of it
ws-git checkout <name> -- <paths>   # take some
```

`ws-git` is the agent's own git over the session — status, commit, log,
diff, branch, merge, checkout — and it is on because delegation is what
needs it. A delegate's work arrives as a *named* commit only if the
delegate runs `ws-git commit`; what it staged is taken as exactly that,
and anything it wrote past its last commit is reported as left out
rather than committed on its behalf. A delegate that never touches
ws-git is simpler: its branch head is its result, since every write is
already there.

**Delivery is pull, notification is the studio's.** nontainer holds the
answer until something collects it; the studio shows a count on the
parent's rail row and injects the answer into that session's next turn
— into the transcript as its own card, and into what the model is sent.
The text says whose answer it is and that this is the delegation
mechanism rather than the person at the keyboard: an answer is evidence
to weigh, not an instruction from a principal.

Delegates stay out of the rail (they are forked by a tool call, not by
a human), and are deleted with the session that asked. Which sessions
those are is recorded when the studio opens one, never read off the
name: `analyst.sleepy-otter` says who asked, and a `analyst.notes` you
made yourself is an ordinary session that nothing hides or deletes.
A delegate's conversation never comes back — its reply is the summary.
Budget is turns: `Registry(delegate_turns=...)`, three by default, and
a delegate that stops without a reply spends the rest being asked to
finish before its answer resolves as `capped`.

**A delegate's branch is not forever.** Retention is an idle TTL: one
nobody has dealt with for `NONTAINER_STUDIO_DELEGATE_TTL` hours (24 by
default) has its branch deleted and its answer dropped, so a week of
delegating does not leave a week of branches. Reading an answer counts
as dealing with it, and so does `sessions keep`, which exempts a
delegate for good — the agent is told both, and the rail's ⑂ badge
opens the same list for the human, with a keep toggle per delegate and
an age beside it. The sweep runs when the studio starts and hourly
after that. nontainer supplies it and schedules nothing on purpose: a
sweep on the way past an `ask` would make one delegate's retention
depend on how often another is asked for. A delegate asked for before
the last restart is in no job table, so the studio sweeps those off its
own record of who forked whom — which is also where a keep is written
down, since the job table it is flagged in does not survive a restart.
`0` turns the whole thing off. A swept delegate's name is refused
rather than opened: the record still says whose delegate it was, so
opening it fresh would hand back a blank session wearing the name of
work that is gone.

**Starting from a published app.** Publishing also names the session
commit the version came from: a store tag, `<token>/<version>/origin`,
which outlives the session the way the app does. `sessions` with
`action="published"` lists what the human has published with those
tags, and a store tag is a ref wherever `ws-git` takes one:

```sh
ws-git worktree add old <tag>          # the whole origin tree, read-only
ws-git checkout <tag> -- app/          # take files out of it
ws-git diff <tag>                      # compare it with here
```

The origin is the SESSION, where a version is `app/` alone — so the
notes, the uploads and the data beside the app come with it. `sessions
ask` with `fork_from=<tag>` and `inherit="full"` goes further and puts
the task to a clone of the agent that built it, carrying its memory as
of the publish; fresh gives that agent's files and no conversation. So
"another one like that" starts from the app rather than from a blank
page. The tag is a GC root, which is what keeps the origin session's
history reachable after the session is deleted — deleting the version,
or unpublishing the app, releases it.

**A delegate is readable, not drivable.** Clicking one in that listing
opens its transcript in the ordinary chat view: the parent's title sits
in a breadcrumb above it, a bar stands where the composer would be
saying what the session is (status, whose delegate, `keep`), and any
delegate it forked in turn is listed under that bar — so the drill-down
nests as deep as the delegation did. Every verb that would drive a
session — chat, edit, restore, model, title, upload, publish —
refuses a delegate with a 409, because the parent agent writes its
prompts, judges its branch and integrates it. An app is addressed by
token rather than by session, so taking one down or moving its pointer
is app management either way. Forking one is still how you take
its work as a session of your own.

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
`nontainer_studio/sessions.py` (registry, synchronized rewind, publish,
durable transcript). Delegation's loop half — the `SessionRunner` that
drives a forked session to an answer — is `nontainer_studio/delegates.py`.

## Tests

```sh
uv sync --extra dev
uv run playwright install chromium   # once, for the browser tests
uv run pytest
```

No LLM key needed anywhere in the suite:

- `test_server.py` drives the server plumbing (workspaces, forks,
  publish, restore) with a fake agent.
- `test_e2e.py` runs the whole stack in a real browser — uvicorn, SSE,
  the built frontend, agno's run loop, real tools — with only the model
  scripted (`NONTAINER_STUDIO_MODEL=dummy`; see `nontainer_studio/dummy.py`
  for the `!tool` / `!text` directive DSL). Needs the committed frontend
  build and the Chromium install above; skips cleanly otherwise.

The dummy model is also handy interactively: run the server with
`NONTAINER_STUDIO_MODEL=dummy` and type directives to puppet the agent.
