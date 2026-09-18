# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- **A `changes` tab says which files are unpublished, and what the
  edit was.** The publish button carries a count; the count needed
  somewhere to lead. The third side tab lists the app files that
  differ from the newest version — `added` / `changed` / `removed`,
  the path and the size — and opening a row fetches that one file's
  two sides and renders them as the line diff the transcript already
  uses for an edit (one `Diff.svelte` now, so an edit and an
  unpublished change never look like two different things). The tab
  label carries the count as well, so the state reads from whichever
  tab is open, and the rows and the open diffs follow the agent as it
  writes. The baseline is the newest version, the same one the count
  measures from; when the URL is behind, the tab says so and offers
  the served version as the side to diff against. A `publish` button
  in the tab header saves what the list shows; a delegate keeps the
  tab and loses the button, because a diff commits nothing and a
  publish does.

- **Unsaved app work is measured from the newest version, and one
  file's two sides have a route.** A session's apps row counted from
  the version the URL serves, so a session rolled back to v1 and then
  left alone read as having unsaved edits it did not have. The count
  now measures from the newest version — the last save — and the
  pointer being behind stays what the version list says it is. Each
  changed path comes with `status` (`added` / `modified` / `removed`)
  and `size`, so a listing can be rendered without reading a file, and
  `GET /api/sessions/{name}/apps/{token}/changes/file?path=&since=`
  answers one path as a named version holds it and as the session
  holds it now. The old side is a fresh read-only open of that
  version, passing no execution settings, so a diff never boots a
  backend; bodies over 64 KB a side, or that are not text, come back
  empty with their sizes.

- **The publish button is the dirty indicator, and publishing is one
  click.** It reads `publish` with nothing published yet,
  `publish · 3 files` when the live `/workspace/app` differs from the
  newest version — the paths in its tooltip, where the warning badge
  used to keep them — and a dimmed `published v2` once they match. So
  the state that mattered (there are edits since the last save) is on
  the control that acts on it instead of beside it. The click sends no
  name and the server picks `vN`, which is what the prefilled field
  offered anyway; naming a version yourself moved behind the `▾` caret,
  which opens the same field empty. Publishing no longer flips the pane
  to `published`: swapping what you are looking at mid-conversation
  loses your place, and the transcript marker and the new label already
  say it happened. Both buttons wait out a running turn, which the
  publish route refuses.

- **A session publishes one app.** The publish route's `app` parameter
  — a token to extend, `"new"` to start another — is gone, and a body
  still carrying it is refused with a 400 saying so rather than
  publishing somewhere the caller did not mean. Nothing sent it: no UI
  offered the choice, the agent cannot publish at all, and the publish
  button, the transcript marker and the count of changed files each
  read one app row per session and had no answer to "which one". A
  second app is a fork's, which is what the refusal for someone else's
  token already said. With the lineage fixed by name, the name is the
  app's for as long as the app is published: a deleted session's name
  is neither minted nor accepted for a new session while an app row
  still names it, since that session would extend the dead one's app —
  publishing over a URL somebody holds, on that app's database.
  Unpublishing hands the name back.

- **The nontainer floor is 0.7.6, and the handler example keeps state in
  `db`.** The example handler in the notes is the code an agent copies
  first, and nontainer's default kept state in `cache`, which in the
  studio rewinds with the workspace and is not published, while the
  run_python primer said to use `db`. nontainer 0.7.6 lets an embedder
  replace the example, and the studio's shows the `db` API: a
  `CREATE TABLE IF NOT EXISTS` that runs on every request, a `query` in
  the GET, an `execute` in the POST. The same release states each rule
  once across the tool descriptions and spells the app paths from the
  workspace root.

- **Apps are offline by rule, not only by habit.** The app policy's
  script hosts are empty: an app's scripts load from its own origin and
  nowhere else, under test_app and when published alike. Everything an
  agent is told to use is vendored, so the host list the agent used to
  read beside "do not load any of it from a CDN" is gone, and a stray
  CDN tag fails where the agent can see it instead of working in the
  preview and failing on an air-gapped machine.
- **The primer is a set of labeled paragraphs.** One paragraph per
  concern (preview, skill first, verify, uploads, reply artifacts, turns
  and publishing, tests, and the gated versioning and delegation
  pieces) instead of one unbroken block, with the same content.

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

- **The skill lists what `vendor/` holds.** An agent cannot `ls` or
  `grep` the vendored libraries, since they are served with the app but
  are not in its filesystem, and an agent asked what it missed named
  exactly that: which libraries exist, under what import names, at what
  versions. `references/vendor.md` is the listing it would have made —
  every file with its size and version, the bare import names and the
  file each resolves to, the icon names, the theme's custom properties
  and what `house/theme` exports — generated from the served files by
  `scripts/vendor_inventory.py` and checked against them by a test, so
  updating a library without updating the listing fails the build. The
  skill and the frontend notes point at it.

- **`testdb`, and a `db`-backed handler in the skill.** A test of a
  handler that keeps state in `db` has nowhere safe to put rows: the
  live store is what every published version serves over, and the
  sandbox refuses `sqlite3` because a connection's own SQL reaches the
  host filesystem beneath the workspace. `testdb` is a second store
  with the same three methods plus `reset()`, empty and in memory, held
  host-side and handed to `call(..., db=testdb)`. The skill gains a
  second reference pair, `api-scores.py` (a table created on every
  request, a validated and clamped param, a POST that inserts) and
  `test-scores.py` against `testdb`, copied for apps whose users create
  or change things, and the verification test runs them with the rest.

- **The reference app keeps its filters in the URL.** State that
  changes what the page shows is read from the query string on load and
  written back on change, merged into the params the page was loaded
  with (the studio's own cache-busting `v` survives) and with
  `replaceState`, so a reload keeps the user's place, a link carries a
  view, and a test can open the page at a state. `format.js` gains
  `filtersFromSearch` and `searchWithFilters`, the vitest reference
  covers the round trip and the kept foreign param, and the skill's
  Frontend section states the rule with the two details that matter:
  merge rather than rebuild, and push only for a view change.

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
