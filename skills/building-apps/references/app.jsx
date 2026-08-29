// Reference frontend, half two of two: filters -> fetch -> stats, chart,
// table, dialog. Copy this AND app.html; it pairs with api-handler.py.
//
// This is a WORKING FILE, not an illustration. Copy it, then cut it down
// to your data — rename the columns, delete the parts you don't need.
// Starting here and cutting is consistently faster than building up from
// nothing, and the non-obvious parts are already handled: empty results,
// null aggregates, relative urls, stable selectors, a themed chart.
//
// Write imports exactly as you would in any React project. The bare
// names resolve because the loader supplies an import map; rewriting
// them as 'vendor/mui.min.js' would break them.
import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Alert, Box, Button, Card, CardContent, CssBaseline, Dialog, DialogActions,
  DialogContent, DialogTitle, Paper, Stack, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, TextField, ThemeProvider, Typography,
} from "@mui/material";

// The house theme: this shell's palette, already built. Use it rather
// than calling createTheme yourself — an app that picks its own colours
// looks like a different product from the page embedding it.
import theme from "house/theme";

// Plotly draws on white paper unless told otherwise, so a chart on a
// dark page is a glaring white rectangle — the most visible way an app
// stops matching its shell. Take the colours from the theme, so there is
// still only one place they are decided.
const CHART_THEME = {
  paper_bgcolor: "rgba(0,0,0,0)", // let the Paper behind it show through
  plot_bgcolor: "rgba(0,0,0,0)",
  font: { color: theme.palette.text.primary, family: theme.typography.fontFamily },
  colorway: [theme.palette.primary.main, theme.palette.secondary.main],
  xaxis: { gridcolor: theme.palette.divider },
  yaxis: { gridcolor: theme.palette.divider },
};

function Chart({ x, y }) {
  const box = useRef(null);
  useEffect(() => {
    // Plotly.react redraws in place — cheaper than newPlot on every
    // filter change, and it leaves no stale trace behind when the
    // filtered result is empty.
    Plotly.react(
      box.current,
      [{ x, y, type: "bar" }],
      { ...CHART_THEME, margin: { t: 20, r: 20, b: 40, l: 60 } },
      { displayModeBar: false, responsive: true },
    );
  }, [x, y]);
  return <Box id="chart" ref={box} sx={{ height: 360 }} />;
}

function Stat({ id, label, value }) {
  return (
    <Card sx={{ minWidth: 140 }}>
      <CardContent>
        <Typography variant="body2" color="text.secondary">{label}</Typography>
        <Typography variant="h5" id={id}>{value}</Typography>
      </CardContent>
    </Card>
  );
}

function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [filters, setFilters] = useState({ category: "", region: "" });
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value) params.set(key, value);
    }
    // RELATIVE url, always. The app is served under a path prefix, so a
    // leading slash ('/api/summary') escapes it and 404s. And never put
    // .py in the url — the route is the filename without it.
    fetch("api/summary?" + params)
      .then(async (res) => {
        // Errors come back as JSON too, so read the body either way and
        // surface data.error rather than showing a blank page.
        const body = await res.json();
        if (!res.ok) throw new Error(body.error || res.status);
        return body;
      })
      .then((body) => { setData(body); setError(null); })
      .catch((e) => setError(String(e)));
  }, [filters]);

  if (error) {
    return <Alert severity="error" id="error" sx={{ m: 2 }}>{error}</Alert>;
  }
  if (!data) {
    return <Typography id="loading" sx={{ m: 2 }}>Loading…</Typography>;
  }

  return (
    <Box sx={{ p: 3, maxWidth: 1100, mx: "auto" }}>
      <Typography variant="h5" id="title" sx={{ mb: 2 }}>Explorer</Typography>

      {/* Filters. `SelectProps={{ native: true }}` is not a style choice:
          MUI's default Select is a div plus a popover, which test_app's
          {"select": ...} action cannot drive (it needs a real <select>).
          Native keeps the page testable — and if you do use the default,
          drive it with a click on the control then a click on the
          option, never by typing. */}
      <Stack direction="row" spacing={2} sx={{ mb: 3 }}>
        {["category", "region"].map((key) => (
          <TextField
            key={key}
            id={`f-${key}`}
            select
            SelectProps={{ native: true }}
            label={key}
            size="small"
            value={filters[key]}
            onChange={(e) => setFilters({ ...filters, [key]: e.target.value })}
            sx={{ minWidth: 160 }}
          >
            {/* Options come from the UNFILTERED frame (the handler builds
                them that way), so the dropdowns don't collapse as the
                user narrows. */}
            <option value="">All</option>
            {data.options[key].map((v) => <option key={v} value={v}>{v}</option>)}
          </TextField>
        ))}
        <Button id="reset" onClick={() => setFilters({ category: "", region: "" })}>
          Reset
        </Button>
      </Stack>

      <Stack direction="row" spacing={2} sx={{ mb: 3 }}>
        <Stat id="total" label="Rows" value={data.total.toLocaleString()} />
        {/* mean_value is null when there was nothing to average (no rows,
            or an all-null column). Render the dash — null.toLocaleString()
            throws and takes the whole render down with it. */}
        <Stat
          id="mean"
          label="Mean value"
          value={data.mean_value === null ? "—" : data.mean_value.toLocaleString()}
        />
      </Stack>

      {/* An empty result is a normal outcome, not an error state. */}
      {data.total === 0 && (
        <Alert severity="info" id="empty" sx={{ mb: 3 }}>
          No rows match these filters.
        </Alert>
      )}

      <Paper sx={{ mb: 3, p: 1 }}>
        <Chart x={data.chart.x} y={data.chart.y} />
      </Paper>

      <TableContainer component={Paper}>
        <Table id="rows" size="small">
          <TableHead>
            <TableRow>
              <TableCell>category</TableCell>
              <TableCell>region</TableCell>
              <TableCell>year</TableCell>
              <TableCell align="right">value</TableCell>
              <TableCell />
            </TableRow>
          </TableHead>
          <TableBody>
            {data.rows.map((row) => (
              // A STABLE id per row: test_app drives the page by
              // selector, and nth-child breaks the moment you sort.
              // Values go in as JSX children, never through innerHTML —
              // React escapes them, so a category called `North "A"`
              // renders as itself instead of truncating the markup.
              <TableRow key={row.id} data-key={row.id}>
                <TableCell>{row.category}</TableCell>
                <TableCell>{row.region}</TableCell>
                {/* Any field can be null — the handler sends None for
                    anything pandas calls missing — so render the dash
                    rather than a blank cell. */}
                <TableCell>{row.year ?? "—"}</TableCell>
                <TableCell align="right">{row.value ?? "—"}</TableCell>
                <TableCell>
                  <Button size="small" id={`open-${row.id}`} onClick={() => setSelected(row)}>
                    Details
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

      <Dialog open={selected !== null} onClose={() => setSelected(null)}>
        <DialogTitle>Row {selected?.id}</DialogTitle>
        <DialogContent>
          {/* MUI puts this id on the INPUT itself, not a wrapper: the
              selector is '#note', never '#note input'. */}
          <TextField id="note" label="Note" defaultValue={selected?.category} margin="dense" />
        </DialogContent>
        <DialogActions>
          <Button id="close" onClick={() => setSelected(null)}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

createRoot(document.getElementById("root")).render(
  <ThemeProvider theme={theme}>
    {/* Paints the theme's background and text colours onto the page
        itself. Without it you get themed components floating on the
        browser's default white. */}
    <CssBaseline />
    <App />
  </ThemeProvider>,
);
