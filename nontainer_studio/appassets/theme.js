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
// A missing property yields `undefined` and MUI falls back to its own
// default. That is deliberate: a hardcoded fallback here would be a
// third copy of the palette, and a wrong-but-confident one is worse
// than stock Material.

import { createTheme } from "./mui.min.js";

const root = getComputedStyle(document.documentElement);

function css(name) {
  const value = root.getPropertyValue(name).trim();
  return value || undefined;
}

function main(name) {
  const value = css(name);
  return value ? { main: value } : undefined;
}

export const theme = createTheme({
  palette: {
    mode: css("--app-color-scheme") === "light" ? "light" : "dark",
    primary: main("--app-primary"),
    secondary: main("--app-secondary"),
    success: main("--app-success"),
    warning: main("--app-warning"),
    error: main("--app-error"),
    background: {
      default: css("--app-background"),
      paper: css("--app-surface"),
    },
    text: {
      primary: css("--app-text"),
      secondary: css("--app-text-muted"),
    },
    divider: css("--app-border"),
  },
  typography: {
    fontFamily: css("--app-font-body"),
    // Buttons shouting in caps is the single most recognisable "this is
    // stock Material" tell, and the shell does not do it.
    button: { textTransform: "none" },
  },
  shape: { borderRadius: 8 },
});

export default theme;
