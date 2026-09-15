"""Generated text ABOUT a session: its title, and the description an
app carries when the human publishes one.

Both are one cached projection of the same thing — the transcript —
and neither is something the agent is asked for. A tool the model may
or may not call costs a turn's attention, arrives when the model feels
like it, and is missing exactly where it is most wanted (the session
nobody named). A side generation is a second, tiny model run over the
transcript the studio already holds: it is the studio's own answer,
produced on the studio's cadence, and its failure costs nothing but
the previous answer standing.

The generator is stateless by construction — no db, no history, no
tools — so nothing it reads or writes touches the session's own agent,
its memory or its workspace.
"""

from __future__ import annotations

import logging
import os

from . import providers

log = logging.getLogger(__name__)

TRANSCRIPT_LIMIT = 6_000
"""How much transcript tail a generator is shown, in characters. The
run is a side cost of a turn the human is waiting on, so the prompt
stays small: the recent exchange is what names the work, and an early
one that has since been superseded would name the wrong thing."""

ARG_PREVIEW = 80  # a tool call's arguments, as a hint of what it did

DESCRIPTION_MAX = 300

TITLE_PROMPT = (
    "You name a work session for the human's session list. Read the "
    "transcript and answer with 3 to 6 words naming the work — "
    '"Revenue dashboard", "Debugging the CSV import". No quotes, no '
    "trailing period, no preamble: the answer is the title and nothing "
    "else."
)

DESCRIPTION_PROMPT = (
    "You describe a work session to another agent deciding whether to "
    "start from it. Read the transcript and answer with one or two "
    "sentences on what this session knows, built, or decided — what "
    "somebody would be inheriting. No preamble: the answer is the "
    "description and nothing else."
)


def summary_spec(session_spec: str | None = None) -> str | None:
    """The model spec the generators run on: ``NONTAINER_STUDIO_SUMMARY_MODEL``
    if set, else the session's own model (and ``None`` means the
    server default, which is what every spec reader here means by it).

    A knob of its own because the two runs are not the same kind of
    work: the session's model is chosen for the building, and naming a
    transcript is a job a small, cheap model does as well.
    """
    return (os.getenv("NONTAINER_STUDIO_SUMMARY_MODEL") or "").strip() or session_spec


def transcript_text(session: object, limit: int = TRANSCRIPT_LIMIT) -> str:
    """The session's conversation as plain text, tail-first-bounded.

    What a reader needs to name the work: what the human asked, what
    the agent said back, and one line per tool call — a name, and a
    hint of its arguments — because forty file writes are a fact about
    the session that nothing in the prose says. Tool RESULTS are left
    out: they are the bulk of a transcript and the least of what it is
    about.

    Read through the transcript projection, so an edit's rewind is
    honoured: a turn the human unsaid must not name the session.
    """
    from .sessions import Registry

    lines: list[str] = []
    assistant: list[str] = []

    def flush() -> None:
        text = "".join(assistant).strip()
        assistant.clear()
        if text:
            lines.append(f"assistant: {text}")

    for _, event in Registry._visible(list(getattr(session, "events", []))):
        kind = event.get("type")
        if kind == "text":
            assistant.append(event.get("delta") or "")
            continue
        flush()
        if kind == "user":
            text = (event.get("text") or "").strip()
            if text:
                lines.append(f"user: {text}")
        elif kind == "tool_start":
            name = event.get("name") or "?"
            args = event.get("args")
            preview = "" if args in (None, "", {}) else str(args)[:ARG_PREVIEW]
            lines.append(f"[tool] {name} {preview}".rstrip())
    flush()

    text = "\n".join(lines)
    if len(text) > limit:
        # The cut lands mid-line; drop what is left of that line rather
        # than open the prompt on half a word.
        text = text[-limit:].split("\n", 1)[-1]
    return text


def generate_title(spec: str | None, transcript: str) -> str | None:
    """A 3-to-6-word name for the work, or ``None`` when the model
    answered with nothing usable. Raises whatever the model call
    raises — a caller decides what a failure costs."""
    from .sessions import _clean_title

    return _clean_title(_generate(spec, TITLE_PROMPT, transcript))


def generate_description(spec: str | None, transcript: str) -> str | None:
    """A sentence or two on what a session holds, for another agent
    weighing whether to start from it. ``None`` when the model
    answered with nothing usable."""
    return _clean_description(_generate(spec, DESCRIPTION_PROMPT, transcript))


def _clean_description(text: object) -> str | None:
    """Free text -> a stored description, or None for "none".

    A model wrote this, so it is untrusted shape: collapse the
    whitespace runs a wrapped answer arrives with, bound the length,
    and treat blank as absent so an empty answer never shadows the
    human's own words with "".
    """
    if not isinstance(text, str):
        return None
    return " ".join(text.split())[:DESCRIPTION_MAX] or None


def _generate(spec: str | None, prompt: str, transcript: str) -> str | None:
    """One stateless model run over a transcript.

    No db, no history, no tools: the run reads the text it is handed
    and answers once. That is also why it is safe to make from a
    worker thread while the session it describes is doing something
    else — it shares nothing with the session's own agent.
    """
    if not transcript.strip():
        return None
    from agno.agent import Agent

    agent = Agent(
        model=providers.build_model(spec),
        instructions=prompt,
        markdown=False,
    )
    content = getattr(agent.run(transcript), "content", None)
    return content if isinstance(content, str) else None
