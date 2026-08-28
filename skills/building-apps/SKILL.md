---
name: building-apps
description: deep guide for building /workspace/app web apps — architecture, debugging endpoints, frontend patterns, verification strategy
---

# Building apps in this workspace

The tool descriptions cover the contract basics. This is the deep
guide: read it before building anything beyond a trivial page, and
come back when debugging.

## Do this first

The references are WORKING FILES, not illustrations. Read both before
you write anything:

```sh
cat /workspace/skills/building-apps/references/api-handler.py
cat /workspace/skills/building-apps/references/chart-app.html
```

`api-handler.py` is filters in / chart-ready JSON out. `chart-app.html`
is dropdowns -> fetch -> plotly in plain DOM. They are a matched pair —
the frontend calls the endpoint the handler serves.

Copy rather than retype; it is one call instead of a few hundred lines:

```sh
cp /workspace/skills/building-apps/references/chart-app.html /workspace/app/index.html
cp /workspace/skills/building-apps/references/api-handler.py /workspace/app/api/summary.py
```

Then edit them down to your data — rename the columns, drop what you
don't need. Starting from the pair and cutting is consistently faster
than building up from nothing, and it is where the non-obvious parts
already live (empty results, numpy casts, relative urls, stable ids).

Plain DOM is the only frontend shape documented here. There is no
component-library reference right now — the tool description names what
libraries you actually have, and everything else is unavailable rather
than merely unmentioned.

## A handler, whole

```python
# /workspace/app/api/summary.py  ->  serves GET /api/summary
import pandas as pd

def get(req):
    df = pd.read_parquet("/workspace/app/data/records.parquet")
    region = req.params.get("region") or ""      # optional filter
    if region:
        df = df[df["region"] == region]
    if df.empty:                                  # NOT an error
        return {"total": 0, "chart": {"x": [], "y": []}}
    by_year = df.groupby("year")["value"].sum().sort_index()
    return {
        "total": int(len(df)),                    # int(): numpy won't
        "chart": {"x": [int(v) for v in by_year.index],
                  "y": [float(v) for v in by_year.values]},
    }
```

- The route is the filename WITHOUT the `.py`: `app/api/summary.py`
  serves `/api/summary`, and the frontend fetches `api/summary`. Never
  put `.py` in a url. Its verb functions are the methods, and a second
  endpoint is a SECOND FILE, not another branch inside this one.
- `Request`, `Response`, `HttpError` are already in scope — no import.
  `raise HttpError(400, "why")` for a bad request; return a dict/list
  for JSON, a str for text, bytes for a blob, None for 204.
- `req.params` is `dict[str, str]` — use `.get()` for OPTIONAL filters.
  For a REQUIRED one, `req.require("n", int)` coerces and raises a
  clean 400 when it is missing or unparseable.
- Return CHART-READY data. Aggregate server-side into parallel arrays
  the frontend can hand straight to plotly; don't ship raw rows and
  reshape them in JS.

## Architecture that works

- Convert big source data ONCE (run_python -> parquet under
  /workspace/app/data/), then handlers read the parquet. Never re-parse a big
  CSV per request. Create the directory first — `to_parquet` will not
  make it, and fails with "Cannot save file into a non-existent
  directory".
- The handler source is RE-EXECUTED on every request, so module-level
  state does NOT persist between requests: a `_DF = None` lazy cache
  reloads every time. Keep the per-request read cheap (parquet, and
  pass `columns=[...]` for just what you use) rather than assuming it
  happens once.
- For something genuinely too expensive to redo per request, precompute
  it into `cache` from run_python — handlers can READ `cache` and it
  persists. A GET cannot WRITE it (read-only; writing 500s).
- Shared backend code goes in /helpers/<mod>.py, imported QUALIFIED:
  `from helpers.mymod import fn`. Imports resolve from the workspace
  root — a bare `import mymod` will not find it.
- Handlers are VERB functions only: get/post/put/delete/patch. A
  `def query(req)` or `def search(req)` is NEVER called by requests —
  read filters from req.params inside a verb instead. (Dispatch notes
  stray non-verb functions in /workspace/app/logs/api.log.)

## Data gotchas (they 500 in production, not in your head)

- NaN in object columns: `sorted(df[col].unique())` dies comparing
  float NaN with str. Use `sorted(df[col].dropna().unique())`.
- Numpy types don't JSON-serialize: wrap with int()/float() or use
  `df.to_dict(orient="records")` after `.astype(object)` care.
- NaN is not JSON either, and a response carrying one is REFUSED (500,
  naming the path) — because a bare NaN would make the browser reject
  the entire body and blank the page. An aggregate over an empty or
  all-null selection is the usual source: `mean()` of nothing is NaN.
  Send None instead: `float(x) if pd.notna(x) else None`. This bites a
  NON-empty selection too — rows exist, the aggregated column is all
  null — so an `if df.empty` guard alone does NOT cover it. Then render
  the null frontend-side as a dash; `null.toLocaleString()` throws and
  takes the whole render down with it.
- Error responses are JSON: `{"error": ...}` — your frontend's
  res.json() will parse them; check `res.ok` and show `data.error`.
- A filter combination matching NO rows is a normal outcome, not an
  error. Return the same shape with zeros/empty arrays so the page can
  render an empty state; erroring here is how a valid selection kills
  the UI. Compute options from the UNFILTERED frame too, or the
  dropdowns collapse as the user narrows.

## Frontend

Plain HTML + DOM + fetch is the most reliable pattern — copy
references/chart-app.html: dropdowns, a relative fetch, error and empty
states, and `Plotly.react` to redraw in place (cheaper than newPlot per
change, and it leaves no stale trace when the result is empty).

Its two <script> tags load from `vendor/` — plotly and tailwind, served
with your app from its own origin. That is why they work with no
network. Don't rewrite them as CDN urls: those hosts may not resolve
where this is deployed, and the failure looks like a broken page rather
than a blocked request.

Give every control a stable `id` or `data-key`. test_app drives the page
by selector, and positional guesses (`nth-child`, `:first-of-type`)
break the moment you add a filter.

Build elements that carry DATA as nodes — `new Option(v, v)`, or set
`.value`/`.textContent` — never by interpolating values into
`innerHTML`. One quote in a category name (`North "A"`) truncates the
value attribute, the selection stops round-tripping, and the handler
filters on something the user never picked — which reads as a backend
bug and sends you debugging the wrong half.

## Keep it editable — this is where turns get burned

Writing the whole frontend as one 20KB `file_write` feels fast and then
costs you the rest of the session: every later change is a blind edit on
a file you cannot see, and you end up spending more calls FINDING code
than changing it.

- **Split once it grows.** `index.html` holds the markup and
  `<script src="app.js"></script>`; `app.js` holds the logic. Relative
  src works exactly like relative fetch. A single-purpose `app.js` is
  also small enough to rewrite wholesale when an edit gets hairy —
  which a 900-line index.html never is.
- **Grow in verified steps.** Get one endpoint plus one rendered number
  working, THEN add charts and filters. A big-bang first draft moves all
  the debugging to the point where you have the least idea which part
  is wrong.
- **Find code with `grep`, not Python.** `grep -n 'populateSelect'
  /workspace/app/app.js` is one call. Reading the file into run_python
  and looping over `readlines()` to print line numbers is the same
  answer for several calls and a lot of context.
- **When file_edit fails, retry file_edit.** "old_string not found"
  prints the lines it *did* find near your match — copy those exactly
  (whitespace included) and go again. Falling back to string surgery in
  run_python is slower, and unlike file_edit it will happily match the
  wrong occurrence and tell you it worked.

The libraries you have are served from `vendor/` and listed in the
terminal tool's description — that list is the authority, not this
file. External scripts are limited to an allowlist and may not resolve
at all; test_app names anything it blocked in its [rejected requests]
section.

## Debugging loop

1. `tail /workspace/app/logs/api.log` — handler tracebacks, prints, and
   dispatch notes land there. A 500 from any endpoint means the
   traceback is already in this file: READ IT before changing code.
   Guessing from the frontend is how a one-line fix turns into a
   rewrite.
<!--if:commands-->
2. `curl api/x` in the terminal — instant, no server. `-i` shows
   status+headers, `-w '%{http_code}'` prints the code. This hits the
   dispatcher directly, so it isolates backend from frontend in one
   call.
3. test_app for the frontend: page errors carry file:line for runtime
   errors; parse errors mean bisecting your <script> blocks.
<!--endif-->
<!--if:no-commands-->
2. test_app for everything else — both the frontend AND the endpoints.
   Page errors carry file:line for runtime errors; parse errors mean
   bisecting your <script> blocks. To probe an endpoint on its own, use
   an `eval` action: `await (await fetch('api/x')).text()` — `eval`
   awaits what you return, so return the promise chain rather than
   referencing a `.then(r => ...)` binding from outside it.

   There is no `curl` builtin on this executor. The terminal has the
   REAL curl, which would hit the network instead of your app — a
   `curl api/x` here does NOT test your endpoint.
<!--endif-->

## Verification that means something

- Assert on DATA-BEARING elements: a count that isn't '0', a chart
  container with children — not just static text that renders even
  when every fetch failed.
- Exercise the interactive flow: click a filter, wait, assert the
  result region changed.
- If an assert fails, fix the app, not the assert. A weakened
  assertion (`x !== '0' || x === '0'`) verifies nothing.
- Screenshot at the end; the human sees the preview live either way.
