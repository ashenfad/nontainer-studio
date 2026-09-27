#!/usr/bin/env bash
# Build the vendored browser libraries into nontainer_studio/appassets/.
#
# These are SERVED to agent-authored apps (AppsConfig.static_assets), not
# imported by studio itself. They are committed so a deployment needs no
# CDN — which is the whole point: an agent on a local model with no
# internet can still build a working app.
#
# Needs node only to RUN this script. Users never do: the outputs are
# committed, like frontend/'s build.
#
# Run this only to add or upgrade a library. Verify the checksums, commit
# the result, and update FRONTEND_NOTES in nontainer_studio/sessions.py
# so the agent is told what it actually has.
set -euo pipefail

cd "$(dirname "$0")/.."
DEST="nontainer_studio/appassets"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$DEST"

# --- fetched whole -----------------------------------------------------
#
# Plotly 3.7.0, not 4.x. 4.0.0 removed the `scattermapbox` /
# `choroplethmapbox` trace names, which is what a model writes from
# training data — an agent would produce valid-looking code that fails
# only here. 3.x keeps those names alongside the new `map` ones.
#
# The FULL plotly build, not basic/cartesian: the apps notes point agents
# at scattergeo/choropleth for maps, and only the full build carries geo
# traces. A partial bundle trades 3.6MB for a cliff an agent cannot see.
fetch() {
  echo "fetching $1"
  curl -sSLf --max-time 300 -o "$DEST/$2" "$1"
}

fetch "https://cdn.plot.ly/plotly-3.7.0.min.js" "plotly.min.js"
fetch "https://cdn.tailwindcss.com/3.4.17"      "tailwind.js"

# --- bundled from npm --------------------------------------------------
#
# React + MUI as ONE self-contained ES module. esm.sh's ?bundle does not
# inline peer deps (react, react-dom, emotion stay external), so this has
# to be a real bundler run.
#
# Sucrase, not @babel/standalone, for the JSX transform: 206KB against
# 3.1MB, and it preserves source positions natively rather than needing
# babel's retainLines, so a stack trace points at the agent's own line.
# The trade is that sucrase parses loosely — a mismatched closing tag
# (<h1>x</h2>) compiles where babel would reject it. Real syntax errors
# still fail with a file:line:col.
MUI_VERSION="6"
SUCRASE_VERSION="3"
DATAGRID_VERSION="7"
# apache-arrow is pinned EXACTLY, unlike the majors above: its file is
# shipped byte-for-byte as npm publishes it, so the README's checksum is
# one anyone can check against the registry's tarball.
ARROW_VERSION="21.2.0"

echo "bundling react + mui and sucrase (node $(node --version))"
cd "$WORK"
npm init -y >/dev/null
npm i --no-audit --no-fund --silent \
  esbuild react react-dom "@mui/material@$MUI_VERSION" \
  @emotion/react @emotion/styled "sucrase@$SUCRASE_VERSION" \
  "@mui/icons-material@$MUI_VERSION" \
  "@mui/x-data-grid@$DATAGRID_VERSION" "apache-arrow@$ARROW_VERSION" >/dev/null

# A CURATED icon set, not the package. Measured, minified, react
# external: the whole of @mui/icons-material is 4,305 KB for ~2,100
# icons, which would nearly double this directory to ship a vocabulary
# an app draws five words from. The list below is 90 KB — 1.4 KB an
# icon — and covers what an agent actually reaches for.
#
# The cost of curating is that a name outside the list fails, so the
# list is not a private detail: skills/building-apps/SKILL.md prints it
# for the agent, and a test asserts the two agree by reading the built
# bundle's exports. Adding an icon here means adding it there.
ICONS="Add Delete Edit Save Close Check Clear Search FilterList Sort Refresh Download Upload
MoreVert MoreHoriz Menu Settings Home ArrowBack ArrowForward ArrowUpward ArrowDownward
ExpandMore ExpandLess ChevronLeft ChevronRight Visibility VisibilityOff Info Warning
Error CheckCircle Cancel Help Star StarBorder Favorite FavoriteBorder Share Link
ContentCopy OpenInNew Print PlayArrow Pause Stop SkipNext SkipPrevious Fullscreen
InsertChart BarChart ShowChart TableChart PieChart Timeline TrendingUp TrendingDown
CalendarToday Schedule Person People Folder InsertDriveFile Description Code Terminal"

# ONE react runtime module, not three. react-dom is CommonJS and
# `require("react")`s internally; with react marked external, esbuild's
# ESM output answers that with a shim that throws ("Dynamic require of
# 'react' is not supported"). Bundling them together resolves the
# require internally AND guarantees a single React instance -- two
# copies break hooks with an error that reads like the agent's fault.
#
# The import map (in appassets/jsx-loader.js) points every react-ish
# specifier at this one file, so the agent's TRAINED spelling is
# literally correct: `import { useState } from "react"`, `import {
# createRoot } from "react-dom/client"`, and MUI's own internal imports
# all land here.
#
# The export list is GENERATED, not written by hand. react and react-dom
# ship CommonJS, and `export * from` a CJS module produces no named
# bindings at all -- so the names have to come from somewhere. Hand-
# listing them silently omitted React 19's `use`, `useActionState`,
# `useOptimistic` and `useEffectEvent`, which an app would import
# normally and fail on at module instantiation. Reading the real module
# keeps the surface complete and current across upgrades.
cat > gen-react-entry.mjs <<'JS'
import { writeFileSync } from 'node:fs';

const load = async (spec) => {
  const ns = await import(spec);
  return ns.default ?? ns;
};
const [React, ReactDOM, client, jsxRuntime] = await Promise.all(
  ['react', 'react-dom', 'react-dom/client', 'react/jsx-runtime'].map(load),
);

const seen = new Set(['default']);
// Public API only: React's internals are __-prefixed by convention, and
// re-exporting them would put unstable names in an agent's reach.
const pick = (obj) =>
  Object.keys(obj)
    .filter((k) => /^[A-Za-z_$][\w$]*$/.test(k) && !k.startsWith('__'))
    .filter((k) => !seen.has(k) && seen.add(k));

const block = (names, from) =>
  names.length ? `export const { ${names.join(', ')} } = ${from};\n` : '';

writeFileSync('react.js', [
  "import * as reactNs from 'react';",
  "import * as domNs from 'react-dom';",
  "import * as clientNs from 'react-dom/client';",
  "import * as jsxNs from 'react/jsx-runtime';",
  'const React = reactNs.default ?? reactNs;',
  'const ReactDOM = domNs.default ?? domNs;',
  'const client = clientNs.default ?? clientNs;',
  'const jsxRuntime = jsxNs.default ?? jsxNs;',
  'export default React;',
  // Order matters: first writer wins a name. react before react-dom so
  // a shared name (`version`) means React's.
  block(pick(React), 'React'),
  block(pick(ReactDOM), 'ReactDOM'),
  block(pick(client), 'client'),
  block(pick(jsxRuntime), 'jsxRuntime'),
].join('\n'));

console.error(`  react entry: ${seen.size - 1} named exports`);
JS
node gen-react-entry.mjs

# ONE MUI module, carrying the data grid too. The grid could be its own
# bundle, but only by keeping @mui/material external — and it imports
# ~30 SUBPATHS (@mui/material/Button, /styles, /utils, ...) whose
# modules export their component as DEFAULT, which a single-barrel
# mui.min.js cannot answer. Bundling the grid separately WITH its own
# copy of MUI is worse: a second @mui/private-theming means a second
# React context, so the grid would quietly ignore the house theme while
# everything around it honoured it.
#
# Verified safe: @mui/x-data-grid's 271 exports collide with
# @mui/material's on ZERO names, so `export *` from both is
# unambiguous. 542 KB -> 898 KB.
{ echo "export * from '@mui/material';"
  echo "export * from '@mui/x-data-grid';"; }                         > mui.js
echo "export { transform } from 'sucrase';"                           > sucrase.js

# Named re-exports, so esbuild tree-shakes the rest of the package away
# -- `export * from '@mui/icons-material'` would pull in all 2,100.
: > icons.js
for icon in $ICONS; do
  echo "export { default as ${icon} } from '@mui/icons-material/${icon}';" >> icons.js
done

bundle() {  # name, extra esbuild args...
  local name="$1"; shift
  npx esbuild "$name.js" --bundle --format=esm --minify \
    --define:process.env.NODE_ENV='"production"' "$@" \
    --outfile="$OLDPWD/$DEST/$name.min.js" >/dev/null
}
bundle react
# MUI keeps react external so it shares the single instance above; the
# import map resolves its `react` / `react-dom` imports to react.min.js.
# The CJS trap again, one layer further in. Something under
# @mui/x-data-grid is CommonJS and `require("react")`s; with react
# external, esbuild answers that with a shim that THROWS ("Dynamic
# require of 'react' is not supported") the moment a grid renders. It
# cannot be fixed by bundling react in — that would be a second React
# instance, which breaks hooks.
#
# esbuild's shim delegates to a module-scope `require` if one exists, so
# the banner supplies one. It resolves exactly the one name observed
# (verified: `react` is the ONLY literal call site) and throws by name
# on anything else, so a future dependency adding a second require fails
# loudly here instead of silently at runtime in someone's app.
MUI_BANNER='import * as __ntReact from "react";
var require = (name) => {
  if (name === "react") return __ntReact;
  throw new Error("MUI bundle: unexpected require(" + JSON.stringify(name) + ") — see scripts/fetch-appassets.sh");
};'
bundle mui --external:react --external:react-dom --external:react-dom/client \
           --external:react/jsx-runtime --banner:js="$MUI_BANNER"
bundle sucrase
# Icons keep @mui/material/utils external as well: inlining it drags
# createSvgIcon and its emotion tail in, which is 14 KB of icons against
# 90 KB of bundle. The import map points that specifier at
# appassets/mui-utils.js, a two-line shim re-exporting it from
# mui.min.js — so there stays exactly one MUI instance.
bundle icons --external:react --external:react-dom --external:react/jsx-runtime \
             --external:@mui/material/utils

# apache-arrow: the decoder for a handler's Arrow response (a table
# return, fetched with `Accept: application/vnd.apache.arrow.stream`).
# Apache-2.0, not MIT like the rest.
#
# The UMD build, Arrow.es2015.min.js, copied as it is published -- not
# rebuilt -- so `<script src="vendor/arrow.min.js">` gives window.Arrow
# and the checksum matches npm's. It is self-contained (flatbuffers and
# tslib are inside it) and ~190 KB.
#
# It cannot be imported as a module, though: the UMD wrapper assigns
# `this.Arrow`, and `this` is undefined at a module's top level, so an
# `import` of it throws before anything is defined. `arrow.mjs` is the
# module face jsx-loader.js maps `apache-arrow` to: it loads the UMD
# file as a classic script (once, and not at all when the page already
# has) and re-exports the global. One Arrow on the page either way,
# and one copy of the bytes.
#
# Its export list is READ from the build, the lesson of react.min.js's
# list: names written by hand drift, and a missing one fails at module
# instantiation in someone's app.
cp node_modules/apache-arrow/Arrow.es2015.min.js "$OLDPWD/$DEST/arrow.min.js"
cat > gen-arrow-module.mjs <<'JS'
import { createRequire } from 'node:module';

// By path: the package's `exports` map does not list the UMD file.
const Arrow = createRequire(import.meta.url)(
  './node_modules/apache-arrow/Arrow.es2015.min.js',
);
const names = Object.keys(Arrow)
  .filter((k) => /^[A-Za-z_$][\w$]*$/.test(k) && k !== 'default')
  .sort();

const lines = [
  "// apache-arrow as an ES module: what `import { tableFromIPC } from",
  "// 'apache-arrow'` resolves to. GENERATED by scripts/fetch-appassets.sh",
  "// from arrow.min.js's own exports; do not edit by hand.",
  "//",
  "// arrow.min.js is the upstream UMD build, which cannot be imported: its",
  "// wrapper assigns `this.Arrow`, and `this` is undefined in a module. So",
  "// this loads it as a classic script -- once, and not at all when the",
  "// page already has <script src=\"vendor/arrow.min.js\"> -- and re-exports",
  "// the global it defines.",
  "if (!globalThis.Arrow) {",
  "  await new Promise((resolve, reject) => {",
  "    const script = document.createElement('script');",
  "    script.src = new URL('./arrow.min.js', import.meta.url).href;",
  "    script.onload = resolve;",
  "    script.onerror = () =>",
  "      reject(new Error('apache-arrow: vendor/arrow.min.js did not load'));",
  "    document.head.appendChild(script);",
  "  });",
  "}",
  "const Arrow = globalThis.Arrow;",
  "export default Arrow;",
  "export const {",
];
let row = '';
for (const name of names) {
  if (row && row.length + name.length > 70) {
    lines.push('  ' + row.trimEnd());
    row = '';
  }
  row += name + ', ';
}
if (row) lines.push('  ' + row.trimEnd());
lines.push('} = Arrow;', '');
process.stdout.write(lines.join('\n'));
console.error(`  arrow module: ${names.length} named exports`);
JS
node gen-arrow-module.mjs > "$OLDPWD/$DEST/arrow.mjs"
cd "$OLDPWD"

echo
for f in "$DEST"/*.js "$DEST"/*.mjs; do
  printf '  %s  %8s KB  %s\n' \
    "$(shasum -a 256 "$f" | cut -d' ' -f1)" \
    "$(( $(wc -c < "$f") / 1024 ))" "$(basename "$f")"
done
echo
echo "done. Expected checksums are in $DEST/README.md."
echo "jsx-loader.js is OURS — hand-written, not generated here."
