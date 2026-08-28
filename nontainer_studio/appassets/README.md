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
| `mui.min.js` | npm `@mui/material@6` + emotion, bundled | `51e357905679523efe86a0624935ffd55a1b41ac2a3ce9ec02cac4377686ddc9` |
| `sucrase.min.js` | npm `sucrase@3`, bundled | `8bbf28da8aedb231f4315800f6b0d7310706ae4f1e6e4cdbca2311bbfb7a2913` |
| `jsx-loader.js` | **ours** — hand-written, not generated | `1001e88e78f0590a13354f60b718cffdecd46c16fa5c4db97e434998e86bb3e0` |

All MIT licensed. ~6.0 MB total, of which plotly is 4.7 MB.

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
