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
  Button, CssBaseline, Dialog, DialogActions, DialogContent, DialogTitle,
  Paper, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  TextField, ThemeProvider, Typography,
} from "@mui/material";

// The house theme: the shell's own palette, already built. Use it rather
// than calling createTheme yourself -- an app that picks its own colours
// looks like a different product from the page embedding it.
import theme from "house/theme";

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
      {/* Paints the theme's background and text colours onto the page
          itself. Without it the app renders dark-on-dark text over the
          browser's default white. */}
      <CssBaseline />

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
