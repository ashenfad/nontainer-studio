# The frontend, in full

The detail behind the frontend rules in SKILL.md: theming, what the
bundle holds, and the patterns the reference app already wires. Read it
before your first change to `app.jsx` beyond renaming.

## One stack

There is one frontend stack here — MUI with React and JSX, compiled in
the browser — so there is nothing to choose between. `app.html` is
tiny: a `#root` div and one `<script src="vendor/jsx-loader.js">` tag.
That tag compiles your JSX and resolves the imports, so write bare names
(`react`, `@mui/material`, `house/theme`) exactly as in any React
project — do NOT rewrite them as vendor paths.

Plain HTML and DOM still work, and for a genuinely trivial page they are
fine. But everything here assumes the reference set, and an app built
out of raw DOM will not match the shell unless you theme it by hand from
`vendor/theme.css`.

## Theming: use the house palette, don't pick colours

Apps built here are embedded in a shell that has its own look, so the
palette is supplied rather than chosen:

```jsx
import theme from 'house/theme';       // already built — do not call createTheme
<ThemeProvider theme={theme}><CssBaseline />…</ThemeProvider>
```

`CssBaseline` is what paints the background and text colour onto the
page; without it you get themed components floating on default white.

A plain-DOM page gets the same palette as CSS custom properties:

```html
<link rel="stylesheet" href="vendor/theme.css">
```

then `var(--app-primary)`, `--app-surface`, `--app-text`,
`--app-text-muted`, `--app-border`, `--app-success`, `--app-error`. The
jsx-loader links this for you, so a JSX app needs the tag only if it
also has non-React styling to match.

Reaching for `createTheme` and picking your own colours is the common
mistake: it produces stock Material purple, which is recognisably not
this product.

## What's in the MUI bundle

All 462 components of `@mui/material`, plus:

**`@mui/x-data-grid`** — `import { DataGrid } from '@mui/x-data-grid'`.
Worth reaching for over `<Table>` whenever you'd otherwise hand-write
sorting, filtering or pagination; it does all three from a `columns` and
`rows` prop. Give it `getRowId` when your rows aren't keyed on `id`.

**A curated icon set.** Import from the BARE package name:

```jsx
import { Delete, Search, ExpandMore } from '@mui/icons-material';
```

Not `import DeleteIcon from '@mui/icons-material/Delete'` — that
per-file path does NOT resolve here, and it fails with *"Failed to
resolve module specifier"*, whose advice to use a relative path is
wrong and will send you rewriting a correct import.

Only the 66 names listed in `references/vendor.md` exist — the full
package is 4.3 MB for ~2,100 icons, so it is curated down to what apps
actually use. A name outside that list fails with *"does not provide an
export named …"*, so check the list rather than guessing.

There is no `@mui/lab`. For anything else the bundle lacks, plain HTML
and CSS are always available — don't try to load a package from a CDN.

## Frontend

Copy `references/app.{html,jsx}`. Between them they cover the shapes
that keep coming up: a relative fetch, error and empty states, filters
that stay populated as the user narrows, and `Plotly.react` to redraw in
place (cheaper than newPlot per change, and it leaves no stale trace
when the result is empty).

Everything they load comes from `vendor/`, served with your app from its
own origin. That is why it works with no network. Don't rewrite any of
it as a CDN url: those hosts may not resolve where this is deployed, and
the failure looks like a broken page rather than a blocked request.

Give every control a stable `id` or `data-key`. test_app drives the page
by selector, and positional guesses (`nth-child`, `:first-of-type`)
break the moment you add a filter.

**Dropdowns need `SelectProps={{ native: true }}`.** MUI's default
Select is a div plus a popover, not a `<select>` — so test_app's
`{"select": ...}` action cannot drive it, and neither can `type`. Native
renders a real `<select>` and keeps the page testable. If you do use the
default, drive it with a click on the control followed by a click on the
option.

**A year is not a quantity.** `toLocaleString()` groups digits and caps
at three fractional places, so it turns `2023` into `2,023`, `1.23456`
into `1.235` and `0.00001` into `0` — the page then shows a value nobody
computed, with nothing to say it happened. Split the two: an identifier
(a year, an id, a code) renders as itself, and a measure is grouped and
rounded only where a call site asks. `format.js` is that split —
`formatLabel` and `formatValue(x, { digits })` — and both render null as
a dash.

**State in the URL.** Anything that changes what the page shows — a
filter, a tab, a selected row — lives in the query string: read on
load, written back on change. A reload then keeps the user's place, a
link carries a view to someone else, and a test can open the page at a
state instead of clicking its way there. Two rules keep it from going
wrong: merge into the current params rather than rebuilding them, since
the page is loaded with params that are not yours (the studio's own
`v`), and use `replaceState` for filters but `pushState` for a view
change, so a tweak is not a history entry while a tab switch is one
the back button undoes — which it does only if a `popstate` listener
reads the state back from the URL, since the browser moves the address
and nothing else. Two more facts: inside the studio's preview the page
has an opaque origin and the History API refuses the write, so guard it
with try/catch and let it be a no-op there (the URL is the studio's;
the write lands when the app is opened in its own tab or from its
published link). `format.js` has the two pure halves,
`filtersFromSearch` and `searchWithFilters`, and `app.jsx` wires them,
listener included.

**A worker listens before it awaits.** A module worker that does
top-level work first — `await import(...)`, loading data — can miss the
page's first message: it arrives before `onmessage` is set, and the page
waits forever on a reply that will never come (a progress bar stuck at
0). Set the handler first and do the slow work inside it, or have the
worker `postMessage({ ready: true })` once it is set up and have the
page send nothing until it hears that.

Let JSX render your data — `{row.category}` — rather than assembling
markup as a string. React escapes values, so a category called
`North "A"` renders as itself; the same value interpolated into
`innerHTML` truncates an attribute, the selection stops round-tripping,
and the handler filters on something the user never picked, which reads
as a backend bug and sends you debugging the wrong half.

## Keep it editable — this is where turns get burned

Writing the whole frontend as one 20KB `file_write` feels fast and then
costs you the rest of the session: every later change is a blind edit on
a file you cannot see, and you end up spending more calls FINDING code
than changing it.

- **Split once it grows.** The reference pair already is this split:
  `index.html` is a `#root` div and a script tag, `app.jsx` holds
  everything that changes. That keeps the file you edit small enough to
  rewrite wholesale when an edit gets hairy, which a 900-line
  index.html never is.
- **One .jsx file, though.** Only the entry named by `data-app` gets
  compiled, so `import Chart from './chart.jsx'` does NOT work — the
  browser fetches that file itself and chokes on the raw JSX. Keep your
  components in `app.jsx` and use ordinary functions to organize them.
  The plain-JavaScript parts (fetch helpers, formatting) DO split into
  a `.js` module: the reference pair already does it, with `app.jsx`
  importing `./format.js`, and a `.js` module is also the piece
  `ws-vitest` can ask a question of.
- **Grow in verified steps.** Get one endpoint plus one rendered number
  working, THEN add charts and filters. A big-bang first draft moves all
  the debugging to the point where you have the least idea which part
  is wrong.
