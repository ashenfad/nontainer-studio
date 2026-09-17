"""Reference ws-pytest file for api-scores.py, as copied to
/workspace/app/api/scores.py. Copy this one to
/workspace/tests/test_scores.py and run `ws-pytest -v`.

A handler that keeps state in `db` is tested against `testdb`: a second
store with the same three methods, empty and in memory. `testdb.reset()`
first, so no test inherits the last one's rows, then hand it to `call`
as the handler's `db`. The live `db` is what every published version of
the app serves over, so a test must never seed rows into it — and the
sandbox refuses `sqlite3` itself, since a connection's own SQL can reach
the host filesystem beneath the workspace. `testdb` is the store that
needs neither.

Both come from `host`, like `db` does; `HttpError` is in scope without
an import. `call` sends a JSON body with `json=` and reads the response
through `.status` and `.json` (a property, not a method).
"""

from host import call, testdb


def test_a_posted_name_is_the_first_one_back():
    testdb.reset()
    assert call("scores", "POST", json={"name": "ann"}, db=testdb).status == 200
    assert call("scores", "POST", json={"name": "bo"}, db=testdb).status == 200
    resp = call("scores", params={"limit": "1"}, db=testdb)
    assert resp.status == 200
    assert resp.json == {"scores": ["bo"]}


def test_an_empty_store_answers_with_no_scores():
    testdb.reset()
    resp = call("scores", db=testdb)
    assert resp.status == 200
    assert resp.json == {"scores": []}


def test_a_malformed_limit_is_the_callers_error():
    testdb.reset()
    resp = call("scores", params={"limit": "many"}, db=testdb)
    assert resp.status == 400
    assert "limit" in resp.json["error"]


def test_a_blank_name_is_refused_and_nothing_is_stored():
    testdb.reset()
    assert call("scores", "POST", json={"name": "   "}, db=testdb).status == 400
    assert call("scores", db=testdb).json == {"scores": []}
