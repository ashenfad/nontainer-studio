# appassets — browser libraries served to agent-authored apps

These files are **not** part of studio's own frontend (that's `frontend/`,
built into `static/`). They are served *to the apps an agent builds*,
through `AppsConfig.static_assets`, at `vendor/…`.

They are committed for the same reason the frontend build is: a
deployment should need nothing from the network. That is what makes an
air-gapped studio work — an agent on a locally-hosted model with no
internet can still write an app that renders.

| file | source | sha256 |
|---|---|---|
| `plotly.min.js` | `cdn.plot.ly/plotly-3.7.0.min.js` | `8ef4c6ab1369f0019611cbcd2d5b8aafef23e5d19ef58c39d4b4249831fe2180` |
| `tailwind.js` | `cdn.tailwindcss.com/3.4.17` | `176e894661aa9cdc9a5cba6c720044cbbf7b8bd80d1c9a142a7c24b1b6c50d15` |
| `react.min.js` | npm `react` + `react-dom`, bundled | `20ea3072f64662753e2b51521763e69ec0354e3b0f0957b3138f3446a783b6f5` |
| `mui.min.js` | npm `@mui/material@6` + `@mui/x-data-grid@7` + emotion, bundled | `393f0429fb93ffbdd11009286e9e5402769aec92927a70847cd3b89bc98a9683` |
| `icons.min.js` | npm `@mui/icons-material@6`, 66 icons, bundled | `9d090df59d2bae4fc88afe29ed6c701fe1246d4ddf60875781aff991c217ec43` |
| `sucrase.min.js` | npm `sucrase@3`, bundled | `8bbf28da8aedb231f4315800f6b0d7310706ae4f1e6e4cdbca2311bbfb7a2913` |
| `jsx-loader.js` | **ours** — hand-written, not generated | `12463452f042d1b365030dd20da68bc798c5bc17df8fe04baeafee72120b1f40` |
| `mui-utils.js` | **ours** — subpath shim for the icon bundle | `752c227b022b40b2de39d925f56b32f9264dd964a7bd0399e54ca175114c21f9` |
| `theme.css` | **ours** — the shell's palette, app-facing | `46abd6279d31325886178ead73abce4d0c2ce39edbaa46fa6c8828ba0aa8361e` |
| `theme.js` | **ours** — that palette as a MUI theme | `09d0ff29e9a032e84dd4412d141fe8554f89e17248d36afed5bf3ae86bbb8e7d` |

All MIT licensed. ~6.8 MB total, of which plotly is 4.7 MB.

## Regenerating

```sh
./scripts/fetch-appassets.sh     # needs node; then verify the checksums
```

Users never need node — these outputs are committed, like `frontend/`'s
build. The script carries the reasoning for each pin; the four decisions
below are the ones that will look arbitrary later.

**Plotly 3.7.0, not 4.x.** 4.0.0 removed the `scattermapbox` /
`choroplethmapbox` trace names. Those are what a model writes from
training data, so an agent would produce valid-looking code that fails
only here, with no hint that the name is the problem.

**The full plotly build.** The apps notes point agents at
`scattergeo`/`choropleth` for maps, and only the full build carries geo
traces. A partial bundle would save ~3.6 MB and buy a cliff the agent
cannot see coming.

**Sucrase, not `@babel/standalone`, for the JSX transform.** 201 KB
against 3.1 MB, and it preserves source positions natively rather than
needing babel's `retainLines` — so a stack trace points at the line the
agent actually wrote, which `test_app` then quotes back. The trade: it
parses loosely, so a mismatched closing tag (`<h1>x</h2>`) compiles where
babel would reject it. Genuine syntax errors still fail with a
file:line:col.

**React and react-dom in ONE module.** react-dom is CommonJS and
`require("react")`s internally; with react marked external, esbuild's ESM
output answers that with a shim that throws *"Dynamic require of 'react'
is not supported"*. Bundling them together resolves the require and
guarantees a single React instance — two copies break hooks with an error
that reads like the agent's fault.

**The React export list is generated, not written.** `export * from` a
CommonJS module produces no named bindings at all, so the names have to
come from somewhere — the first build succeeded, exported nothing, and
the page died with *"does not provide an export named 'jsx'"*. Writing
them by hand then failed the other way: the list silently omitted React
19's `use`, `useActionState`, `useOptimistic` and `useEffectEvent`, and
an app importing one of those broke at module instantiation while the
function sat on the bundled React object. `gen-react-entry.mjs` reads
the real modules instead, so the surface stays complete across upgrades
(59 exports at React 19.2). A test asserts it.

**The icon set is curated, not the package.** Measured with the build's
own flags: all of `@mui/icons-material` is **4,305 KB** for ~2,100
icons, which would nearly double this directory to ship a vocabulary an
app draws five words from. 66 named re-exports, tree-shaken, come to
**14 KB** — 1.4 KB an icon. The cost is that a name outside the set
fails, so the list is not a private detail: `SKILL.md` prints it for the
agent and a test asserts the two agree by reading this bundle's exports.

Icons must be imported from the BARE package name. The per-file form
(`@mui/icons-material/Delete`) cannot be mapped: import maps resolve a
trailing-slash prefix to an extensionless URL, which nontainer's
`_STATIC_TYPES` serves as `application/octet-stream`, and a browser
refuses that as a module. Worse, its failure — *"Failed to resolve
module specifier … Relative references must start with ./"* — advises
the exact rewrite the notes forbid, so the agent is steered wrong. That
is why the notes name the barrel form explicitly.

**The data grid rides in `mui.min.js`.** It could be its own bundle only
by keeping `@mui/material` external, and it imports ~30 SUBPATHS
(`@mui/material/Button`, `/styles`, ...) whose modules export their
component as *default* — which a single-barrel `mui.min.js` cannot
answer. Bundling it with its own copy of MUI is worse: a second
`@mui/private-theming` is a second React context, so the grid would
quietly ignore the house theme while everything around it honoured it.
Verified safe to combine: the grid's 271 exports collide with
`@mui/material`'s on **zero** names. 542 KB → 898 KB.

**A `require("react")` shim, for the same reason react-dom needed one.**
Something under the data grid is CommonJS and `require`s react; with
react external, esbuild answers that with a shim that throws the moment
a grid renders. Bundling react in is not available — that is a second
React instance. So the build injects a banner supplying a module-scope
`require` that resolves the one name observed and throws by name on
anything else, so a dependency adding a second require fails loudly at
the build rather than silently in someone's app.

**The palette is copied, and a test keeps the copy honest.**
`theme.css` restates `frontend/src/app.css`'s `:root` block under
app-facing names (`--app-primary`, not `--accent`) so the shell can
rename its internals without breaking every app an agent ever wrote. A
copy is the cost of that indirection, and nothing would notice it going
stale — an app with last quarter's accent colour still renders
perfectly — so `test_the_app_palette_still_matches_the_shell` compares
the two files directly. `theme.js` then *reads* those properties rather
than restating them a third time.

**A missing property is omitted, not passed as `undefined`.** That is
the difference between degrading to stock Material and taking the app
down: `createTheme({palette: {primary: undefined}})` does not fall back,
it throws *"Cannot read properties of undefined (reading 'type')"* from
ten frames inside library code and the page renders nothing. `theme.js`
therefore builds its palette by dropping absent entries, and exports
`createHouseTheme(read)` so a test can drive that path through the real
module rather than a copy of it.

**Fonts are stacks, not faces.** The shell loads Fraunces and Public
Sans from Google Fonts, which an air-gapped deployment cannot reach, so
`theme.css` names only families that resolve with no network. Vendoring
the woff2s would close the gap — `_STATIC_TYPES` already serves them.

## After changing anything here

Update `FRONTEND_NOTES` in `nontainer_studio/sessions.py` and
`VENDOR_IMPORTS` in `jsx-loader.js`. The bytes, the sentence that
describes them, and the map that resolves them are one decision: a
library the agent isn't told about may as well not be here, and one it's
told about that doesn't resolve is worse.

The import map lives in the loader rather than in the page on purpose.
In the page it was machinery the agent had to reproduce in every app,
and an app whose html lacked it failed on the first import with an error
about module specifiers — pointing at the wrong thing entirely. The
loader defers to a map the page declares, so an embedder or agent
extending the set still wins.
