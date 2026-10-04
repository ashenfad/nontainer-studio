# Handlers, in full

The detail behind the handler rules in SKILL.md. Read it when you write
your first handler, and come back when one 500s.

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
        return {"total": 0, "mean": None, "chart": {"x": [], "y": []}}
    by_year = df.groupby("year")["value"].sum().sort_index()
    return {
        "total": len(df),
        "mean": df["value"].mean(),               # all-null -> NaN -> null
        "chart": {"x": by_year.index.tolist(), "y": by_year.tolist()},
    }
```

- The route is the filename WITHOUT the `.py`: `app/api/summary.py`
  serves `/api/summary`, and the frontend fetches `api/summary`. Never
  put `.py` in a url. Its verb functions are the methods, and a second
  endpoint is a SECOND FILE, not another branch inside this one.
- `Request`, `Response`, `HttpError` are already in scope — no import.
- `req.params` is `dict[str, str]` — use `.get()` for OPTIONAL filters.
  For a REQUIRED one, `req.require("n", int)` coerces and raises a
  clean 400 when it is missing or unparseable.
- Return CHART-READY data: parallel arrays aggregated server-side,
  handed straight to plotly, not raw rows reshaped in JS.

| return | response |
|---|---|
| `dict` / `list` | JSON; numpy values, dates (ISO 8601), NaN (`null`) need no casting |
| a DataFrame / Series | JSON rows, `[{"year": 2023, "value": 1.5}, ...]`; nested in a dict too |
| `str` | text |
| `Response(body=bytes, headers={"content-type": ...})` | a file or image |
| `None` | 204 |
| `raise HttpError(404, "why")` | `{"error": "why"}`; 4xx/5xx only |

A Series in a dict goes out as ROWS and a bare `Index` is refused, so
chart arrays are `.tolist()`. An unencodable value (a `set`) is a 500,
and `api.log`'s `BAD RETURN` line names its path: `$.rows[3].tags`.

**If an endpoint returns more than a few thousand rows, a file
download, or anything that isn't JSON, read `references/returns.md`
first.** It covers Arrow for big tables (and decoding it on the page),
which index becomes a column, downloads, the 10 MB / 32 MB response
caps, and the request side in full.

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
  it into a FILE under /workspace/app/data/ from run_python. `cache`
  works in the live preview — handlers can READ it — but it is not
  published: a version is the `app/` tree and its rows, so a published
  handler finds an empty cache. A GET cannot WRITE `cache` either
  (read-only; writing 500s).
- `db` is the opposite of `cache`: it is not published because it is
  not carried at all. A published app serves over the SAME live db
  this session writes to, and so does every later version of it — a
  row you write in the preview is there for the app's users, theirs
  are there for you, and a new version meets whatever schema the last
  one left. `CREATE TABLE IF NOT EXISTS` and tolerant reads are how a
  handler survives that.
- Everything a published app RUNS from lives under app/. Publishing
  takes that tree and nothing else, so a module anywhere else imports
  fine in the preview and raises ImportError on every request once the
  app is published. /workspace/helpers is for code no handler imports;
  a module in there that a handler already imports has to move under
  app/, since nothing moves it for you.
- Shared backend code goes in app/api/_shared.py (any `_`-prefixed
  name there), imported QUALIFIED from the workspace root:
  `from app.api._shared import fn`. A bare `import _shared` will not
  find it. `_`-prefixed files under app/api/ are importable and
  reachable by nobody — the api/ prefix routes only to a bare handler
  name, and static serving refuses everything under app/api/ — so the
  module is as private as handler source.
- Handlers are VERB functions only: get/post/put/delete/patch. A
  `def query(req)` or `def search(req)` is NEVER called by requests —
  read filters from req.params inside a verb instead. (Dispatch notes
  stray non-verb functions in /workspace/app/logs/api.log.)

## Data gotchas (they 500 in production, not in your head)

- NaN in object columns: `sorted(df[col].unique())` dies comparing
  float NaN with str. Use `sorted(df[col].dropna().unique())`.
- "No data" is `None`, not `0.0`: a mean of nothing is not a mean of
  zero, and a real 0.0 would be indistinguishable from it. A NaN mean
  (rows exist, the column is all null) goes out as null by itself;
  render null frontend-side as a dash, since `null.toLocaleString()`
  throws and takes the render down with it.
- Error responses are JSON: `{"error": ...}` — your frontend's
  res.json() will parse them; check `res.ok` and show `data.error`.
- A filter combination matching NO rows is a normal outcome, not an
  error. Return the same shape with zeros/empty arrays so the page can
  render an empty state; erroring here is how a valid selection kills
  the UI. Compute options from the UNFILTERED frame too, or the
  dropdowns collapse as the user narrows.
