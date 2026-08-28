#!/usr/bin/env bash
# Fetch the vendored browser libraries into nontainer_studio/appassets/.
#
# These are SERVED to agent-authored apps (AppsConfig.static_assets), not
# imported by studio itself. They are committed so a deployment needs no
# CDN — which is the whole point: an agent on a local model with no
# internet can still build a working chart.
#
# Run this only to add or upgrade a library. Verify the checksums, commit
# the result, and update `frontend_notes` in nontainer_studio/sessions.py
# so the agent is told what it actually has.
set -euo pipefail

cd "$(dirname "$0")/.."
DEST="nontainer_studio/appassets"
mkdir -p "$DEST"

# Pinned by exact version, never "latest": these bytes ship, so the
# version an agent gets should change only when someone chooses it.
#
# Plotly 3.7.0, not 4.x. 4.0.0 removed the `scattermapbox` /
# `choroplethmapbox` trace names, which is what a model writes from
# training data — an agent would produce valid-looking code that fails
# only here. 3.x keeps those names alongside the new `map` ones.
#
# The FULL build, not plotly-basic or plotly-cartesian: the apps notes
# point agents at scattergeo/choropleth for maps, and only the full
# build carries geo traces. A partial bundle trades 3.6MB for a cliff an
# agent cannot see coming.
fetch() {
  local url="$1" out="$2"
  echo "fetching $url"
  curl -sSLf --max-time 300 -o "$DEST/$out" "$url"
  printf '  %s  %s\n' "$(shasum -a 256 "$DEST/$out" | cut -d' ' -f1)" "$out"
}

fetch "https://cdn.plot.ly/plotly-3.7.0.min.js"  "plotly.min.js"
fetch "https://cdn.tailwindcss.com/3.4.17"        "tailwind.js"

echo
echo "done. Checksums above; expected values are in $DEST/README.md."
