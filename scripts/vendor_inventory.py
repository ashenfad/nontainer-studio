"""The inventory of what `vendor/` holds, generated from the assets.

An agent building an app cannot list `vendor/`: the files are served
with the app but are not in its filesystem. This writes what it would
have found by listing and grepping — every file with its size and
version, the bare import names and the file each resolves to, the icon
names the icon bundle exports, the theme's CSS custom properties and
what `house/theme` exports — as one reference the skill points at.

Generated, never hand-edited: a test regenerates it and fails when the
committed copy differs, so updating a vendored library without updating
the inventory fails the build.

    uv run python scripts/vendor_inventory.py          # rewrite the file
    uv run python scripts/vendor_inventory.py --check  # exit 1 if stale
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "nontainer_studio" / "appassets"
FETCH = ROOT / "scripts" / "fetch-appassets.sh"
OUT = ROOT / "skills" / "building-apps" / "references" / "vendor.md"

# What each file is, in the words an agent needs; versions come from
# the bundles or the fetch script below, never from here.
DESCRIPTIONS = {
    "react.min.js": "React and react-dom in one module (react, react/jsx-runtime, react-dom, react-dom/client)",
    "mui.min.js": "@mui/material with @mui/x-data-grid and emotion, one MUI instance",
    "icons.min.js": "a curated subset of @mui/icons-material (the names listed below)",
    "mui-utils.js": "the @mui/material/utils subpath the icon bundle imports (createSvgIcon)",
    "plotly.min.js": "plotly.js, the full build: every trace type, geo included",
    "tailwind.js": "the Tailwind play build: utility classes compiled in the browser",
    "sucrase.min.js": "sucrase, the in-browser JSX transform jsx-loader.js uses",
    "jsx-loader.js": "the loader: declares the import map, compiles the file named by data-app",
    "theme.css": "the shell's palette as CSS custom properties (listed below)",
    "theme.js": "that palette as a MUI theme: `import theme from 'house/theme'`",
    "arrow.min.js": "apache-arrow, the UMD build: `<script src>` gives `window.Arrow` (`Arrow.tableFromIPC`)",
    "arrow.mjs": "apache-arrow as a module, for `import { tableFromIPC } from 'apache-arrow'`; loads arrow.min.js",
    "README.md": "the vendoring record: sources, checksums, and why each pin",
    "hyperframes.runtime.js": "the HyperFrames runtime a video composition loads: its clock, clips and seeking (skill: making-videos)",
    "hyperframes-player.js": "the `<hyperframes-player>` element: plays a composition with controls",
    "hyperframes-LICENSE.txt": "the Apache-2.0 license HyperFrames ships under",
    "anime.min.js": "Anime.js v4, the UMD build: `<script src>` gives `window.anime` (`anime.createTimeline`)",
    "fonts.css": "the `@font-face` rules for the fonts in `fonts/` (listed below): link it, then name a family",
}

# What each vendored font family is for; the rest of its row is read
# from fonts.css and the files.
FONT_REGISTERS = {
    "Inter": "neutral sans for text and UI",
    "Public Sans": "the studio shell's own sans",
    "Space Grotesk": "sans with character, for headlines",
    "Fraunces": "display serif, the shell's headings; opsz, SOFT and WONK axes too",
    "Source Serif 4": "text serif",
    "JetBrains Mono": "code and tabular numbers",
    "Archivo": "display sans with a width axis: condensed (62%) to wide (125%), up to black",
}


def _version(name: str, text: str, fetch: str) -> str:
    if name == "plotly.min.js":
        m = re.search(r"plotly\.js v(\d+\.\d+\.\d+)", text)
        return m.group(1) if m else "?"
    if name == "react.min.js":
        m = re.search(r'version:"(\d+\.\d+\.\d+)"', text)
        return m.group(1) if m else "?"
    if name == "tailwind.js":
        m = re.search(r"cdn\.tailwindcss\.com/(\d+\.\d+\.\d+)", fetch)
        return m.group(1) if m else "?"
    if name == "mui.min.js":
        mui = re.search(r'MUI_VERSION="([^"]+)"', fetch)
        grid = re.search(r'DATAGRID_VERSION="([^"]+)"', fetch)
        exact = re.search(r'"(6\.\d+\.\d+)"', text)
        parts = [
            f"@mui/material {exact.group(1) if exact else (mui.group(1) if mui else '?')}"
        ]
        parts.append(f"@mui/x-data-grid {grid.group(1) if grid else '?'}.x")
        return ", ".join(parts)
    if name == "icons.min.js":
        m = re.search(r'MUI_VERSION="([^"]+)"', fetch)
        return f"@mui/icons-material {m.group(1) if m else '?'}.x"
    if name == "sucrase.min.js":
        m = re.search(r'SUCRASE_VERSION="([^"]+)"', fetch)
        return f"{m.group(1) if m else '?'}.x"
    if name in ("arrow.min.js", "arrow.mjs"):
        m = re.search(r'ARROW_VERSION="([^"]+)"', fetch)
        return m.group(1) if m else "?"
    if name.startswith("hyperframes"):
        m = re.search(r'HYPERFRAMES_VERSION="([^"]+)"', fetch)
        return m.group(1) if m else "?"
    if name == "anime.min.js":
        m = re.search(r"@version v(\d+\.\d+\.\d+)", text)
        return m.group(1) if m else "?"
    return "ours"


def _exports(text: str) -> list[str]:
    names: set[str] = set()
    for group in re.findall(r"export\{([^}]*)\}", text):
        for part in group.split(","):
            names.add(part.split(" as ")[-1].strip())
    return sorted(names)


def _fonts() -> list[str]:
    """The families fonts.css declares, one row each, with what the
    files make of them: weights, styles, width, and size on the wire."""
    css = (ASSETS / "fonts.css").read_text()
    families: dict[str, dict] = {}
    for block in re.findall(r"@font-face\s*\{(.*?)\}", css, re.S):
        name = re.search(r'font-family:\s*"([^"]+)"', block).group(1)
        fam = families.setdefault(name, {"styles": [], "size": 0})
        fam["weights"] = re.search(r"font-weight:\s*([\d ]+);", block).group(1).strip()
        stretch = re.search(r"font-stretch:\s*([^;]+);", block)
        if stretch:
            fam["stretch"] = stretch.group(1).strip()
        fam["styles"].append(re.search(r"font-style:\s*(\w+);", block).group(1))
        src = re.search(r'url\("([^"]+)"\)', block).group(1)
        fam["size"] += (ASSETS / src).stat().st_size
    lines = [
        "",
        "## Fonts",
        "",
        '`<link rel="stylesheet" href="vendor/fonts.css">`, then name a family',
        "in `font-family`. Each is one variable font, so any weight in its range",
        "works, not only the hundreds. Latin characters only. System fonts differ",
        "from machine to machine; these look the same everywhere.",
        "",
        "| family | for | weights | styles | size |",
        "|---|---|---|---|---|",
    ]
    for name, fam in families.items():
        weights = fam["weights"].replace(" ", "–")
        if "stretch" in fam:
            weights += f", width {fam['stretch'].replace(' ', '–')}"
        lines.append(
            f"| {name} | {FONT_REGISTERS.get(name, '')} | {weights} | "
            f"{', '.join(fam['styles'])} | {-(-fam['size'] // 1024)} KB |"
        )
    return lines


def render() -> str:
    fetch = FETCH.read_text() if FETCH.exists() else ""
    files = sorted(p for p in ASSETS.iterdir() if p.is_file())
    lines = [
        "# What `vendor/` holds",
        "",
        "Generated from `nontainer_studio/appassets/` by",
        "`scripts/vendor_inventory.py`; a test fails when this file and the",
        "assets disagree, so what is written here is what is served.",
        "",
        "The files are served with your app at `vendor/` from its own origin,",
        "and they are not in your filesystem: `ls vendor/` finds nothing and a",
        "file you write there is not served. This is the listing you would",
        "have made. To see a file's bytes, request it:",
        "`ws-curl $APP_ORIGIN/vendor/theme.js`.",
        "",
        "## Files",
        "",
        "| file | size | version | what it is |",
        "|---|---|---|---|",
    ]
    for p in files:
        text = p.read_text(errors="replace")
        size = p.stat().st_size
        shown = (
            f"{size / 1024 / 1024:.1f} MB"
            if size >= 1024 * 1024
            else f"{-(-size // 1024)} KB"
        )
        lines.append(
            f"| `{p.name}` | {shown} | {_version(p.name, text, fetch)} | "
            f"{DESCRIPTIONS.get(p.name, '')} |"
        )
    loader = (ASSETS / "jsx-loader.js").read_text()
    lines += [
        "",
        "## Import names",
        "",
        "What a bare specifier resolves to. Import these names exactly; a",
        "per-file path (`@mui/icons-material/Delete`) or a vendor path",
        "(`vendor/mui.min.js`) does not resolve.",
        "",
        "| import | file |",
        "|---|---|",
    ]
    for name, target in re.findall(
        r'^\s*"?([\w@/.-]+)"?:\s*"\./vendor/([^"]+)"', loader, re.M
    ):
        lines.append(f"| `{name}` | `{target}` |")
    icons = _exports((ASSETS / "icons.min.js").read_text())
    lines += [
        "",
        f"## Icons ({len(icons)} names)",
        "",
        "`import { Delete, Search } from '@mui/icons-material'`. These exist and",
        "nothing else does; a name outside the set fails with *does not provide",
        "an export named*.",
        "",
        "```",
    ]
    row: list[str] = []
    for icon in icons:
        if sum(len(x) + 1 for x in row) + len(icon) > 70:
            lines.append(" ".join(row))
            row = []
        row.append(icon)
    if row:
        lines.append(" ".join(row))
    lines.append("```")
    lines += _fonts()
    tokens = sorted(
        set(re.findall(r"--app-[a-z-]+", (ASSETS / "theme.css").read_text()))
    )
    theme_js = (ASSETS / "theme.js").read_text()
    exports = re.findall(
        r"^export (?:default (\w+)|(?:const|function) (\w+))", theme_js, re.M
    )
    names = sorted({a or b for a, b in exports})
    lines += [
        "",
        "## Theme",
        "",
        "`theme.css` defines these custom properties on `:root`, readable from",
        'any page that links it (`<link rel="stylesheet" href="vendor/theme.css">`):',
        "",
        "```",
        " ".join(tokens),
        "```",
        "",
        "`house/theme` builds a MUI theme from them; it exports "
        + ", ".join(f"`{n}`" for n in names)
        + ". The default export is the theme to pass to `<ThemeProvider>`.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current != text:
            sys.stderr.write(f"{OUT} is stale; run scripts/vendor_inventory.py\n")
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
