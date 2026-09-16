// Reference module: the plain-JavaScript half of the frontend, split
// out of app.jsx. Copy to /workspace/app/format.js; app.jsx imports it
// as './format.js'.
//
// Only the entry named by `data-app` is compiled, so a second .jsx file
// does not work — but a .js module does, and this is what belongs in
// one: pure functions with no JSX, no React and no fetch in them. That
// is also what makes them the cheap thing to test. `ws-vitest` runs
// tests/format.test.js against this file in about a second, where the
// same question asked through test_app needs the whole page to render.

// A value from the handler, as the page should show it.
//
// Any field can be null: the handler sends None for anything pandas
// calls missing, and an aggregate over nothing is null rather than 0.
// So the dash lives here, once, instead of at every call site —
// null.toLocaleString() throws and takes the whole render down with it.
export function formatValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return value.toLocaleString();
  return String(value);
}

// Filters as a query string, dropping the ones the user has not set.
//
// An empty value means "don't filter on it", so it must not be sent:
// `category=` is a present-but-empty param, and a handler reading
// req.params.get("category") gets "" either way only because it guards
// for it. Sending nothing is the shape that cannot be misread.
export function filterQuery(filters) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value);
  }
  return params.toString();
}
