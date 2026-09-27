# Returns and requests: the whole contract

Read this before an endpoint returns more than a few thousand rows, a
file download, or anything that isn't JSON. SKILL.md has the common
case; this has the rest, with examples checked against this version.

## What a handler can return

| return | response |
|---|---|
| `dict` / `list` | JSON 200, encoded by the rules below |
| a table: `DataFrame`, `Series`, pyarrow `Table` | JSON rows, or an Arrow stream when the request asks for one |
| `str` | text |
| `bytes` | `application/octet-stream`; wrap in `Response` to name the type |
| `None` | 204, no body |
| `Response(status=, body=, headers=)` | any of the above with your status and headers |
| `raise HttpError(404, "why")` | `{"error": "why"}` with that status; 4xx/5xx only |

Anything else is a 500. So is `HttpError(302, ...)`: redirects and
other statuses go through `Response(status=...)`.

JSON encoding handles the data stack at any depth, so return values as
they are, with no casting:

| value | JSON |
|---|---|
| numpy scalar / array | number / nested list |
| `datetime`, `date`, `Timestamp`, `datetime64` | ISO 8601 string (`"2024-05-01T12:00:00"`) |
| `timedelta`, `Timedelta` | total seconds (`90.0`) |
| `Decimal` | number |
| NaN, ±inf, `pd.NaT`, `pd.NA` | `null` |
| a table nested anywhere (`{"rows": df, "total": 42}`) | its rows, a list of objects |
| `set`, or any other object | refused: 500 |

A refused value names its path, in the response and in `api.log`:

```
BAD RETURN: $.rows[1].tags: set is not JSON-encodable (its order is unstable; return sorted(...))
```

`tail /workspace/app/logs/api.log` shows the line; fix the value it
names.

## Tables, and Arrow

```python
# app/api/sales.py
import pandas as pd

def get(req):
    df = pd.read_parquet("/workspace/app/data/sales.parquet",
                         columns=["year", "revenue"])
    return df.groupby("year")["revenue"].sum()
```

What the caller gets depends on its `Accept` header:

| request's `Accept` | response |
|---|---|
| `application/vnd.apache.arrow.stream` | Arrow IPC stream, same content type |
| anything else, `*/*`, or none (a plain `fetch`) | JSON rows: `[{"year": 2023, "revenue": 3.5}, ...]` |

Every table response carries `Vary: Accept`. Only a table that is the
WHOLE return can be Arrow; `{"rows": df}` is always JSON.

Use Arrow when a page pulls thousands of rows or more. It is binary and
columnar, so it is smaller and faster to parse than JSON, and its cap is
32 MB where JSON's is 10 MB. For a few hundred rows, JSON is simpler.

### The index rule

The index's NAME decides whether it becomes a column. JSON rows and
Arrow follow the same rule:

| index | becomes |
|---|---|
| named: `groupby("year")`, `set_index("id")` | a column with that name (`year`, `id`) |
| unnamed integer: a default `RangeIndex`, or what `df[df.score > 50]` leaves | dropped |
| unnamed, not integer: dates, strings | a column called `index` |
| `MultiIndex` | one column per level |

```python
df.groupby("year")["revenue"].sum()
# -> [{"year": 2023, "revenue": 3.5}, {"year": 2024, "revenue": 0.0}]

df.groupby("year").agg(total=("revenue", "sum"), n=("revenue", "size"))
# -> [{"year": 2023, "total": 3.0, "n": 2}, ...]

df[df.score > 50]
# -> [{"score": 60}, {"score": 70}]           (no row numbers)

pd.Series([1.0, None], index=pd.to_datetime(["2024-01-01", "2024-01-02"])).to_frame("v")
# -> [{"index": "2024-01-01T00:00:00", "v": 1.0}, {"index": "2024-01-02T00:00:00", "v": null}]
```

To keep an unnamed index under a real name, name it:
`s.rename_axis("day")`.

## The page side

Decode Arrow in `app.jsx`. `apache-arrow` is vendored, and the loader
resolves the bare import:

```jsx
import { tableFromIPC } from 'apache-arrow';

const ARROW = 'application/vnd.apache.arrow.stream';

// Arrow when the handler returned a table; JSON otherwise (a dict, or
// an error). Branch on what came back, not on what you asked for.
async function fetchTable(url) {
  const res = await fetch(url, { headers: { Accept: `${ARROW}, application/json;q=0.9` } });
  if (!res.ok) throw new Error((await res.json()).error);
  if ((res.headers.get('content-type') || '').startsWith(ARROW)) {
    return tableFromIPC(await res.arrayBuffer());
  }
  return res.json(); // JSON rows
}
```

Arrow values are not always plain JS values. An integer column
(`int64`, which is what pandas makes) comes back as **BigInt**:
`2023n`, which Plotly cannot plot and `JSON.stringify` throws on. Read
columns through a helper that converts:

```jsx
const plain = (v) => (typeof v === 'bigint' ? Number(v) : v);

// one column as a JS array: Plotly's x or y
const column = (table, name) => Array.from(table.getChild(name), plain);

// row objects: a DataGrid's rows, or a table body
const rows = (table) =>
  table.toArray().map((r) =>
    Object.fromEntries(Object.entries(r.toJSON()).map(([k, v]) => [k, plain(v)])));
```

- Iterating a column (`Array.from(col)`, `col.get(i)`) gives a
  timestamp as epoch milliseconds, ready for `new Date(ms)`.
  `col.toArray()` does NOT: for timestamps and int64 it returns the raw
  `BigInt64Array`, in the unit the column was stored in.
- Nulls come back as `null`.
- `table.numRows` is the row count; `table.schema.fields.map(f => f.name)`
  lists the columns.

```jsx
const t = await fetchTable('api/sales');
Plotly.react(el, [{ x: column(t, 'year'), y: column(t, 'revenue'), type: 'bar' }], layout);
<DataGrid rows={rows(t)} columns={[{ field: 'year' }, { field: 'revenue' }]}
          getRowId={(r) => r.year} />
```

A plain-DOM page loads the same library as a global:

```html
<script src="vendor/arrow.min.js"></script>
<script>
  fetch('api/sales', { headers: { Accept: 'application/vnd.apache.arrow.stream' } })
    .then((res) => res.arrayBuffer())
    .then((buf) => { const t = Arrow.tableFromIPC(buf); /* ... */ });
</script>
```

Keep the `apache-arrow` import in `app.jsx`, not in `format.js`:
`ws-vitest` does not serve `vendor/`, so a module importing it cannot
load there. Put the pure part (turning arrays into chart traces) in
`format.js` and test it on plain arrays.

## Downloads

Return `Response` with bytes, a content type, and
`content-disposition`:

```python
# app/api/export.py  ->  GET api/export?format=csv|parquet
import io
import pandas as pd

def get(req):
    df = pd.read_parquet("/workspace/app/data/sales.parquet")
    if req.params.get("format") == "parquet":
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        return Response(body=buf.getvalue(), headers={
            "content-type": "application/vnd.apache.parquet",
            "content-disposition": "attachment; filename=sales.parquet",
        })
    return Response(body=df.to_csv(index=False).encode(), headers={
        "content-type": "text/csv; charset=utf-8",
        "content-disposition": "attachment; filename=sales.csv",
    })
```

A PNG goes the same way (`"content-type": "image/png"`), but render it
AHEAD of time from `run_python` into `app/data/`, then return
`open(path, "rb").read()`. matplotlib cannot run inside a GET handler
here: it needs a writable cache directory, and a GET's filesystem is
read-only, so the request 500s.

On the page, a download is a plain link: `<a href="api/export?format=csv">`.
The studio's preview pane is a sandboxed frame and may block the
download; open the app in its own tab to try it.

## Size caps

| body | cap |
|---|---|
| text: JSON, CSV, HTML, anything `text/*` | 10 MB |
| binary: Arrow, Parquet, images, octet-stream | 32 MB |

Over a cap, the request is a 500 whose message says what to do:

```
JSON response is 11.5 MB, over the 10 MB limit for text: aggregate or paginate on the server, or return a table and request Arrow (limit 32 MB)
```

In order of preference:

1. **Aggregate on the server.** A chart needs a point per pixel, not a
   row per record: `groupby`, `resample`, a histogram's bin counts.
2. **Paginate.** `limit` and `offset` params, parsed and clamped:
   ```python
   try:
       limit = int(req.params.get("limit") or 1000)
       offset = int(req.params.get("offset") or 0)
   except ValueError:
       raise HttpError(400, "limit and offset must be integers")
   limit, offset = max(1, min(limit, 5000)), max(0, offset)
   return {"rows": df.iloc[offset : offset + limit], "total": len(df)}
   ```
3. **Return the table and request Arrow**, which has the 32 MB cap and
   is smaller for the same rows.

Don't print a large or binary body with `ws-curl`: write it to a file
with `-o`, then check the size or read the file from `run_python`.

## The request side

- `req.params` is `dict[str, str]`, the query string. A repeated key
  keeps only its LAST value: `?tag=a&tag=b` gives `{"tag": "b"}`. For a
  list, take one comma-separated param:
  `[t for t in req.params.get("tags", "").split(",") if t]`.
- `req.require("n", int)` reads `n` from the JSON body first, then the
  query string, coerces it, and raises a 400 when it is missing or does
  not parse. Types: `str`, `int`, `float`, `bool` (`true/1/false/0`).
- `req.json` is the parsed body when the body is JSON, else `None`.
- `req.body` is the raw bytes, always.
- There is no multipart parsing. To upload a file, `fetch(url, {method:
  'POST', body: file})` sends its bytes as the body; read `req.body`.
  Put the filename in a query param.
- `req.headers` has lowercased names. From a browser (preview,
  published, `test_app`), only these reach a handler: `content-type`,
  `accept`, `authorization`, `user-agent`, and any `x-*`. `ws-curl` and
  `call` pass every header you give them, so a header outside that list
  can work in a test and be missing in the app.

## Testing each

**ws-curl** is the wire:

```sh
ws-curl -i $APP_ORIGIN/api/sales                     # JSON rows, with headers
ws-curl -H 'Accept: application/vnd.apache.arrow.stream' -o t.arrow $APP_ORIGIN/api/sales
ws-curl -i $APP_ORIGIN/api/export                    # check content-disposition
ws-curl -o sales.parquet "$APP_ORIGIN/api/export?format=parquet"
```

Read a saved stream back from `run_python`:
`pa.ipc.open_stream(open('t.arrow', 'rb').read()).read_all()`.

**ws-pytest**: `call` takes `headers=`, and the response has
`.content` (bytes), `.content_type`, `.headers`, `.text`, `.json`:

```python
import pyarrow as pa
from host import call

def test_sales_as_arrow():
    resp = call("sales", headers={"accept": "application/vnd.apache.arrow.stream"})
    assert resp.content_type == "application/vnd.apache.arrow.stream"
    table = pa.ipc.open_stream(resp.content).read_all()
    assert table.column_names == ["year", "revenue"]

def test_sales_as_json():
    assert call("sales").json[0] == {"year": 2023, "revenue": 3.5}

def test_csv_download():
    resp = call("export")
    assert resp.headers["content-disposition"] == "attachment; filename=sales.csv"
    assert resp.text.splitlines()[0] == "year,revenue"
```

**ws-vitest**: `vi.stubFetch` accepts a `Response`, so a stub can hand
back Arrow bytes. `vendor/` is not served there, though, so the test
cannot decode them. Test the functions that take plain arrays, and let
`test_app` cover the decode:

```js
import { barTrace } from '../app/format.js';

it('builds a bar trace from columns', () => {
  expect(barTrace([2023, 2024], [3.5, 0])).toEqual({ x: [2023, 2024], y: [3.5, 0], type: 'bar' });
});
```

**test_app** runs the real page with the real vendored decoder, so
assert on a value that only a decoded response could produce, such as a
total or a row count.
