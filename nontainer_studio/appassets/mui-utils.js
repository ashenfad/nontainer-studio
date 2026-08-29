// The `@mui/material/utils` subpath, answered from the one MUI bundle.
//
// This file is OURS (hand-written, not produced by fetch-appassets.sh).
// icons.min.js is built with that subpath external — inlining it drags
// createSvgIcon and its emotion tail into the icon bundle, 90 KB against
// 14 KB, and gives the icons their own copy of MUI's theming internals.
// Keeping it external and pointing the import map here means there is
// exactly one MUI instance, so an icon inside <ThemeProvider> reads the
// same theme as the components around it.
//
// A named re-export, not `export *`: the icon bundle imports exactly one
// name, and stating it keeps this file honest about the contract.
// tests/test_server.py asserts that icons.min.js needs nothing else.

export { createSvgIcon } from "./mui.min.js";
