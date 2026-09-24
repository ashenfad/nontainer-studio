# Delegation

An agent can hand a task to a fork of itself and collect the answer a
turn or two later. Everything here is wired and tested; the knob is what
hands the agent the tool.

## The knob

`NONTAINER_STUDIO_SESSIONS` is **off by default for now**, while the
app-building path is polished. On, it registers the `sessions` tool and
turns the `ws-git` verb on with it, since that verb is how a delegate's
work comes back and nothing else is. Off, the tool is not registered and
the primer says nothing about delegating or about published apps — but
the session's helper is still built, so the retention sweep, the
drill-down routes and the delegates listing all keep working and simply
find nothing new. See [Configuration](configuration.md).

## The `sessions` tool

One tool with an action argument:

```
sessions ask        task=... [name=] [paths=] [inherit=] [wait=]
                    [fork_from="<session@commit or tag>"] [resume="<name>"]
sessions list       your jobs: name, status, what you asked for
sessions result     name=... — the answer, once the job is done
sessions cancel     name=...
sessions keep       name=...
sessions published  the human's apps: title, current version, origin tag
```

`published` is the studio's own action — it reads the app registry, which
is the studio's half of publishing and nothing nontainer holds. Every
other action is dispatched by nontainer unchanged, with the arguments the
model sent. Refusals come back as text rather than as exceptions: "no job
named x" is something a model can act on where a traceback is not.

`ask` returns at once with the child's name; `wait=true` blocks instead,
which is worth it only for short work. `paths` narrows what the delegate
SEES without narrowing its branch — it still holds everything and may
write anywhere. `inherit="fresh"` (the default) gives it a fresh
conversation over those files; `"full"` continues the one at the fork
point. `resume=<name>` gives a new task to a delegate you already have,
conversation kept; it does one task at a time.

A delegate is a fork with an agent on it. `ask` forks the session under a
name scoped to it (`analyst.sleepy-otter`), and the studio assembles that
branch the way it assembles any session — same model, same tools, same
python config, and the same app db, because a delegate works against the
store its parent is looking at, the way a real subagent does. Then it
runs the task as that session's turn, exactly as a human's turn runs. The
task arrives with a provenance header saying whose delegation it is, so
the mechanism is explicit rather than felt as an instruction from the
human principal.

## Nothing comes back on its own

The delegate's files are on its own branch, and the parent takes them
itself, in the terminal:

```sh
ws-git diff <name>                  # read what it did
ws-git merge <name>                 # take all of it
ws-git checkout <name> -- <paths>   # take some
ws-git worktree add <dir> <name>    # check its tree out under a directory
```

A delegate's work arrives as a *named* commit only if the delegate runs
`ws-git commit`; what it staged is taken as exactly that, and anything it
wrote past its last commit is reported as left out rather than committed
on its behalf. A delegate that never touches `ws-git` is simpler: its
branch head is its result, since every write is already there.

Without the verb, delegation is the degraded half of itself — the
delegate's answer is all that ever comes back, and the honest thing to
ask it for is findings rather than edits. That is why the `sessions` knob
turns `ws-git` on.

## How an answer arrives

Delivery is pull; the notification is the studio's. nontainer holds the
answer until something collects it, and the studio shows a count on the
parent's rail row.

Collection happens at two moments. **Mid-turn**, an answer that lands
while the parent is working rides out appended to its next tool result,
through the same inbox a human's queued message uses (nontainer 0.7.8).
**Between turns**, anything still uncollected is injected at the start of
the parent's next turn — ahead of the human's message in what the model
is sent, because it arrived first and the message is the instruction.
Either way the transcript gets the same `delegate` event, so the rail's
waiting count and the delivery record read one fact rather than two.

The text says whose answer it is and that this is the delegation
mechanism rather than the person at the keyboard: an answer is evidence
to weigh, not an instruction from a principal. A delegate's conversation
never comes back — its reply is the summary.

## What a restart leaves behind

The job table lives in the process and the branch lives in the store, so
a restart keeps the record of who forked whom and the branch, and takes
every uncollected answer with it.

The parent's next turn carries one note per such delegate — into the
transcript and into what the model is sent — saying the task is
outstanding, that `ws-git diff` / `merge` / `checkout` still reach the
branch, and that asking again is `sessions ask` rather than `resume`.
Delivery is a fact of the transcript like every other, so a rewind past
the note brings it back and a second restart does not repeat it.

Shutting the studio down does not wait for a delegate. Each delegate turn
runs on a loop the registry can reach, and closing asks every turn in
flight to stop before it joins the workers. A stopped turn is kept like
any other cut turn — the child's memory keeps what it did — and the
job resolves as `failed` saying the studio shut down mid-run.

## Budget and caps

Budget is turns: three by default, and a delegate that stops without a
reply spends the rest being asked to finish before its answer resolves as
`capped`. That number is `Registry(delegate_turns=...)`, a constructor
argument rather than an environment variable, and `main()` never passes
one.

Each of those turns is a tool loop with nobody watching it and no stop
button over it, so a delegate's agent also carries a per-turn tool-call
cap: `NONTAINER_STUDIO_DELEGATE_TOOL_CALLS`, 60 by default, `0` off. Past
it the calls are refused with a tool result saying so and the turn
carries on to its reply. A human's session carries no such cap.

**Delegation does not nest forever.** A delegate is a full agent on the
parent's model, with four delegate workers of its own, so nesting
multiplies rather than adds. `NONTAINER_STUDIO_DELEGATE_DEPTH` counts
hops from the session a human started: 2 by default, so that session
delegates and its delegates delegate, and the generation after them reads
a refusal on `sessions ask` telling it to do the task itself and answer
with what it found. The refusal is written before the fork, since the
fork is the expensive half. The other actions stay, and only the session
the cap binds is told about it. `0` turns the cap off.

## Retention

A delegate's branch is not forever. Retention is an idle TTL: one nobody
has dealt with for `NONTAINER_STUDIO_DELEGATE_TTL` hours (24 by default)
has its branch deleted and its answer dropped, so a week of delegating
does not leave a week of branches.

Reading an answer counts as dealing with it, and so does `sessions keep`,
which exempts a delegate for good. The agent is told both, and the rail's
⑂ badge opens the same list for the human, with a `keep` toggle per
delegate and an age beside it.

The sweep runs when the studio starts and hourly after that. nontainer
supplies it and schedules nothing on purpose: a sweep on the way past an
`ask` would make one delegate's retention depend on how often another is
asked for. A delegate asked for before the last restart is in no job
table, so the studio sweeps those off its own record of who forked whom —
which is also where a `keep` is written down, since the job table it is
flagged in does not survive a restart. `0` turns the whole thing off.

A swept delegate's name is refused rather than opened: the record still
says whose delegate it was, so opening it fresh would hand back a blank
session wearing the name of work that is gone. The rail shows that status
as `swept`.

## Starting from a published app

Publishing names the session commit a version came from with a store tag,
`<token>/<version>/origin`, which outlives the session the way the app
does. `sessions published` lists what the human has published with those
tags, and a store tag is a ref wherever `ws-git` takes one:

```sh
ws-git worktree add old <tag>          # the whole origin tree, read-only
ws-git checkout <tag> -- app/          # take files out of it
ws-git diff <tag>                      # compare it with here
```

The origin is the SESSION, where a version is `app/` alone — so the
notes, the uploads and the data beside the app come with it. `sessions
ask` with `fork_from=<tag>` and `inherit="full"` goes further and puts
the task to a clone of the agent that built it, carrying its memory as of
the publish; `fresh` gives that agent's files and no conversation. So
"another one like that" starts from the app rather than from a blank
page. An app whose current version carries no origin tag says so in the
listing: there is nothing to mount, take from or fork there.

Reading a tag needs `ws-git`, so the skill that teaches this workflow is
seeded only into sessions that have the verb.

## A delegate is readable, not drivable

Delegates stay out of the rail — they are forked by a tool call, not by a
human — and are deleted with the session that asked. Which sessions those
are is recorded when the studio opens one, never read off the name:
`analyst.sleepy-otter` says who asked, and an `analyst.notes` you made
yourself is an ordinary session that nothing hides or deletes.

Clicking one in the ⑂ listing opens its transcript in the ordinary chat
view. The parent's title sits in a breadcrumb above it, a bar stands
where the composer would be saying what the session is (status, whose
delegate, `keep`), and any delegate it forked in turn is listed under
that bar — so the drill-down nests as deep as the delegation did.

Every verb that would drive a session — chat, edit, restore, model,
title, upload, publish — refuses a delegate with a 409, because the
parent agent writes its prompts, judges its branch and integrates it.
Reads are allowed: a human looking at what a delegate built needs to see
the diff, and a diff commits nothing. An app is addressed by token rather
than by session, so taking one down or moving its pointer is app
management either way. Forking one is still how you take its work as a
session of your own.
