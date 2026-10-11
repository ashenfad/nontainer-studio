# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- **Sessions can run on agex: `NONTAINER_STUDIO_LOOP=agex`.** A new
  session runs on the loop the knob picks (agno by default), one with a
  conversation on the loop that wrote it, and a delegate on its
  parent's; an agex session has the same tools as an agno one. Needs
  the `agex` extra.

- **The dummy test model runs on agex too.** `NONTAINER_STUDIO_MODEL=dummy`
  reads the same script on either loop, and the server, delegation and
  browser tests run on both.

- **Agents can find and ask the human's other sessions.** A new
  `sessions others` action lists every session but the asker's, most
  recently active first: the name `fork_from` takes, the title, when it
  last committed, its delegate count, and each app it published with
  the app's description. Delegates aren't listed. An agent asked to put
  a question to another session had only bare branch names from
  `ws-git branch` to choose from. The delegation primer now teaches
  `fork_from=<session>` (nontainer 0.8.13 takes a bare session name)
  and points at `others` for finding one.

- **`others` says which sessions a session asked.** A session that only
  asked another one about its work gets its title from that work, so it
  could read exactly like the session that did it. One agent mounted two
  identically titled sessions' trees to tell which had the files. The
  row of a session that asked now ends with "asked golden-goblin". Each
  delegate's manifest record keeps the session it was forked from, when
  that wasn't its parent.

- **Long conversations are compacted.** Past a budget of 60% of the
  model's context window, every earlier turn is folded into one summary
  the agent writes itself, and it is sent that summary and the turns
  since rather than the whole conversation. The transcript keeps every
  turn, and a marker where the fold happened ("Context compacted · 12
  earlier turns summarized for the agent") opens to the summary. It is
  nontainer 0.8.8's compaction, so the fold lives in the session's
  branch: an edit to before it takes it back. The summary request
  reuses the agent's own prompt, so it is mostly read from the cache.
  `NONTAINER_STUDIO_COMPACT_TOKENS` sets the budget, or `off`. Requires
  nontainer 0.8.8. This is the bound that dropping tool-result
  compression left for later: since the agent is sent every earlier
  turn, nothing else keeps a long session inside the window.

- **The agent can make music.** `media.music(prompt, path,
  length="clip")` composes with Google's Lyria 3 on OpenRouter and writes
  an MP3. A clip is about 30 seconds for $0.04; `length="song"` follows
  the length the prompt asks for, roughly, for $0.08. It returns the
  music's length in seconds, read from the file, and a vocal track's
  lyrics line by line, with the second each starts where Lyria times
  them (a clip's lines; a song's come untimed). Like the other `media` calls,
  a list makes several at once. The video skill says how to lay a bed
  under the narration: a quiet clip (`data-volume`) that fades in and
  out.
- **The video skill covers narration and long videos.**
  - **A narrated-scene template** (`references/narrated-scene.html`):
    one scene in a file of its own, carrying its narration. Inside a
    scene file, sound clips and CSS animations are timed from the
    scene's start (checked in the browser), while Anime.js and WAAPI
    run on the video's clock, so a scene built before its place is
    known animates with CSS only. Its styles are scoped to its id, since
    every scene lands in one page.
  - **The narration section** says `media.speech` writes the file
    itself, as WAV (so name it `*.wav`); that `media` paths count from
    `/workspace` while HTML paths count from `app/`; and that the voice
    sets a scene's length.
  - **A narration check:** each clip loaded, and a played moment puts
    the voice where the scene's timing says. A paused seek leaves a
    clip's `currentTime` at 0, so the check plays.
  - **"Long videos with delegates":** a scene per delegate. Set the
    shared composition first, give each delegate its own files and a
    task that stands alone, end the turn and let answers wake you,
    merge each scene back, and stitch. The section is shown only to
    sessions that can delegate.
- **Skill text can depend on delegation.** `<!--if:delegation-->` blocks,
  beside the executor's `<!--if:commands-->` ones, are kept only in
  sessions with the `sessions` tool and `ws-git`.

- **You can see delegates at work.** A parent waiting on its delegates
  has ended its turn, and nothing said it was not done unless you
  thought to open the ⑂ listing.
  - **A strip above the composer** shows one chip per delegate that is
    out: its name, how long it has been running, and its last step
    ("Ran Python …", "thinking"), until its answer reaches the parent.
    A chip opens the delegate's live transcript.
  - **The rail's ⑂ badge pulses** while a session's delegates are
    running.
  - **`sessions` work lines say what happened:** "Asked intro: Scene
    1 …", grouped as "Asked 3 delegates", instead of "Sessions ask".
  - **The delegates listing carries what the strip needs:** each row's
    task, start and finish times, whether its answer was delivered, and
    a running delegate's last step, with long arguments trimmed. The
    session list counts running delegates.
- **With waking off, no wake notice.** With
  `NONTAINER_STUDIO_DELEGATE_WAKES=0` the transcript said answers had
  "already started as many turns as they may", as if a budget had run
  out. Waking off means answers wait for the human, so nothing is said.

- **A delegate's answer wakes its parent.** Before, an answer that
  arrived after the parent's turn ended waited for the human's next
  message, so "build three scenes, then put them together" stalled until
  someone nudged it.
  - **Now the answer starts the parent's next turn itself.** That
    happens straight after a turn ends, for answers that landed after its
    last tool call, and within about a second for an idle session. While
    the parent is working, answers still ride in on its next tool result.
  - **A woken turn opens with a new `wake` event** instead of a message
    from the human, so it is no edit anchor. The model gets the answers
    plus a note from the mechanism saying nobody spoke.
  - **Guards:** a stopped or errored turn turns waking off until the
    human writes. `NONTAINER_STUDIO_DELEGATE_WAKES` (default 10, `0` for
    off) bounds woken turns between human messages, and the transcript
    says once when it runs out. A delegate is woken by its runner
    instead: a reply it gives while its own delegates are out is treated
    as waiting, and its answer is the reply it gives once their answers
    are in.
  - **The agent is told** that answers come to it on its own, so it ends
    its turn instead of polling.

- **The agent can make images and speech.** When `OPENROUTER_API_KEY`
  is set, an agent session's Python has a `media` host object. The new
  `NONTAINER_STUDIO_MEDIA` setting turns it off.
  - **`media.image(prompt, path, transparent=False, …)`** writes a PNG
    and returns its size, whether it has alpha, and its cost. `transparent=True` gives a real
    alpha channel. That is why the model is OpenAI's
    `gpt-image-2.5-sunburst`: Gemini's image models fake transparency
    with a painted checkerboard. `references` pass earlier images back
    in, to keep a character consistent or to edit an image.
  - **`media.speech(text, path, voice="Kore")`** writes a WAV from
    Gemini text-to-speech and returns its length, which a video needs to
    time its scenes. Bracketed direction such as `[whispers]` steers the
    delivery. The agent is told about ten of the 30 voices, and an
    unknown voice is refused with the full list.
  - **A list of dicts makes several at once,** and a failed item holds
    its error in its slot.
  - **Files, not bytes.** Each call writes into the workspace and
    returns JSON facts. Under dud, bytes inside a dict or list can't
    cross back from the guest, and a batch has to.
  - **The making-videos skill has a narration section:** voice the
    script in one call, size scenes from the returned lengths, and place
    each line as an audio clip.


- **The agent can search and read the web.** When `OPENROUTER_API_KEY`
  is set, an agent session's Python has a `web` host object. The new
  `NONTAINER_STUDIO_WEB` setting turns it off.
  - **`web.search(query, deep=False)`** answers from a Perplexity search
    and lists its numbered sources. `deep=True` uses the multi-step
    search, and a list of queries runs concurrently.
  - **`web.fetch(url, question)`** answers a question from one page. A
    small model reads the page through OpenRouter's `web_fetch` tool,
    with Parallel extracting its main content. A list of URLs is read
    concurrently, with one question for every page or one per page.
    Parallel was chosen by
    measurement: Exa served a days-old copy of a GitHub README, and the
    raw fetch drowned the model in 124k tokens of HTML.
  - **It returns an extract, not the page.** OpenRouter gives fetched
    content only to a model, and a model asked to repeat a page
    verbatim quietly shortened a 10k-character README to 2.7k.
  - **Agent sessions only, forks included.** Published apps never get
    it, since they serve anyone with the link.
  - **A longer Python timeout comes with it,** 180 seconds instead of
    30. Waiting on a host call counts toward the limit, and a deep
    search takes 20–40 seconds.

- **A skill about the stack itself.** `nontainer-ecosystem` tells the
  agent what it is running on: nontainer-studio, nontainer, termish,
  monkeyfs, sandtrap, kvgit, reprobate and dud, with a short overview of
  how they fit.
  - **Each package's README comes with it,** under `references/`. They
    are read when a session is seeded, from the metadata of the copies
    installed here, so they match the versions the server runs. A
    package that is not installed, such as dud without its extra, is
    skipped.
  - **Existing sessions get it too, and stay current.** The top-up pass
    that adds new seed files to older sessions writes the READMEs as
    well. On each open, a README the agent hasn't edited is replaced
    when its package was upgraded and removed when it was uninstalled.
    A hash in each README's first line tells an unedited one apart; one
    the agent edited is left alone.

### Changed

- **Turns drive the agent through a `TurnDriver`.** `turns.py` reads
  nontainer's turn events from a session's driver (`drivers.py`), and
  `AgnoDriver` is the agno loop, so a second loop can sit behind the
  same turns; nothing a person sees changes.

- **Runs on a pinned commit of nontainer's main until its next
  release.** CI installs with uv, so it tests that commit too.

- **Requires nontainer 0.10.0.** Its async delegate runners and
  `until_settled` are what the delegation below is built on.

- **Delegates run on the server's loop.** Each delegate is a task beside
  the human's turns rather than a thread with an event loop of its own,
  and waits on its own delegates through nontainer's `until_settled`.

- **Shutdown cancels a delegate mid-turn.** Its transcript still says
  the studio shut down, and its job now ends cancelled rather than
  failed.

- **Requires nontainer 0.9.0, which stores the conversation on a
  harness-neutral plane.** A session's conversation moves from
  `__agno__/` to `__conversation__/` on its next turn, in that turn's
  own commit. After that, nontainer releases older than 0.9.0 can't
  read it. The studio now reaches the conversation through core:
  - **A delegate that inherited a conversation** is recognised from the
    conversation index (its runs, and the session it was forked from),
    not from agno's session record.
  - **A fresh fork from the rail** is `ws.fork(inherit="fresh")`, as a
    delegate's is. The child holds no conversation, and the agent
    starts one on its first turn. It used to keep agno's session record
    with its runs cleared, which took a second commit.
  - **Deleting a session** no longer clears its conversation first. The
    branch deletion takes it with everything else.

- **Asking another session carries its conversation by default.** The
  `sessions` tool's `inherit` is now left unset unless the agent passes
  one, so nontainer's default applies: a delegate forked from another
  session (`fork_from`) is the agent that was there, its conversation
  included, and a delegate of this session still starts fresh. The tool
  passed `"fresh"` before, so a question put to another session was
  answered from its files alone unless the agent knew to pass
  `inherit="full"`.

- **Session titles get the rail's whole width.** A row's fork and
  delete buttons were hidden until hover but still took their space,
  and the delegate count always showed beside the title, so every
  title was cut short.
  - **At rest:** a row is now its dot and its title. Its actions (the
    delegate count, fork, delete) sit in a tray over the end of the
    title, shown on hover, on keyboard focus within the row, and while
    a delete is armed. Hidden, the tray lets clicks through, so the
    whole title still switches sessions.
  - **Delegate status moved to the dot,** so it still shows at rest: a
    solid ring means answers are waiting for the session's next turn,
    and a pulsing ring means delegates are still working.

- **Browser checks and trial writes can use `testdb`, with the live
  data in it.** nontainer's test_app takes `bind={"db": "testdb"}` and
  `ws-curl` takes `--bind db=testdb`, handing the handlers the in-memory
  `testdb` where they read `db`. `testdb.reset(copy=True)` makes it a
  copy of the live store, every table and row, so a check sees
  realistic data and nothing it writes reaches the live one.
  - **Why the copy:** with explicit binding, one agent skipped it
    because seeding an empty store with realistic items was a chore. It
    checked against the live store and restored it by hand after every
    run.
  - **`db.reset()` is refused:** it would empty the live store.
  - **Teaching:** the skill, the db primer and a delegate's brief teach
    copy-then-bind, and say never to undo test writes on the live `db`
    by hand. A delegate had run `DELETE FROM groceries` there to tidy up
    after an unbound request.
  - **`testdb` isn't test-only:** the skill no longer says so, since
    run_python has it too. Only `call` is.

- **The building-apps skill covers two more things live runs tripped on.**
  - Setting an input's `.value` from test_app's `eval` bypasses React.
    An app posted an empty due date that way; the skill now says to use
    test_app's `type` and `select` actions.
  - `CREATE TABLE IF NOT EXISTS` never adds a column to a table that
    already exists in the live `db`. The skill now says a new column
    needs `ALTER TABLE ... ADD COLUMN`, guarded by `PRAGMA table_info`.
    An agent caught this only because it had written it into its brief.

- **The building-apps skill puts what bites first.** In live runs,
  delegates read it with `head -n 60` or `head -n 100` and parents with
  `head -n 200`. The skill was 480 lines, and what sat past those points
  went unread: an agent reached for `node`, and another left test rows
  in the live `db`. `SKILL.md` is now about 150 lines:
  - The rules that have cost turns come first, one line each, under
    tests and tools, handlers, and frontend. The first hundred lines
    hold all of them.
  - Then the reference copy block, the definition of done, and what
    makes a verification mean something.
  - The longer explanations moved, verbatim, into
    `references/handlers.md`, `frontend.md`, `testing.md` and
    `debugging.md`, and each rule names the file to read.
  - The icon list lives only in `references/vendor.md`, which is
    generated from the bundle.
  - Conditional blocks are now resolved in every markdown file of a
    skill, not only `SKILL.md`, so the termish note in `debugging.md`
    reaches only sessions whose terminal is termish.
  - A test keeps the rules inside the first hundred lines and the page
    under 160.

- **Starter skills are mounted read-only, not copied in.** Each session
  used to get its own copy of `skills/` in its tree. ws-git saw it, so a
  delegate's `ws-git add -A` swept the whole directory into its commit,
  and every session versioned a copy that drifted from what the server
  ships. Now each set a session can be given (by executor, delegation
  and ws-git) is resolved once into `<store>/.skills/<hash>/` and
  mounted at `/workspace/skills/<name>` with nontainer 0.8.10's
  `skills.mounts`. Starter skills are listed and readable but never the
  session's files: writes are refused, nothing versions them, and a
  package upgrade reaches the next session opened. A skill the agent
  writes is its own file beside them. In an older session, the mount
  shadows its old copies, so it reads the current text. The per-session
  rewrite and the top-up pass are gone.
- **Delegate answers are the final reply.** A delegate's answer joined
  every word it wrote in its turn, so notes it made between tool calls
  were glued, with no space, onto the front of the answer ("Use
  top-level await.Path is wrong; try relative.The v1 content is
  written…"). The answer is now what it wrote after its last tool call.
- **Sessions ignore tool caches.** Sessions open with nontainer's
  `ignore=` set to `__pycache__/` and `.pytest_cache/`, so those are
  never work; anything outside the workspace root already isn't.
- **The delegation primer** says to sequence dependent work, to give the
  skeleton a thin end-to-end check each delegate runs before answering,
  and to ask how each tested where its piece meets the others. A short
  edit-run-read loop is cheaper done yourself. The building-apps skill
  says a module worker must set `onmessage` before any top-level
  `await`. All of these come from an agent's retrospective on building a
  game with six delegates.
- **The building-apps skill answers three wrong turns from a delegated
  build.** The scores reference read `req.require("name")` with a note
  that it answers a 400 only when the field is missing, then checked the
  type again. A delegate took that to mean `require` checks presence
  only. Its `req.require("score")` then refused the number 10, because
  the type is `str` unless you pass one. The reference now says so and
  names `req.require("score", int)`, and its test refuses a name that
  is not text. The skill also says there is no `node` (JavaScript runs
  through `ws-vitest` and `test_app`), and that `call` and `testdb`
  exist only inside a `ws-pytest` run, not in run_python.
- **Requires nontainer 0.8.14**, for `fork_from` with a bare session
  name and an `inherit` that follows it (0.8.13), and for diffs and
  answers that keep a delegate forked from another session apart from
  that session's files (0.8.14).

- **No tool-result compression.** The studio no longer has agno
  summarise old tool results at a context watermark, and
  `NONTAINER_STUDIO_COMPRESS_TOKENS` is gone, along with the `tiktoken`
  and `tokenizers` dependencies it needed. Now that every earlier run
  is sent, it would have worked against the prompt cache: agno sends an
  earlier run as copies it does not store, so a result first summarised
  in a later turn was summarised again on every turn after. That meant
  a model call each time, and a reworded summary that changed the
  prompt. Keeping it working needed more machinery than it was worth.
  Compaction, which folds older turns into one summary for the model
  while the transcript keeps everything, is the planned bound
  (nontainer's `docs/compaction.md`). Until then nothing shrinks the
  context, and a session that outgrows the model's window fails with
  the provider's error.

- **Requires nontainer 0.8.7** (termish 0.2.1, monkeyfs 0.2.5).
  - **Merging a delegate's work holds up.** `cd /workspace && ws-git
    merge x` in one call lands rather than being refused with advice
    for the host. A delegate left in the middle of an unresolved merge
    is refused, naming the files that still carry conflict markers,
    where before its markers were merged in as content.
  - **`ws-git add`** works under git's name: `add -A`, `add .` and
    `add <paths>`.
  - **The shell is closer to GNU.** `>/dev/null` discards, grep and
    sed read basic regexes without `-E` (`\+`, `\(...\)`), grep exits
    1 when nothing matches, and `[ -f x ] && ...` works. `rm -r`
    removes the directories it empties, and `mkdir` refuses a path
    that exists.
  - **A handler sees the same request headers** from `ws-curl`,
    ws-pytest's `call` and the served app.

- **Delegates at work line up.** The strip above the composer showed a
  pill per delegate, as wide as its last step, so several of them wrapped
  raggedly, jumped between rows each time a step changed, and broke a
  hyphenated name across two lines. Each is now a card in a grid of
  equal columns: name and time on top, the step under them, cut short
  rather than widening the card. A line above counts them by status ("3
  delegates working · 1 answered · 1 failed").

- **Delegation says what a delegate starts from, and whose store it
  writes.** An agent that delegated a game forked its delegates before
  writing the frontend they built against, then concluded that a fork
  sees only what is committed. One delegate tried its endpoint with
  `ws-curl` and left a row on its parent's leaderboard.
  - **The primer** says a delegate starts from your tree as it is the
    moment you ask, committed or not, so write what it builds against
    first. It also says a delegate shares your `db`, and when to choose
    `inherit="fresh"` (a separable piece, built to a contract in the
    task) over `"full"` (this conversation up to your last finished turn,
    for a second attempt at something you have been working through).
  - **Every delegate's brief** says its `db` is its parent's own store,
    not a copy, and to test with `call(..., db=testdb)` rather than a
    `ws-curl` request that writes.
  - **A delegate forked with `inherit="full"`** opens on its parent's
    conversation, where the task reads like one more message from the
    person. Its brief now says first that the conversation above is its
    parent's, that the parent is speaking, not the person, and that it
    should neither address the person nor carry on the plan above. A
    resumed delegate, and a `fresh` one, are not told this.
- **A delegate need not think about ws-git.** Its brief used to explain
  that its work arrived as a named commit only if it ran `ws-git
  commit`, that anything written after was left out, and that a merge
  would be refused. nontainer 0.8.6 (now the floor) commits whatever a
  delegate wrote past its last commit when it answers, and never counts
  app logs or test_app screenshots as work. So the brief now says
  everything it writes is its answer, its own commits are checkpoints,
  and its logs and screenshots stay with it. A delegate that checked its
  scene with test_app after committing used to have its merge refused;
  it merges cleanly now.
- **A delegate's answer is one line in its parent's transcript.** It
  says whose it is (the short name its parent gave it), what became of
  it, how long it worked and the first line of its answer, and opens the
  delegate's own view, as a chip above the composer does. The whole
  answer was inlined before, and in a transcript long enough to scroll
  each card was squeezed to a sliver with a scroll area of its own. Once
  the retention sweep has taken a delegate there is no view to open, so
  its card says it expired and unfolds the answer it left instead.
- **The way back from a delegate's view is plain:** a `←` before the
  breadcrumb, and "← back to <parent>" in the bar where the composer
  would be.
- **`keep` says what it is for.** Beside it, the bar says when the
  retention sweep may take the delegate ("expires in 23h"); a kept one
  reads "kept". With the sweep off, neither is shown. Delegate rows
  carry the time as `expires`.
- **The building-apps skill says the terminal is not bash** (on the
  default executor): which shell features termish has, and that
  `{ …; }` groups and subshells fail confusingly. Agents hit this twice
  while writing files.
- **The fonts note no longer says "Latin characters only."** Other
  characters, emoji included, fall back to the viewer's system fonts;
  an agent had read the old wording as "emoji will not show" and removed
  them. The video skill also says small text and emoji can look broken
  in a grid's small frames when they are fine.

- **Waking a parent no longer polls.** Delegates' answers wake an idle
  parent the moment they are recorded, through the session helper's
  `on_answer` in nontainer 0.8.4 (now the floor), instead of a watcher
  checking every live session once a second. A delegate waiting on
  delegates of its own blocks on the helper's `wait()` instead of
  checking every tenth of a second. A turn looks for answers once more
  after it lets go of the session, so one arriving in that moment is not
  left for the human.

- **`sessions.py` and `server.py` are split into modules,** with no
  change in behaviour. `sessions.py` was 5,574 lines and is now the
  registry's core; `Registry` mixes in the rest from `manifest.py`,
  `titles.py`, `skills.py`, `delegates.py` and `publishing.py`. The
  environment settings went to `config.py`, the agent-facing text to
  `prompts.py`, `Session` and `Db` to `session.py`, and the turn engine
  out of `server.py` into `turns.py`.
  - `from nontainer_studio.sessions import Registry` works as before.
    Code importing other internals from `sessions` or `server` (`Db`,
    `apps_config`, the primers, `_run_turn`) imports them from their
    new modules.

- **The Claude picks are Sonnet 5.5 and Opus 5.5.** This covers the
  direct `anthropic` provider (`claude-sonnet-5-5`, the new default, and
  `claude-opus-5-5`) and OpenRouter (`anthropic/claude-sonnet-5.5`, the
  new default, and `anthropic/claude-opus-5.5`). Any other model id
  still works as a spec.

### Fixed

- **An agex run the studio stops resuming is closed with its note.** When
  every resume fails, or a stop lands while it waits, the run ends with
  the note agno's gets, so the next turn's model knows where its work
  stopped.

- **Tool calls pair by id, and a failed one says so.** Parallel calls
  of one tool no longer swap results (`call_id`), and a call that
  raised shows as failed (`is_error`).

- **The transcript takes events from any thread or loop.** A follower
  no longer misses an event published from another loop, such as a
  delegate's when no server runs it.

- **The strip shows a delegate from the moment it is asked.** The
  listing left out a run that had not yet written its record, so a
  strip that refreshed on the ask could miss a delegate that then
  answered without anything telling it to look again.

- **A delegate forked from another session is told whose fork it is.**
  Its header read "Your workspace is a fork of `<asker>` at commit
  <c>", but the commit was the other session's, so the delegate was told
  it held the asker's tree. It now names the session it was forked from
  ("a fork of `golden-goblin` at commit 935ab643, not of `able-krill`"),
  and the asker as the one asking.

- **A provider outage gets more than one resume, and an error that
  says it was the provider.** A run that ended in a provider error was
  resumed once, five seconds later. Gemini through OpenRouter stalled
  for about two minutes, then stalled again on that resume, and the
  turn ended with "The operation was aborted". The person had to type
  "go on" to a provider that was fine by then, and the agent blamed
  the delegates that had just answered. A turn now resumes up to three
  times, after 5, 30 and 60 seconds (`RESUME_BACKOFFS`), each later try
  saying so. A stop during any wait is honored. When every try fails,
  the error, and the note the model reads, say "the model provider
  failed 4 times in a row (last: …). Everything up to here is kept;
  send a message to continue."

- **A delegate that stops on a lead-in is asked to finish.** A
  delegate's answer is the prose after its last tool call. One wrote
  "All 32 green. Live smoke check (GETs only — no writes to live
  `db`):" and its turn ended, so that line was its whole answer and the
  report it meant to write never reached the parent. A reply ending in
  a colon is now treated like no reply: the delegate is nudged, quoting
  the line, to finish the step and report. If it runs out of turns, the
  lead-in comes back with the capped note rather than being dropped.

- **A delegate sent back for changes is heard from again.** Studio
  recorded a delegate's answer as delivered by the delegate's name. A
  delegate resumed with a second task answers under the same name, so
  once its first answer was in the transcript the next one counted as
  delivered too. It never woke an idle parent and never reached it,
  which breaks a review loop where the parent asks for changes and
  waits. A `delegate` event now records the run its answer came from
  (`started`, `finished`), and delivery matches on it. Events written
  before this still count for the answer that had landed by then. Each
  answer's card shows how long its own run took, where it used to show
  the delegate's latest run.

- **The agent remembers the whole conversation.** It was sent only its
  last three runs, agno's default, and a delegate's answer wakes the
  parent as a run of its own. After one round of delegation the agent no
  longer had the turns in which it asked: asked how delegation went, it
  said it had never delegated, and pieced the story back together from
  `ws-git log`. The window also moved on every run, which changed the
  start of the prompt, so from the fifth run on none hit the prompt
  cache, not even a wake seconds after the run before it. Every
  earlier run is sent now.

- **An edit to before a delegate was asked for unsays it.** Rewinding to
  an earlier message took back the agent's turns after it but not the
  delegates they asked for. Their chips stayed above the composer and
  their answers counted as waiting. Worse, the edited turn was handed
  the answer to a task that, in the conversation that now existed,
  nobody had given. Now delegates asked at or after the edited message
  are recorded as undone. They are never delivered, a running one is
  cancelled, and they vanish from the strip, the rail and its badge,
  across restarts too. Their branches stay and age out as usual, and
  resuming one by name brings it back. Each delegate's record now notes
  when it was asked for.
- **Speech direction is no longer read aloud.** The primer told agents
  to steer `media.speech` with bracketed direction in the text
  ("[whispers] It's here."). Gemini 3.8 reads its input as a verbatim
  transcript, so the voice read "whispers" aloud before the line.
  `media.speech` now takes `style`, how a line is said
  in plain words ("whispering, conspiratorial"), and sends it as
  Gemini's `speech_metadata.style` through OpenRouter's provider
  options. The primer, the video skill and the configuration doc say:
  the text holds only what is said; delivery goes in `style`; momentary
  sounds go inline in angle brackets (`<laugh>`, `<sigh>`, `<short
  pause>`) and are performed, not read; names are spelled the way they
  should sound. Checked by transcribing real clips: a bracketed
  direction was spoken, a `style` whispered without adding words, and
  `<laugh>` laughed.
- **A scrubbed video keeps its narration.** nontainer 0.8.5 (now the
  floor) serves app files in byte ranges. Without them a browser could
  not seek a sound clip, so after a scrub the narration played from its
  start, or stayed silent until playback crossed the clip's start again.
  This covers the preview pane and published apps alike.

- **A video in the preview pane plays its narration.** The pane and the
  published view now grant their frame `allow="autoplay"`. A video
  player plays its narration one frame down from the click, and sound
  may start only in a frame every frame above has granted it to. Without
  the grant, pressing play before the video had loaded played the picture
  in silence, while the same video in its own tab had sound.

- **Claude no longer stops after thinking, before acting.** Sonnet 5.5
  would end a turn mid-task with OpenRouter reporting a stop reason of
  `length` after about 16k tokens.
  - **The cause:** each Claude response was capped at 16,384 tokens,
    thinking included. Claude 5.x thinks adaptively and ignores a token
    budget, so the 4,096-token budget sent through OpenRouter did
    nothing: a hard turn could think for 15k tokens and run out before
    its tool call. A response with no tool call ends the agent's run.
  - **The fix:** a Claude response may now run to 64,000 tokens, or the
    model's own output limit if that is lower. The limit comes from the
    capability lookup directly and from OpenRouter's catalog; when
    neither answers, the cap stays 16,384. You pay for tokens generated,
    not for the cap. Session titles and app descriptions still use
    16,384 on the direct path, because they run without streaming and
    the Anthropic SDK refuses a longer non-streaming request.
  - **OpenRouter Claude is steered by effort now**, not the ignored
    budget, and reads the same `NONTAINER_STUDIO_EFFORT` (default
    `medium`) as the direct path.
- **Claude now caches its prompt, directly and through OpenRouter.**
  Anthropic models cache only when a request asks. Most other providers,
  including OpenAI, DeepSeek, Gemini and much of what OpenRouter routes
  to, cache on their own, which is why other models showed cache hits
  and Claude never did.
  - **The effect:** every turn of an agent loop re-sent the whole
    prompt at full price. Both Claude paths now send Anthropic's
    top-level `cache_control`, so each request caches its prefix (tools,
    system prompt, conversation so far) and the next turn reads it back
    at a tenth of the input price, after paying 1.25x once to write it.
  - **Measured end to end with sonnet-5:** a second run on a
    ~20.7k-token prompt read all of it from cache; before, neither path
    ever did.

### Fixed

- **Video checks no longer log a sandbox warning.** The video skill's
  reference player page framed the composition on its own origin (the
  HyperFrames player's default, `allow-scripts allow-same-origin`), so
  every `test_app` run on a video app logged *"An iframe which has both
  allow-scripts and allow-same-origin … can escape its sandboxing"*.
  - **Harmless, but worth fixing.** The page and the composition are
    the same app, so that sandbox guarded nothing, and the studio
    preview's own frame is opaque. But the warning was noise in every
    check, and one an agent might try to "fix".
  - **The fix:** the reference now sets `sandbox-origin="opaque"`, and
    the player drives the composition through messages.
  - **Checked** with a sub-composition too, in the preview (top level
    and inside the shell), on a published link, and under `test_app`.
    The skill says to keep the attribute, and the recipe test asserts
    no sandbox warning.
  - **Sound and video clips need `crossorigin` in opaque mode.**
    Without it the runtime cannot route their sound through Web Audio.
    It falls back to plain playback, losing fades, effects, groups and
    gain above 1, and logs `runtime_web_audio_bypass`. The skill now
    says to put `crossorigin` on every `<audio>` and `<video>` clip.
    Studio already serves app files with `Access-Control-Allow-Origin`,
    so with the attribute nothing is lost. A test covers both ways.

### Changed

- **The transcript shows the agent's work quietly, in plain words.**
  Studio now works the way Cursor does:
  - **Tool runs** are a muted line with a chevron, "Ran 2 commands,
    edited 1 file", instead of a pill with a dot and raw tool names.
  - **Thinking** is a line of its own, "Thought for 12s", or
    "Thinking…" while it streams.
  - **Detail opens in three levels:** the group, one line per step
    ("Ran `cat app.py`", "Edited app/app.jsx", "Tested the app ·
    failed · 6 steps · 1 screenshot"), then the step's output, with the
    same per-tool renderers as before.
  - **Each line sits where the work happened,** between the agent's
    prose: prose, "Ran 3 commands", prose, "Edited 1 file, tested the
    app". The prose and any artifacts are never folded.
  - **Work in progress** shimmers ("Running", "Thinking…",
    "Working…") instead of pulsing a dot.
  - **Screenshots** stay in their step. The step's line says it has
    them.
- **Every transcript event carries `ts`,** when it happened. The
  thinking durations are read off it; a transcript from before this
  change says "Thought" without a time.

### Fixed

- **An unpublish or repoint no longer fails a request mid-flight on a
  dud rung, or stalls the studio behind one.** The published-app cache
  closed a snapshot the moment its app was unpublished or repointed,
  while holding the registry lock. On `dud`/`dud-vm` that went wrong
  two ways:
  - a request that had looked the snapshot up just before hit a closed
    executor (`DudExecutor is closed`, a 500);
  - the close waited for any handler still running on the guest, and
    every registry operation waited behind it.

  The `/apps` mount now counts the requests holding each snapshot. A
  dropped snapshot leaves routing at once but closes when the last
  request lets go, off the lock. That is nontainer 0.8.2's documented
  eviction order: out of routing, then close.

### Changed

- **Requires nontainer 0.8.2.**
  - **The video skill checks a whole video in one call.** `test_app`
    at `viewport: "hd"` sees the 1920×1080 stage whole. Before, every
    screenshot lost its right third. The screenshots name a `grid`, so
    every scene and the crossfade come back as one captioned image
    that counts once against the screenshot limit. An agent making a
    7-scene video had needed three calls and never saw a full frame.
  - A test runs the recipe, taken from the skill, at that viewport,
    and checks it comes back as one 1920-wide image.
- **The skills' copy blocks make their directories first.** A fresh
  session has no `/workspace/app`, so the `cp` into it failed until
  the agent made it. Both `making-videos` and `building-apps` now start
  with `mkdir -p`.

### Added

- **Fonts are vendored, for apps and for the studio itself.** System
  fonts differ from machine to machine, so a page (and a video tuned
  frame by frame) laid out differently for each viewer, and an agent had
  nothing to choose from. `vendor/fonts.css` now serves seven families,
  each one variable font with every weight: Inter, Public Sans, Space
  Grotesk, Fraunces, Source Serif 4, JetBrains Mono and Archivo (with a
  width axis). Italics for the three text families. About 530 KB, all
  SIL Open Font License 1.1 with each family's license beside it.
  - `vendor.md` lists the families with what each is for, generated from
    `fonts.css`. The app notes name them, and the video skill's
    reference now sets its type in Inter from them.
  - **The studio's own page no longer calls Google Fonts.** Its Fraunces
    and Public Sans come from the same files, copied into the build, so
    an air-gapped studio looks the same as a connected one and makes no
    request off its own origin. The e2e video test now asserts that for
    the whole page, not only the app's frames. Public Sans italic is
    real now, where the browser used to slant the upright.

- **Agents can make videos, offline.** A video is an app: an HTML
  composition of timed scenes that the HyperFrames runtime plays on its
  own clock, shown by a `<hyperframes-player>` page with play, pause and
  a scrubber. It previews and publishes like any other app; nothing
  renders an MP4.
  - **Vendored:** the HyperFrames 0.8.92 runtime and player
    (Apache-2.0, license file beside them) and Anime.js 4.5.0 (MIT),
    about 700 KB.
  - **No GSAP.** HyperFrames' examples animate with it, but it is
    licensed under Webflow's own terms, and every vendored file stays
    under an OSI license. CSS keyframes, WAAPI and Anime.js are seeked
    by the runtime without it.
  - **A `making-videos` skill:** a working 10-second reference (player
    page plus composition, crossfading between scenes), the composition
    rules, transitions, animating with CSS and Anime.js, what breaks
    scrubbing, and a `test_app` check that seeks into each scene and onto
    a scene boundary, screenshots them, and scrubs backwards to compare.
    A test runs that check, taken from the skill, against the reference,
    and another shows it failing on two Anime.js steps on one property.
  - **Two guides adapted from HyperFrames** (Apache-2.0, license beside
    them): Anime.js v4 and CSS animations, with CDN tags pointed at
    `vendor/` and the CLI steps removed. A spike on a real model (18
    videos over three document sets) found these the two worth carrying:
    on a text-effects prompt, runs that read the Anime.js guide took
    151–265s against 568s without it; the craft docs were rarely read and changed nothing.
  - **Tips for the failures that spike found in every set:** scenes cut
    to black instead of crossfading (9 of 18 videos had an empty frame
    at a scene boundary), and two Anime.js steps on one property, which
    scrubs backwards wrong (2 of 18). With the tips and the new
    reference, six more videos had neither.
  - The agent's app notes name the video files and point at the skill.
  - An e2e test plays the reference in the preview pane, where the
    player's own iframe nests inside the sandboxed frame, and asserts
    no request leaves the studio's origin.

- **apache-arrow is vendored for pages.** nontainer 0.7.11 lets a
  handler return a DataFrame, Series or pyarrow Table and answers with
  an Arrow IPC stream when the request's `Accept` asks for one. A page
  needs a decoder for that, and the studio's apps have no network. So
  `apache-arrow@21.2.0` ships as `vendor/arrow.min.js`, the upstream
  UMD build byte for byte (`window.Arrow`). `vendor/arrow.mjs` is a
  module face over it, which `jsx-loader.js` maps `apache-arrow` to,
  so `import { tableFromIPC } from 'apache-arrow'` works in `app.jsx`.
  Its export list is generated from the build, and a browser test
  checks the two agree. Apache-2.0, recorded in the appassets README.
  A browser test covers the whole chain: a DataFrame handler, an
  `Accept` header, the vendored decoder, a rendered value.

- **`references/returns.md` in the building-apps skill.** Everything
  about returns and requests that SKILL.md does not need for the
  common case: tables and Arrow on both sides of the wire, the index
  rule, downloads, the response caps and what to do instead, the
  request side in full, and how to test each. SKILL.md points at it
  with the cases that need it, and the primer does too. Its
  examples were run against 0.7.11, and the page-side helpers are
  the ones the browser test runs.

- **A message typed while the agent works is queued, not refused.**
  The composer used to lock for the length of a turn and `POST /chat`
  answered 409, so a correction that arrived one second late waited
  for the work it was meant to redirect to finish. The seam that
  exists mid-run is the tool result, and nontainer 0.7.8's inbox is
  how a note gets there: the chat route now queues the message (202,
  with the note's id) and the agent reads it appended to its next tool
  result, framed as coming from the person it works for rather than as
  the tool's output. Nothing is interrupted and no message the model
  has already read is rewritten. The transcript records the delivery
  as an `interject` event in the slot it arrived in — visible, and
  NOT an edit anchor, since the turn it landed in began before it was
  said. The tool box keeps showing the tool's own output: the block is
  cut back off (`nontainer.inbox.split`) before the result reaches the
  transcript. A run that ends with the queue still full starts a
  follow-up turn with what is waiting, as an ordinary editable message
  — except after a stop or an error, where the notes stay queued for
  the human's next send, because a turn they stopped must stay
  stopped. `DELETE /api/sessions/{name}/queue/{id}` takes a message
  back while it is still waiting, and the session payload lists the
  queue, so a reload shows what is pending: the queue is the server's,
  not the tab's.

- **A delegate's answer that lands mid-turn arrives mid-turn.** The
  same delivery point carries answers the parent's own turn was too
  late to collect, through `Sessions.take()`. It is the same
  `delegate` event the between-turns path emits, so the delivery
  record, the rail's waiting count and an edit's re-delivery all keep
  working, and an answer taken this way is not delivered twice.

- **Compression keeps a queued message verbatim.** agno's tool-result
  compression summarises what it is given; a person's words inside a
  tool result must not become a paraphrase in the agent's memory. The
  studio's compression manager splits the block off, compresses the
  tool's own half, and re-appends the block byte for byte.

### Changed

- **Requires nontainer 0.8.1, and a turn starts the session's worker.**
  nontainer 0.8.1 starts nothing when a workspace opens: the worker
  behind `run_python`, or the guest on a dud rung, starts on the first
  execution. Switching to a session to read it no longer costs a worker
  or a VM. So that the first tool call does not pay for the start
  instead (about 1.5s on `dud-vm`), each turn starts it on a worker
  thread while the model reads the prompt. A failure there is only
  logged, because the first execution tries again and reports it.

- **Requires nontainer 0.8.0** (kvgit 0.4.1, monkeyfs 0.2.4).
  - **Upgrading an existing store is one-way, so back up the store
    directory first.** kvgit 0.4 reads it unchanged, but the first commit
    stamps it with storage version 4, and older studio releases then
    refuse to open it.
  - The agno db now uses the studio's own `Store` instead of a path.
    It reads through the same kvgit repository as the workspaces:
    one connection pool on whichever backend the store uses, closed
    with the store at shutdown.
  - `ws-git` status, commit, checkout and diff now read only what
    changed, so they no longer slow down as a workspace grows.

- **nontainer 0.7.11, and a skill that no longer casts.** Handler
  returns encode numpy values, dates and NaN by themselves now, inside
  the handler's sandbox, and an unencodable value's `BAD RETURN` line
  names its path. SKILL.md drops the `int()`/`float()` casts and the
  "NaN is not JSON, nothing stops you" guidance for a short returns
  table, and the reference handler drops `_cell()` and its casts. It
  sends its row sample as a frame whose named index is the `id`
  column, and its chart arrays with `.tolist()`, because a Series in a
  dict goes out as rows and a bare Index is refused. What stays is
  what is still true: `dropna()` before sorting a mixed column, the
  same shape for an empty result, and `None` for "no data" (a NaN mean
  now arrives as null by itself). SKILL.md is shorter than before.

- **A provider error resumes the turn where it stopped instead of
  restarting it.** A turn had three retry layers: the provider SDK's
  own, agno's model-call retry (which retries one call and keeps the
  turn's tool results), and agno's run-level retry, which restarted
  the whole run from the user message. The restart forgot every tool
  call the failed attempt made while the files those calls wrote
  stayed, so the studio rewound the workspace to the start of the turn
  to match — which threw away the attempt's work and, with it, anything
  else written during the turn, such as a file uploaded mid-turn. The
  run-level retry and the rewind are gone. A run that still ends in a
  provider error after the model call's retries is resumed in place,
  under the same run id, from its last tool result, after a short wait
  and a `notice` saying so. Once: if the resume fails too, the turn
  ends in an `error` and the run is kept in the agent's memory as an
  interrupted turn. A stop is never resumed, including one pressed
  during the wait, and an exception out of the run loop is not a
  provider hiccup and ends the turn as before. The dummy model gains a
  `!fail` directive so a script can make a model call fail.

- **agno 3 is a stated dependency.** The resume is agno 3's continue of
  an errored run, which agno 2 does not have; nontainer's `agno` extra
  guarantees only 2.1, so the studio now requires `agno>=3.0` itself.
  Every install already resolved agno 3.

- **An aborted run is kept by nontainer's `keep_aborted_run`, and the
  nontainer floor is 0.7.10.** The studio's own repair of an errored or
  cancelled run moved into nontainer as `keep_aborted_run`, which also
  writes agno 3's separate runs table. The studio calls it on every
  failing ending — stop, a failed resume, an exception, shutdown —
  through a wrapper that logs and never raises, and the notes it closes
  the run with are unchanged.

- **The nontainer floor is 0.7.9.** A minted app token can no longer
  begin with a dash, which is what made `ws-git worktree add`, `diff`
  and `checkout` refuse an origin tag about one time in 64: the token
  names the publication and the tag under it, and every verb reads a
  leading dash as a flag. The floor is what carries the fix into a
  fresh install.

- **The origin-tag mount test says what it saw.** It failed three
  times on CI since 2026-09-18, always on Python 3.13 and never
  locally, and each time the only evidence was a missing file: the
  guard on the terminal's answer accepted any text containing the word
  "worktree", which the verb's refusals also do. The test now requires
  the exact line a mount prints and reports the mounted tree beside
  the file it could not find. The dummy model no longer answers a
  tool call to an agent that offered it no tools, so the naming pass
  stops logging a missing `file_write` on every run of that test.

- **The README was split into a `docs/` set.** It had grown into four
  documents in one — a front door, a configuration reference, a design
  essay and a delegation manual — so the front door is now short and
  links [Configuration](docs/configuration.md), [Apps](docs/apps.md),
  [What owns what](docs/design.md), [Delegation](docs/delegation.md) and
  [Hacking](docs/hacking.md). Every claim was checked against the code on
  the way across; the HTTP routes and the event log's event types are
  documented for the first time.

- **Shutdown no longer waits out a delegate mid-turn.** Closing a
  session joins its delegate workers, and a delegate's turn could not
  be interrupted — `sessions cancel` discards an answer without
  stopping the loop — so Ctrl-C with a delegate working waited out as
  many turns as that delegate had left. A delegate's turn now runs on
  a loop the registry can reach from another thread, and closing asks
  every turn in flight to stop before it joins anything. A stopped
  turn takes the path a stopped human turn takes: the transcript says
  the studio shut down mid-run, the run is repaired so the child's
  memory keeps the work it really did, and the job resolves as
  `failed` with that same sentence as its answer rather than as prose
  that merely stops.

- **A delegate from before a restart is named on the parent's next
  turn.** The job table is per process and the branch is in the store,
  so a restart parts them: the manifest still records the child and
  `ws-git branch` still lists it, while every answer nobody had
  collected is gone. The parent had no way to learn that — its live
  helper lists no job, so `sessions list` read "no delegated jobs yet"
  over a branch the drill-down was still showing. One note per such
  delegate now arrives in the slot an answer would have, in the
  transcript and in what the model is sent: the task is outstanding,
  the branch is still there (`ws-git diff` / `merge` / `checkout`
  reach it), and asking again is `sessions ask`, since `resume`
  continues a job and the job is what went. Delivery is derived from
  the transcript like an answer's, so a rewind past the note
  re-delivers it and a restart that still shows it does not.

- **The `sessions` knob turns `ws-git` on with it.** Delegation
  without the verb is the degraded half of itself: the delegate's
  files stay on its own branch, the terminal has nothing that brings
  them over, and the primer tells the agent to ask for findings rather
  than edits. A studio configured to hand out delegation was still
  able to withhold the one thing that makes a delegate's work
  arrive. `NONTAINER_STUDIO_SESSIONS=1` now means the verb too;
  `NONTAINER_STUDIO_WSGIT` alone still means versioning without
  delegation. The degraded path stays for the session whose executor
  cannot carry the verb at all — the primer and a delegate's brief
  read what the session recorded, not what the knob asked for.

- **A delegate's turn spends at most sixty tool calls.** One turn is
  one agno run and one agno run is a tool loop with no bound of its
  own. A human's session needs none — somebody is watching it and the
  stop button reaches it — but a delegate's turn has neither, so a
  delegate that finds a rhythm it cannot break out of spends the
  session's budget on it and `sessions cancel` cannot interrupt.
  `NONTAINER_STUDIO_DELEGATE_TOOL_CALLS`
  (`Registry(delegate_tool_calls=...)`) caps the calls of a DELEGATE's
  turn, 60 by default; past it each further call comes back refused
  with a tool result saying the limit is reached, and the run goes on
  to its reply, so what it bounds is what a delegate does rather than
  how long it talks. Human sessions carry no limit, and `0` turns the
  cap off.

- **Delegation stops nesting after two hops.** Every session builds a
  delegation helper with four workers, and every delegate is a full
  agent on the parent's model with a helper of its own — so a delegate
  that delegates multiplies rather than adds, and nothing bounded it.
  `NONTAINER_STUDIO_DELEGATE_DEPTH` (`Registry(delegate_depth=...)`)
  counts hops from the session a human started, 2 by default: that
  session may delegate, its delegates may delegate, and the generation
  after them reads a refusal on `sessions ask` that names the way
  forward — do the task, answer with what you found. The refusal is
  the studio's and lands before the fork, so nothing is created to
  say it; `list`, `result`, `keep`, `cancel` and `published` are
  untouched. Depth is walked over the record of who forked whom, never
  off the dotted name, and the primer sentence about the cap goes only
  to the session it binds. `0` turns the cap off.

- **The version strip sits under the published app, and one pane
  serves both places it is shown.** The version list used to open as a
  modal over the preview, which covered the app it acts on: pointing
  the URL at another version or deleting one hid the frame that would
  have shown the result. It is now a strip under the published frame,
  bounded so the app keeps most of the pane, and the `published…`
  button is gone — the `published` segment of the preview's toggle is
  the way in. The frame and the strip are one component, shared by the
  preview's published mode and the rail's view of an app whose session
  is gone, so the two differ only in the bar above them: the toggle
  and the publish button in a session, the app's title and `close`
  without one.

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

- **Logs and screenshots no longer count as unpublished changes.**
  nontainer 0.7.11 leaves `app/logs/` and `app/screenshots/` out of a
  publish, but the publish button's count and the `changes` tab still
  compared them against the newest version. Every `ws-curl` or
  `test_app` run then showed up as app work waiting to be saved. Both
  now skip what a publish leaves out, a list read from nontainer
  (`nontainer.apps.dispatch.PUBLISH_EXCLUDE`) rather than restated.

- **The fork route's docstring said the app db is copied.** It is
  named: the child writes to the parent's db file, as `Registry.fork`
  says.

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
