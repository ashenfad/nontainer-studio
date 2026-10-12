---
name: agent-tasks
description: agent-defined tasks with agex — a typed function a helper agent runs, called from your code or from an app's handler; when to use one, how to keep its input safe, latency patterns, tests
---

# Agent-defined tasks

`agex` (in scope in `run_python`, and `from host import agex` in a
handler) turns a function signature into a call a helper agent answers:

```python
from dataclasses import dataclass
from typing import Literal

@dataclass
class Verdict:
    label: Literal["bug", "feature", "question"]
    confidence: float

@agex.task
def triage(report: str) -> Verdict:
    """Classify the bug report: label is bug, feature or question, and
    confidence how sure you are, 0 to 1."""

v = triage("The export button does nothing on Safari.")   # a Verdict
many = triage.map(reports)                                # up to 4 at once, in order
```

The docstring is the whole job and the body stays empty. Inputs and the
return value are values: str, numbers, lists and dicts of them, your
own dataclasses and enums, tables and arrays, checked against the
annotations. A helper that can't do it raises `agex.TaskFailed`; one
that needs to ask raises `agex.TaskNeedsInput`.

**A field with fixed choices gets a `Literal` (or an enum) type**, not
`str` and a comment: the helper's answer is checked against it, so your
code never sees a value outside the choices.

**Use one for judgment, not for logic.** Classifying, extracting fields
from free text, summarizing, rewriting, scoring: work whose answer is
language. Anything code can compute, compute.

**A helper knows only what the call gives it.** It starts on an empty
world of its own (none of your files, and nothing it writes comes back),
with the docstring, the inputs and your types. Put everything it needs
in those; a docstring that says "as discussed" leaves it guessing.

## Untrusted input: `objects=()`

A helper holds your host objects (`db`, and the rest) unless you say
otherwise. When its input comes from someone you don't control (an app
visitor's text, a fetched page), give it none, so text written to steer
it has nothing to steer:

```python
@agex.task(objects=())
def summarize(text: str) -> str:
    """A one-sentence summary of the text."""
```

`objects=("db",)` gives it just the ones it needs.

## In an app: it is slow, so design for that

A call takes seconds, sometimes a minute, and its request waits that
long. So:

- **Define the task at a handler module's top level, and call it from a
  POST** that a person's action sends (a button, a form). Never from a
  GET or on page load: the page would hang, and a reload would pay again.
  Give it a leading underscore (`def _summarize`), as any helper in a
  handler module has, or api.log notes it as a function that isn't an
  HTTP verb.
- **Show that it is working.** Disable the button and show a pending
  state until the response lands; say so if it fails.
- **Keep what you got.** Store results in a `db` table keyed by the
  input (a hash of it), and answer a repeat from there before calling
  the task. Not `cache`: a published app can't write it. Let a failed
  write raise; a `try`/`except: pass` around it hides that it never
  stored anything.
- **Batch with `.map`**, not a loop of calls: it runs them at once.
- In the preview, requests are served one at a time, so other requests
  wait while a task runs. A published app does not have that limit.

## Tests: `agex_stand_in`

A test shouldn't spend model calls. `agex_stand_in` answers every task
call at once with the simplest value of its return type (`""`, `0`, `[]`,
the first choice of a `Literal` or enum, a record of such fields), which
is enough to test what the handler does with an answer. If a check of
yours rejects that answer, the check is right and the test is wrong:
type the field (above) or assert the rejection; never loosen the check
to suit the stand-in.

- ws-pytest: `call('triage', 'POST', json={...}, agex=agex_stand_in)`,
  with `from host import call, agex_stand_in`.
- test_app: `test_app(actions, bind={"agex": "agex_stand_in"})`.

Then try the real thing once, by hand or with one test_app run without
the binding, and say in your report that you did.
