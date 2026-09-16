# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- **The nontainer floor is 0.7.5.** With it come kvgit 0.3.9 and
  monkeyfs 0.1.11, and on the dud rung a guest write into an attachment
  or a read-only mount is refused and reported rather than raised.
  `from host import call` is the
  spelling for reaching a handler from a test, and the template and the
  skill use it; `ws-pytest --help` states the rest of the contract, a
  directly imported handler gets `HttpError` the way a request gives
  it, and `types`, `typing` and `dataclasses` import in the sandbox.

- **nontainer resolves from PyPI.** The sibling-checkout override is
  gone from `pyproject.toml`, so a fresh `uv sync` installs the released
  library the floor names, the same one CI tests against. The browser
  tests need `uv run playwright install chromium` once; the README says
  so.

- **The app skill ships unit tests.** A **Tests** section says where
  tests live (`tests/`, never under `app/`), how to run them
  (`ws-pytest -v`, `ws-vitest --reporter=verbose`) and the one line of
  the `call` contract worth knowing before `ws-pytest --help`. Two
  reference files come with it: `test-summary.py`, which passes against
  `api-handler.py` as shipped — happy path, an empty selection, and the
  400 an unknown category earns — and `format.test.js`, over a new
  `format.js` that carries the value formatting and query building
  `app.jsx` used to do inline (copied to `app/format.js`, imported as
  `./format.js`). That formatting is split by what a value IS:
  `formatLabel` for an identifier, which renders a year as `2023` and
  never as `2,023`, and `formatValue` for a measure, which groups the
  integer part, keeps every fractional digit it was given, and rounds
  only for a call site that passes `{ digits }` — bare
  `toLocaleString()` caps at three fractional places, so it renders
  `1.23456` as `1.235` and `0.00001` as `0`. Both are run through a session in the suite, so a
  template that stops passing stops the build rather than reaching an
  agent. The Python reference is spelled with a hyphen because a bare
  `ws-pytest` collects `test_*.py` anywhere outside `app/`, and a
  reference that ran itself would write its fixture parquet over the
  app's data.

- **`ws-git` and the `sessions` tool are behind knobs, both off.**
  `NONTAINER_STUDIO_WSGIT` registers the `ws-git` terminal verb and
  `NONTAINER_STUDIO_SESSIONS` registers the `sessions` tool; unset,
  neither reaches the agent and the primer names neither. The machinery
  is untouched — the session still builds its `Sessions` helper, so the
  retention sweep, the delegates rail and the drill-down routes go on
  working, and the human's rewind, fork, publish and restore are
  host-side workspace verbs that never needed the terminal one. The
  primer is now assembled from four independent pieces (ws-git, the
  unit-test verbs, delegation, retention), each under its own gate, so
  a session is told about exactly what it was given. The unit-test
  verbs are gated on the workspace's own command table rather than on
  ws-git: `enable_apps` installs `ws-pytest` and `ws-vitest`, and they
  are there to teach whether or not the versioning verb is. Starting
  from a published app moved out of the app-building skill into a
  `starting-from-published` skill of its own, seeded only where a
  session can follow it: the `sessions` tool on AND that session's own
  `ws-git` answer true, since the workflow after the listing is the
  ws-git verbs that read an origin tag.

- **The studio names a session; the agent is not asked to.** The
  `recommend_title` tool is gone. After the first turn that was a real
  exchange, and every five messages after that, the studio runs a
  second, stateless model pass over the session's transcript and stores
  what comes back as the title — no db, no history, no tools, off the
  turn lock, and a failure leaves the previous name standing. A tool
  the model may or may not call cost a turn's attention, arrived when
  the model felt like it, and was missing exactly where a name is most
  wanted: the session nobody named. The human's own title still
  outranks it — theirs is the name that shows, while the generated one
  goes on being read underneath, so clearing theirs reveals a name for
  the session as it now stands. `NONTAINER_STUDIO_SUMMARY_MODEL`
  picks the model this runs on (the session's own by default) —
  naming a transcript is a job a small, cheap model does as well as
  the one doing the building. Delegates are never named: they are
  labelled by the handle their parent gave them.
- **The nontainer floor is 0.7.3.** A full-inherit delegate is the
  agent that was at the fork point: the fork carries the conversation
  as the child's own, where before the copied record still named the
  session it came from and the chat db, which binds a branch to one
  session id, gave the delegate no memory and refused its turns. A
  store tag is a ref wherever
  ws-git reads one — `worktree add`, `checkout -- <paths>`, `diff`,
  `log`, `show` — and `inherit="full"` is allowed with `fork_from`, so
  the two halves of starting from a published app are mechanisms and
  not conventions. It also buys the four things this release is
  written against: a delegate's branch has retention, so a
  swept job reads `expired` and its answer is gone; the agent's own
  `sessions keep` actually sets the flag the sweep honours; `register_wsgit`
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

- **The app skill has a definition of done.** An app is done when
  `test_app` passed with data-bearing assertions, `ws-pytest` and
  `ws-vitest` ran and passed with their count lines quoted in the
  report, the copied reference tests are adapted or deleted, and the
  project's `README.md` at the workspace root is filled in from a new
  reference: what the app does, the data, the endpoints, how to run the
  tests, and dated decisions with their reasons. The one escape is a
  stated waiver naming the tier that had nothing to test and why. The
  primer says the same in one sentence, so the rule is in front of the
  agent every turn and not only in the skill.

- **A published app says what it is.** Publishing generates a sentence
  or two about the session behind the app — what it knows, built or
  decided — and keeps it on the APP rather than on any one version, so
  it moves forward with each publish the way the title does. The rail
  shows it under the app row, the `sessions` tool's `published` listing
  carries it under the app it belongs to, so an agent weighing an
  origin tag reads what is in there before mounting it, and
  `POST /api/apps/{token}/description` writes the human's own words,
  which outrank the generated ones (blank clears them again). A
  generation that fails is not a failed publish: the app keeps whatever
  it already said.
- **A swept delegate's name is refused, not reopened.** `open` is
  create-or-return, so a name it does not know mints a fresh session —
  which is the one wrong answer for a delegate whose branch the
  retention sweep took: the record still says whose delegate it was,
  and what opened was a blank session wearing the name of work that is
  gone. It raises now, and `POST /api/sessions` answers 409 with the
  reason instead of handing back an empty session.
- **A published version names the commit it came from.** Publishing
  store-tags the origin — the whole session tree at that publish, where
  the version holds `app/` alone — as `<token>/<version>/origin`, and
  the app's row records the name so nothing reconstructs it. The tag
  belongs to the store rather than to a session, so it opens after the
  session that built the app is deleted, and it is a GC root, so that
  session's history up to the publish is kept for exactly as long as
  the version is. Removing the version releases it, and so does taking
  the app down; a row written without one reads as an app with nothing
  to start from, and no reader invents a name for it.
- **The agent can see what the human published, and start from it.**
  The `sessions` tool is the studio's now: nontainer's shape and its
  actions, plus `published`, which lists each app with its title, its
  current version and its origin tag. A listed tag goes straight into
  the terminal — `ws-git worktree add <dir> <tag>` mounts that session,
  `ws-git checkout <tag> -- <paths>` takes files out of it, `ws-git
  diff <tag>` compares it with here — or into `sessions ask` as
  `fork_from=<tag>` with `inherit="full"`, which puts the task to a
  clone of the agent that built the app, carrying its memory as of the
  publish. "Another one like that" now starts from the app.
- **A delegate opens, read-only.** Its name in the rail's ⑂ listing is
  now a way in: the delegate's own transcript renders in the ordinary
  chat view, with a breadcrumb up to the session that forked it and,
  where the composer would be, a bar saying what it is — status, whose
  delegate, how long since anybody dealt with it, and the keep toggle.
  The delegates it forked in turn are listed under that bar, so the
  drill-down nests. The studio offers no merge, take or discard: the
  parent agent judges a delegate's branch and integrates it, so every
  verb that drives a session — chat, edit, restore, model, title,
  upload, publish — answers 409 on a delegate, and
  `GET /api/sessions/{name}`
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

- **A publish in flight disables the publish buttons.** A version takes
  a moment to land now that publishing also describes the app, and a
  second click meanwhile was refused by the route and shown as an
  error. The buttons wait instead.

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
