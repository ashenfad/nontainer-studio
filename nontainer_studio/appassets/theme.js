// The shell's palette as a ready-made MUI theme.
//
// This file is OURS (hand-written, not produced by fetch-appassets.sh).
// It exists so an agent building a component UI writes
//
//     import theme from 'house/theme';
//     <ThemeProvider theme={theme}><CssBaseline />...</ThemeProvider>
//
// instead of hand-rolling createTheme and guessing colours. Every app
// that does this looks like it belongs in the shell embedding it, which
// is the whole point of vendoring a component library rather than just
// permitting one.
//
// Two details are load-bearing:
//
// 1. RELATIVE import of mui.min.js, not the bare '@mui/material'. This
//    module sits next to it, so the relative path always resolves --
//    including on a page that declares its own import map without an
//    '@mui/material' entry. Same reasoning as jsx-loader.js: the loader
//    itself imports only relative paths, so it needs no map.
//
// 2. Values are READ from the CSS custom properties in theme.css rather
//    than written here. One declaration, two consumers: the properties
//    theme a plain-DOM app directly and this module for a component
//    one, so the two cannot drift. jsx-loader.js awaits that stylesheet
//    before it injects the app, so the properties are resolvable by the
//    time this module evaluates.
//
// A missing property is OMITTED, never passed as undefined. This is the
// difference between degrading to stock Material and taking the app
// down: createTheme({palette: {primary: undefined}}) does NOT fall back
// -- it throws "Cannot read properties of undefined (reading 'type')"
// from ten frames inside library code, and the page renders nothing.
// Verified, after an earlier version of this file claimed the opposite.
//
// It matters because the stylesheet is not guaranteed: a page can be
// served from an embedder whose asset dir lacks it, or link something
// else the loader mistakes for it. Missing colours should cost the
// house look, not the app.

import { createTheme } from "./mui.min.js";

const root = getComputedStyle(document.documentElement);

/** Read one custom property, or undefined when it is absent/empty. */
function cssVar(name) {
  return root.getPropertyValue(name).trim() || undefined;
}

/** Copy only the defined entries; undefined if nothing survives. */
function compact(entries) {
  const out = {};
  for (const [key, value] of Object.entries(entries)) {
    if (value !== undefined) out[key] = value;
  }
  return Object.keys(out).length ? out : undefined;
}

/**
 * Build the house theme from a property reader. Exported so the
 * degradation path is testable through this module rather than through
 * a copy of it: `createHouseTheme(() => undefined)` is what an app gets
 * when theme.css never arrived, and it must still render.
 */
export function createHouseTheme(read = cssVar) {
  const swatch = (name) => {
    const value = read(name);
    return value ? { main: value } : undefined;
  };
  return createTheme({
    palette: compact({
      mode: read("--app-color-scheme") === "light" ? "light" : "dark",
      primary: swatch("--app-primary"),
      secondary: swatch("--app-secondary"),
      success: swatch("--app-success"),
      warning: swatch("--app-warning"),
      error: swatch("--app-error"),
      background: compact({
        default: read("--app-background"),
        paper: read("--app-surface"),
      }),
      text: compact({
        primary: read("--app-text"),
        secondary: read("--app-text-muted"),
      }),
      divider: read("--app-border"),
    }),
    typography: compact({
      fontFamily: read("--app-font-body"),
      // Buttons shouting in caps is the single most recognisable "this
      // is stock Material" tell, and the shell does not do it.
      button: { textTransform: "none" },
    }),
    shape: { borderRadius: 8 },
  });
}

export const theme = createHouseTheme();

export default theme;
