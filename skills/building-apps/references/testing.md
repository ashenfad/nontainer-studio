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

`call` and `testdb` exist only inside a `ws-pytest` run, so
`from host import call` fails in run_python. To watch a handler answer,
write the test (what a test prints is in the report), or `ws-curl` the
endpoint.
