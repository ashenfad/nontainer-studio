# Tests, in full

The detail behind the test rules in SKILL.md: where tests live, what
the reference tests are for, and what `ws-pytest` gives a test.

`ws-pytest` when the failing piece is one Python function, `ws-vitest`
when it is a frontend module, `test_app` when it is the page. A failing
assertion names the function; a blank page names nothing.

Tests live in `tests/`, **never under `app/`** — `app/` is what
publishes, so a test file there ships with the app and is fetchable from
it. Python is `tests/test_<name>.py`, JavaScript is
`tests/<name>.test.js`. Run them with `ws-pytest -v` and `ws-vitest
--reporter=verbose`.

**There is no `node` here**, nor `npm` or `npx`. JavaScript runs in the
browser: `ws-vitest` runs a module's tests, and `test_app` loads the
page, where a syntax error in `app.jsx` is reported with its line.

`references/test-summary.py` and `references/format.test.js` are the
working pair for the reference app, and `references/test-scores.py` is
the test for the `db`-backed handler. Copy them with the rest, then make
them pass against YOUR app before you reach for `test_app`, and delete
the ones that test something you removed. A copied test that was never
run is worse than none: it sits in `tests/` looking like coverage.

**A handler that keeps state in `db` is tested against `testdb`.** It is
a second store with the same three methods, empty and in memory, from
`from host import testdb`: call `testdb.reset()` first, then hand it to
`call('scores', ..., db=testdb)`. The live `db` is what every published
version serves over, so a test must never seed rows into it, and the
sandbox refuses `sqlite3` itself (a connection's own SQL reaches the
host filesystem beneath the workspace), so an in-memory database of
your own is not a way out. `testdb` is the store that needs neither.

`ws-pytest --help` is the authority on the Python side; the part worth
knowing before you read it: `from host import call`, and
`call('summary', params={...})` runs a handler the way a request does
and returns a response with `.status`, `.json` (a property, not a
method), `.text`, `.content` (bytes), `.headers` and `.ok`, so a
`raise HttpError(400, ...)` arrives as `.status == 400` rather than as
an exception; `Request`, `Response` and `HttpError` are in scope, so a
helper you call directly and that raises one is tested with
`except HttpError`; and a keyword argument (`db=fake`) substitutes what
the handler reads when a test wants isolation. There are no fixtures and
no conftest — setup is the test's own code, written in the test.

`call` exists only inside a `ws-pytest` run, so `from host import call`
fails in run_python. To watch a handler answer, write the test (what a
test prints is in the report), or `ws-curl` the endpoint. `testdb` is
bound everywhere, run_python included.

**A browser check runs against `testdb` too, when you ask.**
`test_app(actions, bind={"db": "testdb"})` hands the handlers of the
requests that run makes `testdb` where they read `db`, so a page you click
through writes into the test store and the live one is left alone; the
report says so on its second line. `ws-curl --bind db=testdb ...` does the
same for one request, which is how to try a write (a POST, a DELETE)
without leaving a row behind. Without the binding, both reach the live
`db`. The store holds whatever the last test left, so reset it first,
from run_python. For a check that should see realistic data, copy the
live store into it: `testdb.reset(copy=True)` gives every table and row
the live `db` has right now, and nothing written to the copy reaches the
live one. For a check that needs particular rows, reset and seed:

```python
testdb.reset()
testdb.execute("CREATE TABLE IF NOT EXISTS scores (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
testdb.execute("INSERT INTO scores (name) VALUES (?)", ("ann",))
```

A handler creates its own tables on its first request, so seeding is
only for the rows a check needs to see. A migration is tried the same
way: `testdb.reset(copy=True)`, then the handler against the copy.

**Never undo your test writes on the live `db`.** A `DELETE` or `UPDATE`
to put the live store back is a guess at what it held before, and a
wrong guess loses someone's data. Bind instead, and there is nothing to
undo. `db.reset()` is refused: it would empty the live store.
