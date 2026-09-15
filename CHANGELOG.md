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
- **A delegate whose branch was swept is not something a turn can
  deliver.** Retention for a delegate's branch is an idle TTL, and when
  a sweep takes one the job's row stays as `expired` with its answer
  dropped — asking for it raises. The studio counted such a job in the
  rail's ⑂ badge and sent the next turn to collect an answer that is
  gone; because delivery is recorded by the transcript rather than by a
  flag, nothing would have taken it off that list. It is dropped the way
  a cancelled job is now, from the count and from the delivery alike,
  and a sweep landing mid-delivery skips the job for good rather than
  retrying it every turn. Nothing schedules the sweep yet; this is what
  the studio does when something does.
- **The primer asks the function that did the wiring.** Whether the
  agent can type `ws-git` was being re-derived from two runtime flags in
  two places — the primer's delegation half and a delegate's brief —
  where the call that registers the verb answers exactly that question.
  The session records its answer and both read it, so one session cannot
  be told two things about one verb.

### Added

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
