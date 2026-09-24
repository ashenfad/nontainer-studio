# What owns what

Three kinds of state, on purpose:

| state | durability | restore | fork | publish |
|---|---|---|---|---|
| **workspace** (files, cache, cwd) | kvgit branch per session | rewinds | branches (O(1)) | `app/` only — a publication version, frozen and read-only |
| **app `db`** (live SQLite host object) | one file under an id of its own, named by every row that shares it | untouched — external state has no history | named, not copied (a delegate's too) | named; every version of the app serves over it |
| **conversation** | agno's session in the same kvgit branch (+ a jsonl transcript) | rewinds with the files — one `checkout`, not two writes that can disagree; an `edit` trims the visible transcript too | `inherit` or `fresh` | a marker in the transcript you can restore to, or branch from |

## Apps are publications

An **app** is a nontainer **publication**: one URL, one `db`, and a
growing list of versions. The URL serves whichever version is *current*,
so publishing moves it forward and `make current` moves it back — the
link you handed someone never changes.

One app per session. The registry entry names the session it came from,
so forking is how a second app starts, and the name stays with the app: a
deleted session's name is not handed to a new session while an app it
published is still served.

Unsaved work is measured against the **newest** version rather than the
current one. Rolling the link back to `v1` and then leaving the session
alone means nothing is unpublished, and the link being behind is a
separate fact that the version list and the `changes` tab both name.

A version is a derived commit holding the files under `/workspace/app`
and the filesystem rows that describe them, on a branch of its own that
belongs to no session. Two things follow.

The capability URL hands out the app and not the conversation. A handler
under it can read its whole tree, and so can anyone the link reaches, so
the notes, the uploads, the skills and the transcript are not in it.

And the version outlives its session by construction: where it came from
is recorded as a soft reference, so deleting the session leaves every
version of it exactly as it was. The corollary is that `cache` does not
travel — it is workspace state, not a file — so precompute into a file
under `app/`, or use `db`.

Publishing also names the session commit a version came from, with a
store tag `<token>/<version>/origin`. The tag is a GC root, which is what
keeps the origin session's history reachable after the session is
deleted; deleting the version, or unpublishing the app, releases it. What
an agent can do with one is in [Delegation](delegation.md).

nontainer's publication registry is generic: a name, its versions, which
one is current. The capability token, the route and the `db` are the
studio's, kept in its own manifest and keyed by token; the publication is
named for the token, which is what ties the two tables together.

## The db is the exception

The `db` is a handle to an external store — the production database a
real agent acts on — and nobody clones that when they open a branch. A
fork, a delegate and a published app all *reference* the file the session
was using. A published app's users write rows; so does the session's live
preview; they are the same rows, the way a deployment and its author
share one database. A later version of the app therefore meets whatever
schema the previous one left, and the agent is told to write
`CREATE TABLE IF NOT EXISTS` and read tolerantly.

Two rules hold the whole of it up.

**A db has an id of its own.** It is minted when a session first needs
one — `dbs/<hex>.sqlite` — and never spelled from a session name: a slug
is handed back the moment its session is deleted, while the file lives on
in a fork's row or a publication's, and a path spelled from a name would
hand the next holder of that slug somebody else's rows. Every manifest
row, session and app alike, names the file it opens, so nothing anywhere
derives a path from a name.

**Deleting a session never deletes a db.** `sweep_dbs()` removes every
file no session row and no app entry names; it runs when the registry
opens and can be called outright. That is one place reading the manifest,
rather than delete, release, delegate cleanup and unpublish each counting
referrers correctly. Installs from before ids hold files named for their
sessions and app copies at `dbs/apps/<token>.sqlite`; opening the
registry writes those paths into the rows and moves nothing — a copy
already made is a fact, and merging its rows into another store is not
the studio's to do. Where isolation is genuinely wanted, a read-only
handle is the seam, and it is not built until a case needs it.

## Memory that must not rewind

agno's cross-session tables — user memories, metrics — sit at
`store/agno` and never version: a memory spans conversations, so it must
not rewind with any one branch. Conversations from before the move into
the branch (the old `store/chat.sqlite`) are not carried over; those
sessions keep their files and start with an empty memory.

## Sessions name themselves

After the first real exchange the studio runs a second, stateless model
pass over the transcript and stores what comes back as the session's
title. It re-reads it every five of your messages, so a session that
became something else is not listed under what it was. The agent is never
asked for this: a tool it may or may not call costs a turn's attention
and is missing exactly where it is most wanted, on the session nobody
named.

Rename from the rail — double-click the label — and your title wins from
then on. Yours is what the rail shows, while the generated one goes on
being read underneath it, so clearing yours reveals a name for the
session as it now stands rather than the one it had when you renamed it.
Delegates are not named at all; they are labelled by the handle their
parent gave them.

Publishing generates a second thing from the same transcript: a sentence
or two on what the app IS, kept on the app rather than on any one
version. The rail shows it under the app row, and the `sessions` tool's
`published` listing hands it to an agent weighing where to start. It
follows the same two tiers, so your own words outrank it.

The generator is stateless by construction — no db, no history, no tools
— so nothing it reads or writes touches the session's own agent, its
memory or its workspace, and a failure costs nothing but the previous
answer standing.

## Words for an agent that is already working

The composer stays open while a turn runs. A message sent then is
QUEUED — the server answers 202 and holds it on the session — and the
agent reads it appended to its next tool result, framed as coming from
you rather than as the tool's output. Nothing is interrupted, and nothing
it has already read is rewritten. The transcript shows the message where
it landed, mid-turn, with no edit handle, because the turn it arrived in
began before you said it.

A run that finishes with something still queued starts a follow-up turn
with it, as an ordinary message you can edit. A turn you STOPPED is the
exception: what was still waiting stays waiting for your next send, since
a stopped turn staying stopped matters more than promptness.

Withdraw a queued message with the ✕ beside it, while the agent has not
read it yet — delivery cannot be taken back. "Send now" is not on offer:
stop, then send, is the interrupt, and it already exists.

Compression respects the same line. When old tool results are summarised
at the context watermark, a message that rode out in one is cut off
before the summariser sees it and re-appended byte for byte, so the agent
never acts on a paraphrase of something you said exactly once.

## When the provider fails

A provider error is an interruption, not a restart. The model call
retries a transient failure itself, keeping every tool result the turn
has produced. Past that, the turn says so — "provider error — resuming
the turn where it stopped" — waits a few seconds, and resumes the same
run from its last tool result: the model remembers what it already did,
and nothing is redone.

It resumes once. If the resume fails too, the turn ends with the error,
and the run stays in the agent's memory closed with a note that it was
cut short, so "please continue" picks up from the work rather than
replanning it. A stop is never resumed, including one pressed while the
turn waits to resume.

Nothing is rewound. The workspace keeps every file the turn wrote, and
every file anyone else wrote while it ran — an upload you added mid-turn
is yours, not the failed turn's to take back.

## Rich replies

The agent can drop plots, tables, images and HTML into its answers with a
`ui = {...}` value. Each one becomes a file in the workspace and an
`artifact` event in the log, rendered inline and themed by the shell. The
event carries the artifact's name, its path and its kind, so a reply can
be reconstructed from the log alone — which is what the a2ui projection
below does.

## a2ui egress

`GET /api/sessions/{name}/a2ui` projects the transcript into an
[A2UI](https://a2ui.org) v0.9 message stream — an edge format for
declarative agent UIs, never the internal model (the event log stays
that).

The projection is **turn-level**: each turn becomes one surface, its
prose interleaved with its artifacts, and an `edit`'s rewind emits
`deleteSurface` for every turn it cut. Thinking, tool calls, usage,
notices and the `user` echo are ignored — a2ui renders the agent's reply
surface, not the whole conversation — and a turn with neither prose nor
artifacts emits nothing.

It is the same shape as `/events`: SSE that replays from `?since=N` and
then follows live, or a `?wait=0` JSON snapshot. Every message rides the
driving event's cursor, and a surface id is derived from the `done`
event's sequence number, so the stream is deterministic across replays
and a consumer resumes identically. Plotly specs ride an extension
`Chart` component, since a figure has no basic-catalog approximation and
the consumer brings the renderer; everything else maps to basic-catalog
`Text`, `Image`, `Card`, `Row` and `Column`.
