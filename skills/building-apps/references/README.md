# <App name>

Copied to `/workspace/README.md`. It describes the project, not the
published tree, so it sits beside `app/` rather than inside it. Keep
every section short; the Decisions section is the one that grows, and
it grows by appending, never by rewriting.

## What it does

Two sentences: who looks at this app and what question it answers.

## Data

Where the data comes from, what shape it is stored in under `app/data/`,
and how it is refreshed (a run_python step the agent reruns, a file the
human drops in, a fetch the handler makes).

## Endpoints

- `GET /api/summary` — filters in, chart-ready aggregates out.

One line per handler. A second endpoint is a second file under
`app/api/`, and a second line here.

## Tests

- `ws-pytest -v` — handler and helper tests under `tests/`.
- `ws-vitest --reporter=verbose` — frontend module tests under `tests/`.

If a tier has nothing to test, say so here and why, so the next reader
does not go looking.

## Decisions

Dated bullets, one per choice that a later change would have to respect,
with the reason. Append; do not rewrite. The line below is the shape,
not a decision this app made: replace it with your first one.

- YYYY-MM-DD — <the choice, in one clause>: <why, in one clause; what
  it would cost to change>.
