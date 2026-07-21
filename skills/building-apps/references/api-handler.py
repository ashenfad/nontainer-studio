"""Reference handler — filtered aggregates as chart-ready JSON.

Copy to /workspace/app/api/summary.py and it serves GET /api/summary.
The route is the filename WITHOUT the `.py` — the frontend fetches
`api/summary`, never `api/summary.py`. The get/post/put/delete
functions are the verbs, so a SECOND endpoint is a SECOND FILE, not
another branch in here. `Request`, `Response` and `HttpError` are
already in scope; do not import them.

Two facts decide how this is written:

1. The handler source is re-executed on EVERY request. Module-level
   state does NOT survive between requests — a `_DF = None` lazy cache
   reloads every single time. So keep the per-request read cheap:
   parquet with an explicit column list, never a re-parse of the
   original CSV.

2. A GET's `cache` is READ-ONLY (writing from a GET raises, and you get
   a 500). Anything too expensive to redo per request belongs in
   `cache`, written ahead of time from run_python:

       cache['summary_options'] = {...}     # in run_python, once
       opts = cache.get('summary_options')  # here, every request
"""

import pandas as pd

DATA = "/workspace/app/data/records.parquet"

# Only the columns this endpoint uses. A projected parquet read is a
# fraction of the full-frame cost, and it happens on every request.
COLUMNS = ["category", "region", "year", "value"]


def _frame():
    return pd.read_parquet(DATA, columns=COLUMNS)


def get(req):
    df = _frame()

    # Options come from the UNFILTERED frame so the dropdowns don't
    # collapse as the user narrows. dropna() first: sorting a column
    # that mixes NaN with strings raises.
    options = {
        "category": sorted(df["category"].dropna().unique().tolist()),
        "region": sorted(df["region"].dropna().unique().tolist()),
    }

    # OPTIONAL filters: an absent param just means "don't filter on it".
    # req.require(name, type) is the strict form — it coerces and raises
    # HttpError(400) when missing or unparseable, which is what you want
    # for a genuinely required param.
    category = req.params.get("category") or ""
    region = req.params.get("region") or ""
    if category:
        if category not in options["category"]:
            raise HttpError(400, f"unknown category: {category!r}")
        df = df[df["category"] == category]
    if region:
        df = df[df["region"] == region]

    # EMPTY IS NOT AN ERROR. A filter combination matching no rows is a
    # normal outcome — return the same SHAPE with zeros so the frontend
    # renders an empty state instead of crashing on a missing key. (An
    # error here is the classic bug: the UI dies on a valid selection.)
    if df.empty:
        return {
            "options": options,
            "total": 0,
            "mean_value": 0.0,
            "chart": {"x": [], "y": []},
        }

    by_year = df.groupby("year")["value"].sum().sort_index()

    return {
        "options": options,
        # int()/float() are not decoration: numpy scalars don't
        # JSON-serialize, and the handler 500s on the way out.
        "total": int(len(df)),
        "mean_value": round(float(df["value"].mean()), 2),
        # Chart-ready: the frontend should plot what it receives, not
        # reshape it. Parallel x/y arrays drop straight into plotly.
        "chart": {
            "x": [int(y) for y in by_year.index],
            "y": [float(v) for v in by_year.values],
        },
    }
