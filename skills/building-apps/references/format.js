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

// An IDENTIFIER, as the page should show it: a year, an id, a code, a
// category. Never a number formatter, because that one groups — a year
// 2023 would render as "2,023", which is not a number anyone wrote.
//
// Any field can be null: the handler sends None for anything pandas
// calls missing. So the dash lives here, once, instead of at every call
// site — null.toLocaleString() throws and takes the whole render down.
export function formatLabel(value) {
  if (value === null || value === undefined) return "—";
  return String(value);
}

// A MEASURE, as the page should show it: a count, a total, a mean.
//
// Grouped for reading, and never rounded unless the caller asks. Bare
// toLocaleString() is the trap here: it caps at three fractional digits,
// so 1.23456 renders as "1.235" and 0.00001 renders as "0" — a value
// silently becomes a different value, and nothing anywhere says so.
//
// `digits` is the opt-in: formatValue(x, { digits: 2 }) fixes two
// fractional places, which is a display choice a caller makes for a
// column it knows the scale of.
export function formatValue(value, { digits } = {}) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number") return String(value);
  // NaN and Infinity have no rendering that is not a lie. A handler
  // should not send either, but nothing stops one.
  if (!Number.isFinite(value)) return "—";
  if (digits !== undefined) {
    return value.toLocaleString(undefined, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }
  return groupWholePart(value);
}

// Every digit the number has, with separators in the integer part only.
// The fraction is carried across verbatim, which is what keeps this from
// rounding.
function groupWholePart(value) {
  const text = String(value);
  // Exponential notation ("1e-7") has no integer part to group and no
  // reading that separators improve.
  if (text.includes("e") || text.includes("E")) return text;
  const [whole, fraction] = text.split(".");
  const grouped = Number(whole).toLocaleString();
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
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

// The filters a page was opened with, read off its query string.
//
// State that changes what the page shows lives in the URL: a reload
// keeps the user's place, a link carries a view to someone else, and a
// test can open the page straight at a state instead of clicking to
// it. `keys` names the filters this page has, so a param it does not
// know (the studio's own cache-busting `v`, for one) is left alone
// and a missing one reads as unset.
export function filtersFromSearch(search, keys) {
  const params = new URLSearchParams(search);
  const filters = {};
  for (const key of keys) filters[key] = params.get(key) || "";
  return filters;
}

// The query string to put back after the filters change: the current
// one with these filters written over it, a cleared filter removed,
// and every param that is not a filter kept as it was. Merging rather
// than rebuilding is what keeps a foreign param alive across a change.
// Returns "" when nothing remains, so the caller can write the bare
// pathname.
export function searchWithFilters(search, filters) {
  const params = new URLSearchParams(search);
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value);
    else params.delete(key);
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}
