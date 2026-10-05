---
name: building-apps
description: deep guide for building /workspace/app web apps — architecture, debugging endpoints, frontend patterns, verification strategy
---

# Building apps in this workspace

Read this page to the end before you build: it is short on purpose, and
every rule on it has cost an agent turns. Each rule names the file in
`references/` to read for more. Start from the reference app (below), and
an app is **done** when test_app, `ws-pytest` and `ws-vitest` passed and
your report quotes them, and the README is filled in (**Done**).

## Rules that cost turns when missed

**Tests and tools** (detail: `references/testing.md`,
`references/debugging.md`)

- Tests live in `tests/`, never under `app/`, which publishes.
  `ws-pytest -v` for Python, `ws-vitest` for JavaScript modules,
  test_app for the page.
- **There is no `node`**, nor `npm` or `npx`. JavaScript runs in the
  browser: through `ws-vitest`, or the page through test_app.
- `testdb` is an empty store beside the live `db`. A ws-pytest test runs
  `testdb.reset()`, then `call('scores', ..., db=testdb)` (`call` exists
  only there). Browser checks and trial writes use it too: test_app
  `bind={"db": "testdb"}`, `ws-curl --bind db=testdb` (seed it first).
- A 500 means the traceback is in `/workspace/app/logs/api.log`: read it
  before changing code. `ws-curl $APP_ORIGIN/api/x` tries an endpoint
  without the page; never plain `curl`, which reaches the network.
<!--if:commands-->
- The terminal is not bash: it is termish. Pipes, redirects, heredocs,
  `&&` and `||` work; loops, `$(…)`, `{ …; }` and subshells do not.
<!--endif-->
- When file_edit fails, copy the lines it shows you and retry file_edit.
  Find code with `grep -n`, not a Python loop.

**Handlers** (detail: `references/handlers.md`)

- `app/api/<name>.py` serves `api/<name>` (never `.py` in a url). Its
  VERB functions (`get`, `post`, `put`, `delete`, `patch`) are the
  methods; a second endpoint is a second file. `Request`, `Response` and
  `HttpError` are in scope with no import.
- `req.params` is `dict[str, str]`: `.get()` for an optional filter.
  `req.require("n", int)` for a required field, which answers a 400 when
  it is missing or not that type. The type is `str` unless you pass one.
- Return chart-ready data. A filter that matches nothing is the same
  shape with zeros and empty arrays, not an error, and "no data" is
  `None`, not `0.0`.
- The handler source re-runs on every request, so module-level caching
  does nothing. Convert big data ONCE to parquet under
  `/workspace/app/data/` and read that.
- Only `app/` publishes. Shared backend code goes in
  `app/api/_shared.py`, imported as `from app.api._shared import fn`; a
  module outside `app/` imports in the preview and fails once published.
- `db` is LIVE: the published app, every later version and every
  delegate use the same store. `CREATE TABLE IF NOT EXISTS`, and never
  leave test rows in it. That creates a table but never changes one: a
  new column on a table that exists needs `ALTER TABLE ... ADD COLUMN`,
  run when `PRAGMA table_info` does not list it yet.
- If an endpoint returns more than a few thousand rows, a file download,
  or anything that isn't JSON, read `references/returns.md` first.

**Frontend** (detail: `references/frontend.md`)

- One stack: MUI with React and JSX, compiled in the browser by
  `vendor/jsx-loader.js`. Write bare imports (`react`, `@mui/material`,
  `house/theme`) as in any React project. Never rewrite them as vendor
  paths or CDN urls.
- `import theme from 'house/theme'` inside `<ThemeProvider>` with
  `<CssBaseline />`. Never `createTheme`: it gives stock Material purple.
- Icons come from the bare package, `import { Delete } from
  '@mui/icons-material'`, and only the names in `references/vendor.md`
  exist. There is no `@mui/lab`.
- ONE `.jsx` file: only the entry `data-app` names is compiled. Plain
  JavaScript (formatting, fetch helpers) splits into `.js` modules like
  `format.js`, which is also what `ws-vitest` can test.
- Give every control a stable `id` or `data-key`, and every dropdown
  `SelectProps={{ native: true }}`: test_app drives the page by selector,
  and cannot drive MUI's default Select.
- In test_app, fill inputs with its `type` and `select` actions, never by
  setting `.value` from `eval`: React does not see that change, and the
  form submits what it had before.
- Render data through JSX (`{row.name}`), never as an `innerHTML` string.
- Grow in verified steps (one endpoint and one number, then the rest),
  and keep `app.jsx` small enough to rewrite when an edit gets hairy.
- `vendor/` is served with your app but is not in your filesystem, so
  `ls` cannot see it. `references/vendor.md` is its listing: files and
  versions, the bare import names, the icon names, the theme's tokens.

## Start from the reference app

The references are one WORKING app (a filtered summary endpoint, an MUI
page with a chart and a table, `format.js` for the plain JS). Copy them:

```sh
mkdir -p /workspace/app/api /workspace/tests
cp /workspace/skills/building-apps/references/app.html       /workspace/app/index.html
cp /workspace/skills/building-apps/references/app.jsx        /workspace/app/app.jsx
cp /workspace/skills/building-apps/references/format.js      /workspace/app/format.js
cp /workspace/skills/building-apps/references/api-handler.py /workspace/app/api/summary.py
cp /workspace/skills/building-apps/references/test-summary.py /workspace/tests/test_summary.py
cp /workspace/skills/building-apps/references/format.test.js  /workspace/tests/format.test.js
cp /workspace/skills/building-apps/references/README.md       /workspace/README.md
```

When the app's users CREATE or CHANGE things (a list they add to, a form
that saves), copy the `db`-backed pair too:

```sh
cp /workspace/skills/building-apps/references/api-scores.py  /workspace/app/api/scores.py
cp /workspace/skills/building-apps/references/test-scores.py /workspace/tests/test_scores.py
```

Then read what you copied and **cut it down to your data**: rename the
columns, delete what you don't need. The non-obvious parts already live
there: empty results, null aggregates, stable row keys, relative urls,
stable selectors, a themed chart, a testable dropdown.

## Done

An app is done when all of these are true, and your report to the human
says so with the evidence, not a summary of it:

1. `test_app` passed with data-bearing assertions (below), and the
   report quotes what it asserted.
2. `ws-pytest -v` ran and passed, and the report quotes its count line.
   The copied handler test is adapted to your handler or deleted.
3. `ws-vitest` ran and passed, and the report quotes its count line.
   The copied module test is adapted to your `format.js` or deleted.
4. `/workspace/README.md` is filled in from the reference: what the app
   does, the data, the endpoints, how to run the tests, and the
   decisions with their reasons.

The one escape is a stated waiver: if a tier has nothing to test — a
page with no handler, a frontend with no pure functions — the report
says which tier and why, in a sentence. Skipping without saying so is
the failure this list exists to catch. For a genuinely small app the
README may be two sections; it may not be missing.

## Verification that means something

- Assert on DATA-BEARING elements: a count that isn't '0', a chart
  container with children — not just static text that renders even
  when every fetch failed.
- Exercise the interactive flow: click a filter, wait, assert the
  result region changed.
- If an assert fails, fix the app, not the assert. A weakened
  assertion (`x !== '0' || x === '0'`) verifies nothing.
- Screenshot at the end; the human sees the preview live either way.
