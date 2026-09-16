"""Reference ws-pytest file for api-handler.py, as copied to
app/api/summary.py. Copy to /workspace/tests/test_summary.py and run
`ws-pytest -v`.

tests/, NEVER under app/: app/ is what publishes, so a test file under
it ships with the app and is fetchable from it.

The hyphen in this file's own name is why a bare `ws-pytest` does not
run it where it sits: collection is `test_*.py` anywhere outside app/,
and a reference that ran itself would seed the parquet below into
whatever app the session is building.

`call` reaches a handler the way a request does — the same envelope, so
`raise HttpError(400, ...)` comes back as `.status == 400` instead of
raising. It comes from `host`, like `db`; `Request`, `Response` and
`HttpError` are in scope without an import. The handler name is a literal
string because the handler is composed into this program before the
test runs, so a computed name cannot be resolved in time. What comes
back has `.status`, `.json`, `.text`, `.headers` and `.ok` — `.json` is
a property, not a method.

`call` runs against whatever the session binds, so a handler reading
`from host import db` reads the real db: seed the rows, call, assert.
Substituting a fake (`call('summary', db=fake)`) is the option when a
test wants isolation, and it is refused for a name the handler never
reads — a fake nothing reads proves nothing.

There are no fixtures and no conftest here: setup is the test's own
code. This handler's setup is the parquet it reads, which `_seed()`
writes. THAT FILE IS THIS APP'S DATA — once the app reads real data,
delete `_seed()` and assert against what is there, or the test run
overwrites the app's parquet every time.
"""

import os

import pandas as pd

from host import call

DATA = "/workspace/app/data/records.parquet"

# Small and known, and deliberately not tidy: one row has a null value,
# which is the case the handler's _cell() exists for.
ROWS = [
    {"category": "a", "region": "north", "year": 2023, "value": 10.0},
    {"category": "a", "region": "south", "year": 2024, "value": 20.0},
    {"category": "b", "region": "north", "year": 2024, "value": 30.0},
    {"category": "b", "region": "south", "year": 2024, "value": None},
]


def _seed():
    """Write the parquet the handler reads. The directory first —
    to_parquet will not create it, and fails with 'Cannot save file
    into a non-existent directory'."""
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    pd.DataFrame(ROWS).to_parquet(DATA)


def test_summary_returns_chart_ready_json():
    _seed()

    resp = call("summary")

    assert resp.status == 200
    body = resp.json
    assert set(body) == {"options", "total", "mean_value", "chart", "rows"}
    # Options come from the UNFILTERED frame, so they stay whole as the
    # user narrows.
    assert body["options"] == {"category": ["a", "b"], "region": ["north", "south"]}
    assert body["total"] == 4
    # Aggregated server-side into parallel arrays the frontend hands
    # straight to plotly — one point per year, not one per row.
    assert body["chart"] == {"x": [2023, 2024], "y": [10.0, 50.0]}
    assert len(body["rows"]) == 4
    # The null survives as null, not as a NaN. A NaN would go out as a
    # 200 with bare `NaN` in the body, res.json() would throw in the
    # browser, and the page would blank with nothing in api.log.
    assert body["rows"][3]["value"] is None


def test_a_filter_that_matches_nothing_is_not_an_error():
    _seed()

    resp = call("summary", params={"category": "a", "region": "east"})

    assert resp.status == 200
    body = resp.json
    # The SAME shape, so the page renders an empty state instead of
    # crashing on a missing key — and mean_value is None rather than
    # 0.0, because "nothing to average" is not "the average is zero".
    assert body["total"] == 0
    assert body["mean_value"] is None
    assert body["chart"] == {"x": [], "y": []}


def test_an_unknown_category_is_a_400():
    """The sad path. `raise HttpError(400, ...)` in the handler arrives
    here as a status, and the message rides in the JSON body the
    frontend's res.json() reads."""
    _seed()

    resp = call("summary", params={"category": "nope"})

    assert resp.status == 400
    assert "nope" in resp.json["error"]
