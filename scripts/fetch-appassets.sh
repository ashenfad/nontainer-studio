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

echo "bundling react + mui and sucrase (node $(node --version))"
cd "$WORK"
npm init -y >/dev/null
npm i --no-audit --no-fund --silent \
  esbuild react react-dom "@mui/material@$MUI_VERSION" \
  @emotion/react @emotion/styled "sucrase@$SUCRASE_VERSION" >/dev/null

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

echo "export * from '@mui/material';"                                 > mui.js
echo "export { transform } from 'sucrase';"                           > sucrase.js

bundle() {  # name, extra esbuild args...
  local name="$1"; shift
  npx esbuild "$name.js" --bundle --format=esm --minify \
    --define:process.env.NODE_ENV='"production"' "$@" \
    --outfile="$OLDPWD/$DEST/$name.min.js" >/dev/null
}
bundle react
# MUI keeps react external so it shares the single instance above; the
# import map resolves its `react` / `react-dom` imports to react.min.js.
bundle mui --external:react --external:react-dom --external:react-dom/client \
           --external:react/jsx-runtime
bundle sucrase
cd "$OLDPWD"

echo
for f in "$DEST"/*.js; do
  printf '  %s  %8s KB  %s\n' \
    "$(shasum -a 256 "$f" | cut -d' ' -f1)" \
    "$(( $(wc -c < "$f") / 1024 ))" "$(basename "$f")"
done
echo
echo "done. Expected checksums are in $DEST/README.md."
echo "jsx-loader.js is OURS — hand-written, not generated here."
