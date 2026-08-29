// Compile an agent's JSX in the browser, and keep its stack traces honest.
//
// This file is OURS (hand-written, not produced by fetch-appassets.sh).
// It exists so the agent writes one <script> tag instead of reproducing a
// transform incantation from the prompt -- the recipe lives here, where it
// is tested, rather than in a paragraph a model paraphrases.
//
//   <script type="module" src="vendor/jsx-loader.js" data-app="app.jsx"></script>
//
// Three details are load-bearing, each verified against Chromium rather
// than assumed:
//
// 1. INLINE injection, not a Blob. The obvious approach -- compile to a
//    Blob and point a <script src> at it -- works during verification and
//    is REFUSED once published, because the served CSP's script-src has
//    no blob:. A refused script does not throw, so nothing in the page
//    reports it. Inline is covered by 'unsafe-inline' and survives both.
//
// 2. //# sourceURL, so Error.stack names the agent's file instead of an
//    anonymous injected script. (sourceMappingURL would NOT do this --
//    that is consumed by devtools, not by V8's stack.)
//
// 3. sucrase, which preserves source positions natively, so the line in
//    a stack trace is the line in the .jsx the agent wrote. test_app
//    reads that line back and quotes it.
//
// 4. The AUTOMATIC jsx runtime, so an agent writes JSX without importing
//    React -- which is both modern React style and what a model most
//    often produces. The classic pragma would need React in scope, and
//    injecting that import would collide with an agent that wrote its
//    own. The import map in the page resolves react/jsx-runtime.
//
// Errors are reported to the page rather than only the console: a failed
// compile otherwise leaves a blank page with nothing to act on.

const tag = document.currentScript || document.querySelector("script[data-app]");
const entry = (tag && tag.dataset.app) || "app.jsx";

// What `import { Button } from '@mui/material'` resolves to. Carried
// HERE rather than written into the page, because an import map is
// machinery the agent would otherwise have to reproduce correctly in
// every app it writes -- and an app whose html lacks it fails on the
// first import, with an error about specifiers rather than about the
// thing the agent got wrong.
//
// Injecting it works because a map applies to every module resolved
// AFTER it is added, and the compiled app is injected below. This
// loader itself imports only relative paths, so it needs no map.
const VENDOR_IMPORTS = {
  react: "./vendor/react.min.js",
  "react/jsx-runtime": "./vendor/react.min.js",
  "react-dom": "./vendor/react.min.js",
  "react-dom/client": "./vendor/react.min.js",
  "@mui/material": "./vendor/mui.min.js",
  // The data grid rides in the SAME bundle: it needs ~30 @mui/material
  // subpaths whose modules export a default, which a single barrel
  // cannot answer, and giving it its own copy of MUI would give it its
  // own theme context — a grid quietly ignoring the house theme while
  // everything around it honours it.
  "@mui/x-data-grid": "./vendor/mui.min.js",
  // A CURATED icon set (see the skill for the names), not the 4.3 MB
  // package. Its one MUI dependency is answered by the shim below so
  // there stays a single MUI instance.
  "@mui/icons-material": "./vendor/icons.min.js",
  "@mui/material/utils": "./vendor/mui-utils.js",
  // The shell's palette as a ready-made MUI theme. Namespaced rather
  // than bare so it reads as house-supplied at the import site, and so
  // the namespace has room for whatever else the house ships later.
  "house/theme": "./vendor/theme.js",
};

// Defer to a map the page already declares: an agent extending the set
// with its own entry should win, and older engines allow only one.
if (!document.querySelector('script[type="importmap"]')) {
  const map = document.createElement("script");
  map.type = "importmap";
  map.textContent = JSON.stringify({ imports: VENDOR_IMPORTS });
  document.head.appendChild(map);
}

// The shell's palette, so an app inherits the look of the page it is
// embedded in without asking for it. PREPENDED, not appended: a
// stylesheet added at the END of head would beat the app's own rules on
// equal specificity, so styling the page would stop working the moment
// the app matched.
//
// Awaited, because theme.js reads these properties through
// getComputedStyle at module-evaluation time -- an unloaded stylesheet
// resolves every one of them to "" and the theme silently degrades to
// stock Material. Resolve on error too: a missing theme.css should cost
// the palette, not the whole app.
function themeStylesheet() {
  // $= (ends-with), NOT *= (contains). A substring match treats an app's
  // own `custom-theme.css` as the house stylesheet and skips loading
  // this one -- and the failure is silent, because theme.js then reads
  // every --app-* property as "" and degrades to stock MUI. Ends-with
  // still accepts both spellings a page might use ("vendor/theme.css",
  // "./vendor/theme.css") and any prefix the app is served under.
  if (document.querySelector('link[rel="stylesheet"][href$="vendor/theme.css"]')) {
    return Promise.resolve(); // the page linked it itself; parsing awaited it
  }
  return new Promise((resolve) => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "vendor/theme.css";
    link.onload = resolve;
    link.onerror = resolve;
    document.head.prepend(link);
  });
}

function report(message) {
  console.error(message);
  // Surface it where a human (and a screenshot) can see it too.
  const box = document.createElement("pre");
  box.id = "jsx-loader-error";
  box.style.cssText =
    "white-space:pre-wrap;color:#b3261e;background:#fcecea;padding:12px;" +
    "margin:12px;border-radius:8px;font:13px/1.5 ui-monospace,monospace";
  box.textContent = message;
  (document.body || document.documentElement).prepend(box);
}

try {
  await themeStylesheet();
  const { transform } = await import("./sucrase.min.js");
  const response = await fetch(entry);
  if (!response.ok) {
    throw new Error(`could not load ${entry} (HTTP ${response.status})`);
  }
  const { code } = transform(await response.text(), {
    transforms: ["jsx"],
    jsxRuntime: "automatic",
    production: true,
    filePath: entry,
  });
  const script = document.createElement("script");
  script.type = "module";
  script.textContent = `${code}\n//# sourceURL=${entry}`;
  document.head.appendChild(script);
} catch (error) {
  report(`JSX compile failed in ${entry}\n\n${error && error.message}`);
}
