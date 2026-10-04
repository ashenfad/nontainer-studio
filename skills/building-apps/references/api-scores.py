"""Reference handler, half two: state the app's USERS mutate, kept in `db`.

Copy to /workspace/app/api/scores.py and it serves GET and POST
/api/scores. Where api-handler.py reads a parquet the agent prepared,
this one owns a table: `db` is the live SQLite store every published
version of the app serves over, so what a user posts here is there for
the next request, the next version, and the next session.

Two facts decide how this is written:

1. The handler source is re-executed on EVERY request, so the CREATE
   TABLE below runs each time; IF NOT EXISTS is what makes that cheap
   and repeatable, and it is also what lets a new version meet whatever
   schema the last one left.

2. A param is text from anyone. It is parsed under a try that answers a
   malformed value with a 400, and clamped before it reaches SQL, where
   a negative LIMIT means unlimited.

`db` is in scope in a handler with no import, like `Request`, `Response`
and `HttpError`. A module under app/api/_*.py that needs it imports it:
`from host import db`.
"""

db.execute("CREATE TABLE IF NOT EXISTS scores (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")


def get(req):
    try:
        limit = int(req.params.get("limit", 10))
    except ValueError:
        raise HttpError(400, "limit must be an integer")
    limit = max(1, min(limit, 100))
    rows = db.query("SELECT name FROM scores ORDER BY id DESC LIMIT ?", (limit,))
    return {"scores": [name for (name,) in rows]}


def post(req):
    # require() reads a field from the JSON body and answers a 400 when
    # it is missing or not the type asked for. The type is str unless
    # you pass one, so a number is req.require("score", int).
    name = req.require("name").strip()
    if not name:
        raise HttpError(400, "name must not be blank")
    db.execute("INSERT INTO scores (name) VALUES (?)", (name,))
    return {"ok": True}
