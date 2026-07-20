---
name: building-apps
description: deep guide for building /workspace/app web apps — architecture, debugging endpoints, frontend patterns, verification strategy
---

# Building apps in this workspace

The tool descriptions cover the contract basics. This is the deep
guide: read it before building anything beyond a trivial page, and
come back when debugging.

## Architecture that works

- Convert big source data ONCE (run_python -> parquet under
  /workspace/app/data/), then handlers read the parquet. Never re-parse a big
  CSV per request.
- Shared backend code goes in /helpers/<mod>.py, imported QUALIFIED:
  `from helpers.mymod import fn`. Imports resolve from the workspace
  root — a bare `import mymod` will not find it.
- Handlers are VERB functions only: get/post/put/delete/patch. A
  `def query(req)` or `def search(req)` is NEVER called by requests —
  read filters from req.params inside a verb instead. (Dispatch notes
  stray non-verb functions in /workspace/app/logs/api.log.)
- Module-level caches (`_DF = None` + lazy load) persist per process:
  cheap and effective for read-mostly data.

## Data gotchas (they 500 in production, not in your head)

- NaN in object columns: `sorted(df[col].unique())` dies comparing
  float NaN with str. Use `sorted(df[col].dropna().unique())`.
- Numpy types don't JSON-serialize: wrap with int()/float() or use
  `df.to_dict(orient="records")` after `.astype(object)` care.
- Error responses are JSON: `{"error": ...}` — your frontend's
  res.json() will parse them; check `res.ok` and show `data.error`.

## Frontend

Plain HTML + DOM + fetch is the most reliable pattern. RELATIVE urls
always (`fetch('api/x')` — the app serves under a prefix, absolute
paths 404 with a hint). If you want components, copy
references/preact-app.html from this skill EXACTLY — ES modules from
esm.sh, never UMD builds with guessed globals.

Scripts may only load from: esm.sh, unpkg.com, cdn.jsdelivr.net,
cdn.plot.ly, cdn.tailwindcss.com. Anything else is blocked (test_app
names blocked URLs in its [rejected requests] section).

## Debugging loop

1. `curl api/x` in the terminal — instant, no server. `-i` shows
   status+headers, `-w '%{http_code}'` prints the code.
2. `tail /workspace/app/logs/api.log` — handler tracebacks, prints, and
   dispatch notes land there.
3. test_app for the frontend: page errors carry file:line for runtime
   errors; parse errors mean bisecting your <script> blocks.

## Verification that means something

- Assert on DATA-BEARING elements: a count that isn't '0', a chart
  container with children — not just static text that renders even
  when every fetch failed.
- Exercise the interactive flow: click a filter, wait, assert the
  result region changed.
- If an assert fails, fix the app, not the assert. A weakened
  assertion (`x !== '0' || x === '0'`) verifies nothing.
- Screenshot at the end; the human sees the preview live either way.
