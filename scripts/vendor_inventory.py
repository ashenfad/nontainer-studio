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
    "README.md": "the vendoring record: sources, checksums, and why each pin",
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
    return "ours"


def _exports(text: str) -> list[str]:
    names: set[str] = set()
    for group in re.findall(r"export\{([^}]*)\}", text):
        for part in group.split(","):
            names.add(part.split(" as ")[-1].strip())
    return sorted(names)


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
