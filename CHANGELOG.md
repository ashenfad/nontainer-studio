# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- **The nontainer floor is 0.7.0.** It buys the three things this
  release is written against: a delegate's branch has retention, so a
  swept job reads `expired` and its answer is gone; `register_wsgit`
  answers whether the agent can type the verb instead of leaving the
  caller to re-read the executor flags behind it; and `enable_apps` is
  idempotent over a fork and registers `ws-pytest` and `ws-vitest`
  beside `ws-curl`.
- **What `ui` renders is a closed set, and a plain dict is not in it.**
  A value that is not a chart, a table, a card row, a picture or html
  now writes no file and renders nothing; the tool result carries a note
  naming the binding and the shapes that do render, which the tool
  timeline shows. Assignments that used to come back as a JSON details
  block come back as that note instead — the file it wrote was never
  something the shell could render, and announcing it told the agent its
  figure had arrived. A string naming a workspace file the agent wrote
  itself still lands: that is a pointer to an artifact, not a value.
- **A delegate whose branch was swept is not something a turn can
  deliver.** Retention for a delegate's branch is an idle TTL, and when
  a sweep takes one the job's row stays as `expired` with its answer
  dropped — asking for it raises. The studio counted such a job in the
  rail's ⑂ badge and sent the next turn to collect an answer that is
  gone; because delivery is recorded by the transcript rather than by a
  flag, nothing would have taken it off that list. It is dropped the way
  a cancelled job is now, from the count and from the delivery alike,
  and a sweep landing mid-delivery skips the job for good rather than
  retrying it every turn. This is what the studio does when a sweep
  lands; what schedules one is the retention TTL below.
- **The primer asks the function that did the wiring.** Whether the
  agent can type `ws-git` was being re-derived from two runtime flags in
  two places — the primer's delegation half and a delegate's brief —
  where the call that registers the verb answers exactly that question.
  The session records its answer and both read it, so one session cannot
  be told two things about one verb.

### Added

- **A delegate opens, read-only.** Its name in the rail's ⑂ listing is
  now a way in: the delegate's own transcript renders in the ordinary
  chat view, with a breadcrumb up to the session that forked it and,
  where the composer would be, a bar saying what it is — status, whose
  delegate, how long since anybody dealt with it, and the keep toggle.
  The delegates it forked in turn are listed under that bar, so the
  drill-down nests. The studio offers no merge, take or discard: the
  parent agent judges a delegate's branch and integrates it, so every
  verb that drives a session — chat, edit, restore, model, title,
  upload — answers 409 on a delegate, and `GET /api/sessions/{name}`
  is what tells a shell landing on `?session=<child>` that it is
  looking at one. Fork still works, since taking a delegate's branch as
  a session of your own is a thing to want.
- **A delegate's branch has a TTL, and the studio schedules the sweep.**
  `NONTAINER_STUDIO_DELEGATE_TTL` is the hours a delegate is kept after
  anybody last dealt with it — 24 by default, `0` off. Past it the
  branch is deleted and the answer dropped; reading an answer counts as
  dealing with the delegate, and `sessions keep` exempts one for good.
  It runs when the studio starts and hourly after that, which is the
  half nontainer deliberately leaves to the embedder. The other half is
  one it cannot reach: a job table is per live session in this process,
  so a delegate asked for before the last restart is in no table at
  all. The manifest's record of who forked whom now also carries when
  that delegate was last dealt with and whether somebody kept it, so
  the studio sweeps those itself and is honest about their age across a
  restart. Older records (`{child: parent}`) are read as delegates
  ageing from their session's birthday. The primer says the number and
  that the sweep is on — nontainer's `sessions` tool already says
  `keep` exists; what only the studio can say is whether anything ever
  collects.
- **The rail lists what a session delegated.** The ⑂ badge opens it:
  every delegate with its status, how long since anybody dealt with it,
  and a keep toggle — because those branches age out and a delegate has
  no rail row of its own to say so from. A session whose answers have
  all been read keeps a muted badge rather than losing the way in. A
  swept delegate reads as swept, and one whose job table did not
  survive a restart is marked as what the record knows rather than
  passed off as a live status. `GET /api/sessions/{name}/delegates` and
  `POST /api/sessions/{name}/delegates/{child}/keep` are the routes.
- **The primer names the tier below a request.** `ws-pytest` asks a
  question of one Python function and `ws-vitest` of one frontend
  module, which is what an agent needs when `test_app` fails and the
  page cannot say which half is wrong.
- **The primer says where else a delegate can start.** The studio's
  sessions share one store, so a commit of any of them is a fork point
  for a delegate of another: `fork_from=<session>@<commit>`, with
  `ws-git branch` listing the names. `resume` is named beside it, being
  the alternative to forking a second delegate. What an ask takes beyond
  that is the `sessions` tool's own description, which the primer does
  not repeat.

- **CI runs the browser tests.** The python matrix installs no browser,
  so everything behind an importorskip on playwright skipped there —
  the whole browser E2E suite, and the third of the server suite that
  drives the served page. One `browser` job runs both files against the
  committed bundle, so a regression in the shell fails a pull request
  instead of waiting to be noticed on somebody's laptop.

### Fixed

- **The README and the seeded skill named a verb that does not exist.**
  Both said the apps loop's `curl` builtin is missing under dud and the
  agent should drive the page instead. The verb is `ws-curl` on every
  rung — plain `curl` is the real one, which reaches the network rather
  than the app — and it is relayed out of a guest and answered on the
  host, as `ws-git`, `ws-pytest` and `ws-vitest` are. So the fastest
  debugging step was lost to `command not found` on the default rung and
  denied outright on a dud one. The skill's ladder is unconditional
  again and ends on the two unit-test verbs; the conditional-block
  machinery stays for skill text that really is per-rung.
