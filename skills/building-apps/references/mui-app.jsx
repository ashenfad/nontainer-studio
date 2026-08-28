// Reference app: fetch -> table -> dialog, with MUI components.
// Pairs with mui-app.html (copy that too) and api-handler.py.
//
// Write imports exactly as you would in any React project. The bare
// names below resolve because the loader supplies an import map; you do
// not need to do anything for that, and rewriting them as
// 'vendor/mui.min.js' would break them.
import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Button, Dialog, DialogActions, DialogContent, DialogTitle,
  Paper, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  TextField, ThemeProvider, Typography, createTheme,
} from "@mui/material";

// The shell publishes its palette as CSS custom properties; read them so
// the app matches the page it is embedded in instead of guessing.
const shell = getComputedStyle(document.documentElement);
const theme = createTheme({
  palette: {
    mode: shell.getPropertyValue("--app-color-scheme").trim() || "light",
    primary: { main: shell.getPropertyValue("--app-primary").trim() || "#6750a4" },
  },
});

function App() {
  const [rows, setRows] = useState([]);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    // RELATIVE url. 'api/runs', never '/api/runs'.
    fetch("api/runs")
      .then((r) => (r.ok ? r.json() : r.json().then((e) => Promise.reject(e.error))))
      .then((d) => setRows(d.runs))
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <ThemeProvider theme={theme}>
      <Typography variant="h5" id="title" sx={{ m: 2 }}>Runs</Typography>

      {error && <Typography id="error" color="error" sx={{ m: 2 }}>{error}</Typography>}

      {/* An empty result is a normal outcome, not an error state. */}
      {!error && rows.length === 0 && (
        <Typography id="empty" sx={{ m: 2 }}>No runs yet.</Typography>
      )}

      <TableContainer component={Paper} sx={{ m: 2, width: "auto" }}>
        <Table id="rows" size="small">
          <TableHead>
            <TableRow><TableCell>id</TableCell><TableCell>status</TableCell><TableCell /></TableRow>
          </TableHead>
          <TableBody>
            {rows.map((row) => (
              // A STABLE id per row: test_app drives the page by
              // selector, and nth-child breaks the moment you sort.
              <TableRow key={row.id} data-key={row.id}>
                <TableCell>{row.id}</TableCell>
                <TableCell>{row.status}</TableCell>
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
        <DialogTitle>Run {selected?.id}</DialogTitle>
        <DialogContent>
          {/* MUI puts this id on the INPUT itself, not a wrapper: the
              selector is '#note', never '#note input'. */}
          <TextField id="note" label="Note" defaultValue={selected?.status} margin="dense" />
        </DialogContent>
        <DialogActions>
          <Button id="close" onClick={() => setSelected(null)}>Close</Button>
        </DialogActions>
      </Dialog>
    </ThemeProvider>
  );
}

createRoot(document.getElementById("root")).render(<App />);
