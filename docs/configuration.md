# Configuration

Everything the studio reads from the environment, grouped by what it
decides. A `.env` file next to where you launch is loaded at startup and
the real environment always wins, so `KEY=VALUE` lines there are
defaults rather than overrides — see `.env.example`.

## Model and providers

| name | default | what it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | offers the `anthropic` provider (default model `claude-sonnet-5-5`) |
| `OPENAI_API_KEY` | unset | offers `openai` (default `gpt-5.6-sol`) |
| `OPENROUTER_API_KEY` | unset | offers `openrouter` (default `anthropic/claude-sonnet-5.5`), over the `openai` SDK |
| `GOOGLE_API_KEY` | unset | offers `google` (default `gemini-2.5-pro`); needs `google-genai` installed |
| `OLLAMA_HOST` | unset | offers `ollama` (default `llama3.3`); needs `ollama` installed. Point it at your daemon, usually `:11434` |
| `NONTAINER_STUDIO_MODEL` | first available provider | the default model spec for new sessions |
| `NONTAINER_STUDIO_SUMMARY_MODEL` | the session's own model | the model that names sessions and describes published apps |
| `NONTAINER_STUDIO_EFFORT` | `medium` | reasoning effort for Claude models that take it, direct or via OpenRouter |

Availability is **detected, not configured**: a provider is offered when
its key is present and its SDK is importable, and the keys stay in the
server's environment — the browser picks models and never touches
credentials. With nothing configured the first available of anthropic →
openai → openrouter → google → ollama wins, and a server with no
provider at all exits at startup with a message naming the five keys.

A model spec is `provider:model` (`openrouter:deepseek/deepseek-v4-flash`).
A bare provider (`openrouter`) means that provider's default model, a
bare model id rides the default provider, and `dummy` selects the
scripted test model (see [Hacking](hacking.md)). Each session has its own
model, switchable mid-conversation from the picker; chat memory is keyed
by session, so the new model inherits the whole conversation.

OpenRouter specs take an optional `@slug[/quant]` tag that pins the
upstream provider: `openrouter:qwen/qwen3.6-35b-a3b@wandb/fp8` routes to
Weights & Biases at fp8 with fallbacks off. `@slug` alone pins the
provider; the `/quant` half pins the quantization. The tag is routing,
not identity — it is stripped before the model id is looked up — and it
works anywhere a spec does, including the picker's `custom…` field.

`NONTAINER_STUDIO_SUMMARY_MODEL` picks the model behind a second, tiny
run over the transcript: the session's generated title, and the
description an app carries when you publish one. Naming a conversation is
a job a small, cheap model does as well as the one doing the building.

`NONTAINER_STUDIO_EFFORT` takes `low`, `medium`, `high`, `xhigh` or
`max`. On the `anthropic` provider it reaches only models whose capability
lookup reports adaptive thinking with effort support; a model on the older
`enabled` thinking shape, or with no extended thinking at all, ignores it.
On `openrouter` every `anthropic/` model is sent it as `reasoning.effort`,
which OpenRouter translates for the model underneath.

Effort is the only lever on how long Claude thinks: adaptive thinking
takes no token budget. So one Claude response may generate up to 64,000
tokens, thinking included, or the model's own output limit if that is
lower. The limit comes from the same lookup directly and from OpenRouter's
model catalog; when it can't be learned the cap is 16,384. A smaller cap
risks a turn that spends it all thinking and ends before its tool call.

## Server and store

| name | default | what it does |
|---|---|---|
| `NONTAINER_STUDIO_PORT` | `8321` | the port uvicorn binds on `127.0.0.1` |
| `NONTAINER_STUDIO_STORE` | `~/.nontainer-studio` | where sessions, apps and the manifest live |
| `NONTAINER_STUDIO_SKILLS` | the repo's `skills/` | directory of starter skills seeded into new sessions |
| `NONTAINER_STUDIO_APP_ASSETS` | `nontainer_studio/appassets/` | the browser libraries served to agent-authored apps at `vendor/` |
| `NONTAINER_STUDIO_CSP` | derived | the content-security policy published apps carry; `none` drops it |
| `NONTAINER_STUDIO_COMPRESS_TOKENS` | per-model | the context watermark at which old tool results are compressed |

A skill is any child directory of the skills root holding a `SKILL.md`.
A skill whose workflow needs a knob that is off is not seeded: the
`starting-from-published` skill needs the `ws-git` verb to read an origin
tag, so a session without it never sees the skill.

Within a `SKILL.md`, text between `<!--if:KEY-->` and `<!--endif-->` is
kept only in sessions where KEY holds, and `<!--if:no-KEY-->` marks the
other side. The keys are `commands`, for a terminal that is termish
rather than a real shell, and `delegation`, for a session with the
`sessions` tool and `ws-git`. A session's copy holds the resolved text,
so an agent never reads a marker or a section it cannot follow. Blocks
do not nest.

One skill is partly built at seeding: `nontainer-ecosystem` ships only
its `SKILL.md` overview, and its `references/` are the READMEs of the
stack's packages, read from the metadata of the copies installed here.
They describe the versions this server runs, and a package that is not
installed (dud is an extra) has no file. They stay that way in older
sessions too: on each open, a README the agent has not edited is
rewritten when its package was upgraded and removed when it was
uninstalled. The first line's hash is how an unedited file is told apart;
an edited one is the session's own and is left alone.

The app-assets directory and the notes that describe it are one decision.
Swapping the directory means updating `FRONTEND_NOTES` in
`nontainer_studio/prompts.py` too, so the agent is told about *your*
libraries — see [Apps](apps.md).

`NONTAINER_STUDIO_CSP` is set on the apps config rather than on the
router, so `test_app` verifies an app under the same policy that serves
it. An explicit policy is used verbatim and therefore has to carry its
own script hosts (and `'wasm-unsafe-eval'` if a vendored library has a
wasm core); unset, the policy is derived from the config's `script_hosts`,
which is empty.

`NONTAINER_STUDIO_COMPRESS_TOKENS` overrides a watermark otherwise
computed per model — 60% of its context window, clamped to 32k–250k, or
100k when the context length is unknown. `0`, `off`, `none` or `false`
disables compression; any other number is floored at 1,000. The
transcript keeps full detail either way: only the model's view of old
tool results coarsens.

## Where agent code runs

| name | default | what it does |
|---|---|---|
| `NONTAINER_STUDIO_ISOLATION` | `process` | how the in-process backend contains agent code: `none`, `process` or `kernel` |
| `NONTAINER_STUDIO_EXECUTOR` | unset | swaps the in-process backend for dud: `dud` or `dud-vm` |
| `NONTAINER_STUDIO_VM_WARM` | `1` | VMs to pre-boot at startup under `dud-vm` |
| `NONTAINER_STUDIO_VM_MEDIUM` | `auto` | the guest rootfs medium under `dud-vm` |
| `NONTAINER_STUDIO_VIEW_WORKERS` | `0` | app-handler workers kept warm per view |
| `DUD_VM_MAX_TOTAL` | `4` | dud's cap on running VMs; the studio only supplies the default |
| `DUD_KERNEL` | `~/.dud/kernels/<arch>` | dud's kernel lookup; the studio never reads it |

By default agent code runs **in a worker process of its own**, gated by
[sandtrap](https://github.com/ashenfad/sandtrap) — a walled garden for
cooperative code — with the workspace files, the cache and the `db`
staying host-side and bridged over RPC. That is what `ISOLATION=process`
buys: a segfault or an OOM in C-extension guts costs the turn rather than
the server. `kernel` adds syscall and network lockdown on top; `none`
runs agent code in the server process. An unrecognized value falls back
to `process` rather than raising.

`NONTAINER_STUDIO_EXECUTOR` steps off that model entirely, onto
[dud](https://github.com/ashenfad/dud) (needs the `dud` extra and Python
3.11+):

| value | what runs the code | isolation |
|---|---|---|
| unset (default) | the in-process sandbox | sandtrap's gates, under `ISOLATION` |
| `dud-vm` | a disposable microVM — vfkit on macOS, firecracker on Linux/KVM | real |
| `dud` | a host process — real bash, real files; the workspace lives in a temp directory of its own, not at `/workspace` | **none** |

```sh
uv sync --extra dud
NONTAINER_STUDIO_EXECUTOR=dud-vm uv run nontainer-studio
```

Take `=dud` seriously: it is real bash and real files with no containment
at all, running as your user with your network. It buys fidelity for
development, not a boundary, and the server warns about it at startup.

`dud-vm` boots a `python:slim` guest matched to your interpreter with the
data stack layered in at this venv's pinned versions — cache values are
pickles, and a session authored on one executor gets read on the other.
The first run builds and caches the image; later runs and restarts reuse
it. `NONTAINER_STUDIO_VM_MEDIUM=auto` lets dud resolve an erofs root,
which is demand-paged, so guest RAM is the pages touched rather than a
RAM-resident initramfs; `initramfs` is the fallback. `VM_WARM=0` still
bakes the image in a background thread, so a first session open pays boot
and never build-plus-boot.

`DUD_VM_MAX_TOTAL` and `DUD_KERNEL` are **dud's** variables, not the
studio's. The studio only defaults the first to `4` before anything can
build the pool, because it never closes sessions during a run and every
session ever touched would otherwise hold a VM for the process lifetime;
past the cap dud reclaims the longest-quiet VM and its owner recovers
transparently on its next call. Your own value always wins. `DUD_KERNEL`
is read by dud itself, which looks for an explicit argument, then
`$DUD_KERNEL`, then `~/.dud/kernels/<arch>`, and fails closed without
one.

`NONTAINER_STUDIO_VIEW_WORKERS` is a cache size, not a limit — nothing
here caps how many workers a burst of concurrent requests creates. Studio
preloads the granted data stack into sandtrap's forkserver broker, which
puts a pristine worker at roughly 12ms, so the default of 0 buys clean
per-request process state for about the price of reusing one. Raise it
only if a published app serves real concurrency. An unparseable or
negative value falls back to 0.

## What the agent is allowed to do

| name | default | what it does |
|---|---|---|
| `NONTAINER_STUDIO_WSGIT` | off | gives the agent the `ws-git` terminal verb |
| `NONTAINER_STUDIO_SESSIONS` | off | gives the agent the `sessions` tool, and turns `ws-git` on with it |

Both are flags: `1`, `true`, `yes` or `on` (any case) turn one on, and
anything else, including unset, leaves it off.

`ws-git` is the agent's own git over its session — status, commit, log,
diff, branch, merge, checkout. It is off for now while the app-building
path is polished. The workspace is versioned either way: every mutating
tool call still commits, and the human's rewind, fork, publish and
restore are host-side verbs over that history. This decides what the
*agent* can type, nothing about what the studio can do.

`NONTAINER_STUDIO_SESSIONS` hands the agent the `sessions` tool, its
handle on delegation and on what you have published. It turns `ws-git` on
as well, since that verb is how a delegate's work comes back and nothing
else is; `NONTAINER_STUDIO_WSGIT` on its own is versioning without
delegation. With the tool off, the session's delegation helper is still
built — the retention sweep, the drill-down routes and the delegates
listing all read it — and with no tool to fork through they simply find
nothing.

## The web

| name | default | what it does |
|---|---|---|
| `NONTAINER_STUDIO_WEB` | on when `OPENROUTER_API_KEY` is set | gives agent sessions the `web` host object; `0`, `false`, `no` or `off` withholds it |

`web` lets the agent's Python search and read the web through
OpenRouter, on the operator's key:

- `web.search(query, deep=False)` answers from a Perplexity search
  (`perplexity/sonar`, or `perplexity/sonar-pro-search` with `deep=True`)
  and lists the numbered sources. A list of queries runs concurrently.
- `web.fetch(url, question)` reads one page and answers a question from
  it. A small model (`google/gemini-3.5-flash-lite`) reads the page
  through OpenRouter's `web_fetch` tool, which extracts its main content
  with Parallel. It returns an extract, never the page itself: OpenRouter
  hands fetched content only to a model, and a model asked to relay a page
  verbatim abridges it. The fetch happens at OpenRouter, so it cannot
  reach this machine or its network. A list of URLs is read concurrently,
  with one question for every page or one per page.

Only agent sessions get it. A published app serves anyone holding its
link, so its snapshot is built without `web`, and app code in the
session's own preview should not call it either. A session with `web`
also gets a 180-second Python timeout in place of 30, because time spent
waiting on a host call counts against it and a deep search alone takes
20–40 seconds. Measured costs: about half a cent for a search, 3–4 cents
for a deep one, and a tenth of a cent for a fetch.

## Media

| name | default | what it does |
|---|---|---|
| `NONTAINER_STUDIO_MEDIA` | on when `OPENROUTER_API_KEY` is set | gives agent sessions the `media` host object; `0`, `false`, `no` or `off` withholds it |

`media` lets the agent's Python make images, speech and music through
OpenRouter, on the operator's key, and writes each file into the
session's workspace:

- `media.image(prompt, path, transparent=False, aspect="1:1",
  quality="low", references=None)` writes a PNG from
  `openai/gpt-image-2.5-sunburst`. `transparent=True` asks for a real
  alpha channel, which on OpenRouter only OpenAI's image models give:
  Gemini's and `gpt-image-2` take no transparent background.
  `references` are workspace images to work from. Measured: about 11s
  and $0.006 at low quality.
- `media.speech(text, path, voice="Kore")` writes a WAV from
  `google/gemini-3.8-flash-tts`, with bracketed direction such as
  `[whispers]` in the text, and returns its length in seconds. Gemini
  speech comes back only as raw PCM, so it is wrapped as 24kHz mono WAV,
  about 48KB a second.
- `media.music(prompt, path, length="clip")` writes an MP3 from Lyria 3:
  `google/lyria-3-clip-preview` for a clip of about 30s ($0.04, about
  10s), or `google/lyria-3-pro-preview` for `length="song"`, whose length
  follows the prompt only roughly ($0.08, about 25s). It returns the
  length in seconds, read from the MP3's frames, and a vocal track's
  lyrics with the time each line starts. Lyria answers only streamed,
  as 44.1kHz stereo MP3, and only when asked for audio before text; an
  answer without music is reported as an error rather than written.

All three take a list of dicts of their arguments to make several at once.
Relative paths are taken from `/workspace`, not the agent's cwd, and
nothing is written outside it. Only agent sessions get `media`, for the
same reason as `web`, and each session's is bound to its own workspace.
The file is written when the call returns; under dud the running call
cannot read it back, but the next call can.

## Delegation caps

| name | default | what it does |
|---|---|---|
| `NONTAINER_STUDIO_DELEGATE_TTL` | `24` | hours a delegate's branch is kept after anyone last dealt with it; `0` turns the sweep off |
| `NONTAINER_STUDIO_DELEGATE_DEPTH` | `2` | how deep delegation may nest, in hops from the session a human started; `0` turns the cap off |
| `NONTAINER_STUDIO_DELEGATE_TOOL_CALLS` | `60` | tool calls one delegate turn may spend; `0` turns the cap off |
| `NONTAINER_STUDIO_DELEGATE_WAKES` | `10` | turns delegates' answers may start on an idle session before the human writes again; `0` turns waking off |

All four fall back to their defaults on an unparseable value rather than
raising, and all four are clamped at zero.

**`Registry(delegate_turns=...)` is not an environment knob.** The turn
budget a delegate spends before its answer resolves as `capped` is a
constructor argument, three by default, and `main()` does not pass one —
so a plain `nontainer-studio` run always uses three. Changing it means
building the `Registry` yourself.

What each cap means for the agent is in [Delegation](delegation.md).
