# What `vendor/` holds

Generated from `nontainer_studio/appassets/` by
`scripts/vendor_inventory.py`; a test fails when this file and the
assets disagree, so what is written here is what is served.

The files are served with your app at `vendor/` from its own origin,
and they are not in your filesystem: `ls vendor/` finds nothing and a
file you write there is not served. This is the listing you would
have made. To see a file's bytes, request it:
`ws-curl $APP_ORIGIN/vendor/theme.js`.

## Files

| file | size | version | what it is |
|---|---|---|---|
| `README.md` | 9 KB | ours | the vendoring record: sources, checksums, and why each pin |
| `icons.min.js` | 15 KB | @mui/icons-material 6.x | a curated subset of @mui/icons-material (the names listed below) |
| `jsx-loader.js` | 7 KB | ours | the loader: declares the import map, compiles the file named by data-app |
| `mui-utils.js` | 1 KB | ours | the @mui/material/utils subpath the icon bundle imports (createSvgIcon) |
| `mui.min.js` | 899 KB | @mui/material 6.5.0, @mui/x-data-grid 7.x | @mui/material with @mui/x-data-grid and emotion, one MUI instance |
| `plotly.min.js` | 4.6 MB | 3.7.0 | plotly.js, the full build: every trace type, geo included |
| `react.min.js` | 192 KB | 19.2.8 | React and react-dom in one module (react, react/jsx-runtime, react-dom, react-dom/client) |
| `sucrase.min.js` | 202 KB | 3.x | sucrase, the in-browser JSX transform jsx-loader.js uses |
| `tailwind.js` | 398 KB | 3.4.17 | the Tailwind play build: utility classes compiled in the browser |
| `theme.css` | 3 KB | ours | the shell's palette as CSS custom properties (listed below) |
| `theme.js` | 4 KB | ours | that palette as a MUI theme: `import theme from 'house/theme'` |

## Import names

What a bare specifier resolves to. Import these names exactly; a
per-file path (`@mui/icons-material/Delete`) or a vendor path
(`vendor/mui.min.js`) does not resolve.

| import | file |
|---|---|
| `react` | `react.min.js` |
| `react/jsx-runtime` | `react.min.js` |
| `react-dom` | `react.min.js` |
| `react-dom/client` | `react.min.js` |
| `@mui/material` | `mui.min.js` |
| `@mui/x-data-grid` | `mui.min.js` |
| `@mui/icons-material` | `icons.min.js` |
| `@mui/material/utils` | `mui-utils.js` |
| `house/theme` | `theme.js` |

## Icons (66 names)

`import { Delete, Search } from '@mui/icons-material'`. These exist and
nothing else does; a name outside the set fails with *does not provide
an export named*.

```
Add ArrowBack ArrowDownward ArrowForward ArrowUpward BarChart
CalendarToday Cancel Check CheckCircle ChevronLeft ChevronRight Clear
Close Code ContentCopy Delete Description Download Edit Error
ExpandLess ExpandMore Favorite FavoriteBorder FilterList Folder
Fullscreen Help Home Info InsertChart InsertDriveFile Link Menu
MoreHoriz MoreVert OpenInNew Pause People Person PieChart PlayArrow
Print Refresh Save Schedule Search Settings Share ShowChart SkipNext
SkipPrevious Sort Star StarBorder Stop TableChart Terminal Timeline
TrendingDown TrendingUp Upload Visibility VisibilityOff Warning
```

## Theme

`theme.css` defines these custom properties on `:root`, readable from
any page that links it (`<link rel="stylesheet" href="vendor/theme.css">`):

```
--app-background --app-border --app-color-scheme --app-error --app-font-body --app-font-mono --app-link --app-primary --app-secondary --app-success --app-surface --app-surface-hover --app-text --app-text-muted --app-warning
```

`house/theme` builds a MUI theme from them; it exports `createHouseTheme`, `theme`. The default export is the theme to pass to `<ThemeProvider>`.
