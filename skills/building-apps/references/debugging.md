# Debugging, in full

The loop SKILL.md points at, and the habits that keep a fix from
costing a rewrite.

## Debugging loop

1. `tail /workspace/app/logs/api.log` — handler tracebacks, prints, and
   dispatch notes land there. A 500 from any endpoint means the
   traceback is already in this file: READ IT before changing code.
   Guessing from the frontend is how a one-line fix turns into a
   rewrite.
2. `ws-curl $APP_ORIGIN/api/x` in the terminal — instant, no server.
   `-i` shows status+headers, `-f` fails the call on a 4xx/5xx instead
   of printing the error body as if it were a response. This hits the
   dispatcher directly, so it isolates backend from frontend in one
   call. The verb is `ws-curl`, never plain `curl`: real curl may be on
   the PATH, and it would reach the NETWORK instead of your app.
3. `ws-pytest` / `ws-vitest` for one function or module (`references/testing.md`).
4. test_app for the page: errors carry file:line for runtime errors;
   parse errors mean bisecting your <script> blocks.

test_app names any script it blocked in its [rejected requests]
section.

## Editing and finding code

- **Find code with `grep`, not Python.** `grep -n 'populateSelect'
  /workspace/app/app.js` is one call. Reading the file into run_python
  and looping over `readlines()` to print line numbers is the same
  answer for several calls and a lot of context.
<!--if:commands-->
- **The terminal is not bash.** It is termish, a shell written in
  Python over the workspace. Pipes, redirects, heredocs, `&&` and `||`
  work. Loops and `$(…)` are refused with an error that says so.
  `{ …; }` groups and `( … )` subshells fail confusingly: the braces run
  as commands that do not exist, and the commands between them still
  run. To build a file from pieces, write it with file_write, or append
  with `>>` one command at a time.
<!--endif-->
- **When file_edit fails, retry file_edit.** "old_string not found"
  prints the lines it *did* find near your match — copy those exactly
  (whitespace included) and go again. Falling back to string surgery in
  run_python is slower, and unlike file_edit it will happily match the
  wrong occurrence and tell you it worked.
