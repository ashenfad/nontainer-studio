"""The side generators: a transcript rendered for a model, and the
title and description read back off it.

The model is scripted (no key, no tokens) and everything around it is
the real thing — the same agno Agent the studio builds, over the same
transcript projection the shell renders.
"""

from types import SimpleNamespace

import pytest
from agno.models.response import ModelResponse

from nontainer_studio import providers, summaries
from nontainer_studio.dummy import DummyModel


class ScriptedModel(DummyModel):
    """Answers one fixed string whatever it is asked, and keeps what it
    was asked so a test can see the prompt that was built."""

    def __init__(self, reply: str) -> None:
        super().__init__()
        self.reply = reply
        self.asked: list[str] = []

    def _plan(self, messages) -> ModelResponse:  # type: ignore[override]
        self.asked.append(
            "\n".join(str(m.content) for m in messages if m.role == "user")
        )
        return ModelResponse(role="assistant", content=self.reply)


@pytest.fixture
def scripted_model(monkeypatch):
    """Whatever spec a generator asks for, it gets this model."""

    def install(reply: str) -> ScriptedModel:
        model = ScriptedModel(reply)
        monkeypatch.setattr(providers, "build_model", lambda spec=None: model)
        return model

    return install


def _session(*events) -> SimpleNamespace:
    """A stand-in for the session object a generator reads — the
    transcript is all of it that summaries touch."""
    return SimpleNamespace(
        events=[{"seq": i, **e} for i, e in enumerate(events)],
    )


# -- the transcript a generator is shown -------------------------------------


def test_transcript_carries_the_exchange_and_names_the_tools():
    """What the human asked, what the agent said, and one line per tool
    call — the tool's RESULT is the bulk of a transcript and the least
    of what it is about, so it stays out."""
    text = summaries.transcript_text(
        _session(
            {"type": "user", "text": "chart the revenue csv"},
            {"type": "tool_start", "name": "file_write", "args": {"path": "/app.py"}},
            {"type": "tool_end", "name": "file_write", "result": "wrote 4kb"},
            {"type": "text", "delta": "Charted it"},
            {"type": "text", "delta": " for you."},
            {"type": "done"},
        )
    )
    assert text.splitlines() == [
        "user: chart the revenue csv",
        "[tool] file_write {'path': '/app.py'}",
        "assistant: Charted it for you.",
    ]
    assert "wrote 4kb" not in text


def test_a_queued_message_reads_as_the_person_speaking():
    """An `interject` is a message the human queued while the agent
    worked, delivered with a tool result rather than starting a turn.
    It is still them speaking, and what they said names the work."""
    text = summaries.transcript_text(
        _session(
            {"type": "user", "text": "chart the revenue csv"},
            {"type": "tool_start", "name": "run_python", "args": {}},
            {"type": "interject", "id": "n1", "text": "make it a log scale"},
            {"type": "text", "delta": "Charted it."},
            {"type": "done"},
        )
    )
    assert text.splitlines() == [
        "user: chart the revenue csv",
        "[tool] run_python",
        "user: make it a log scale",
        "assistant: Charted it.",
    ]


def test_transcript_reads_through_an_edit():
    """A turn the human unsaid must not name the session: the
    projection is what the shell shows, and it is what a generator
    sees."""
    text = summaries.transcript_text(
        _session(
            {"type": "user", "text": "first ask"},
            {"type": "text", "delta": "first answer"},
            {"type": "user", "text": "second ask"},
            {"type": "text", "delta": "second answer"},
            {"type": "truncate", "to": 2},
        )
    )
    assert "first ask" in text and "second ask" not in text


def test_transcript_keeps_the_tail_and_no_half_lines():
    """Bounded so the prompt stays small, and bounded from the END: the
    recent exchange is what names the work."""
    events = []
    for i in range(400):
        events.append({"type": "user", "text": f"message {i:03d} " + "x" * 40})
        events.append({"type": "text", "delta": f"reply {i:03d}"})
    text = summaries.transcript_text(_session(*events), limit=500)
    assert len(text) <= 500
    assert "message 399" in text and "message 000" not in text
    # the cut lands mid-line; what is left of that line goes with it
    assert all(
        line.startswith(("user:", "assistant:", "[tool]")) for line in text.splitlines()
    )


def test_a_long_tool_argument_is_a_preview():
    """A file write carries the file. It is a hint about what the turn
    did, not the turn's content."""
    text = summaries.transcript_text(
        _session(
            {"type": "tool_start", "name": "file_write", "args": {"c": "y" * 5_000}}
        )
    )
    assert len(text) < 200 and text.startswith("[tool] file_write")


def test_an_empty_transcript_asks_no_model(scripted_model):
    model = scripted_model("never asked")
    assert summaries.generate_title("dummy", "") is None
    assert summaries.generate_description("dummy", "   ") is None
    assert model.asked == []


# -- what comes back ---------------------------------------------------------


def test_generate_title_reads_the_transcript(scripted_model):
    model = scripted_model("Revenue dashboard")
    assert summaries.generate_title("dummy", "user: chart it") == "Revenue dashboard"
    assert "user: chart it" in model.asked[0]


def test_a_generated_title_is_clamped_like_a_stored_one(scripted_model):
    """A model wrote it, so it is untrusted shape — the rail row takes
    one line and 60 characters."""
    scripted_model("  Revenue\ndashboard  ")
    assert summaries.generate_title("dummy", "hi") == "Revenue dashboard"
    scripted_model("z" * 200)
    assert summaries.generate_title("dummy", "hi") == "z" * 60
    scripted_model("   ")
    assert summaries.generate_title("dummy", "hi") is None


def test_generate_description_is_capped(scripted_model):
    scripted_model("It builds a revenue dashboard over the sales db.")
    assert summaries.generate_description("dummy", "hi") == (
        "It builds a revenue dashboard over the sales db."
    )
    scripted_model("word " * 200)
    assert len(summaries.generate_description("dummy", "hi")) == 300
    scripted_model("\n\n")
    assert summaries.generate_description("dummy", "hi") is None


def test_clean_description_takes_only_text():
    assert summaries._clean_description(None) is None
    assert summaries._clean_description(17) is None
    assert summaries._clean_description("one   two\nthree") == "one two three"


def test_the_generators_share_one_runner(scripted_model):
    """One prompt each, one model, one answer: the difference between a
    title and a description is the prompt it was asked for."""
    model = scripted_model("whatever")
    summaries.generate_title("dummy", "transcript")
    summaries.generate_description("dummy", "transcript")
    assert len(model.asked) == 2
    assert summaries.TITLE_PROMPT != summaries.DESCRIPTION_PROMPT


# -- which model runs them ---------------------------------------------------


def test_the_summary_model_defaults_to_the_sessions_own(monkeypatch):
    monkeypatch.delenv("NONTAINER_STUDIO_SUMMARY_MODEL", raising=False)
    assert summaries.summary_spec("openai:gpt-5.6") == "openai:gpt-5.6"
    assert summaries.summary_spec(None) is None

    monkeypatch.setenv("NONTAINER_STUDIO_SUMMARY_MODEL", "dummy")
    assert summaries.summary_spec("openai:gpt-5.6") == "dummy"
    assert summaries.summary_spec(None) == "dummy"

    # a knob set to nothing is a knob nobody set
    monkeypatch.setenv("NONTAINER_STUDIO_SUMMARY_MODEL", "  ")
    assert summaries.summary_spec("openai:gpt-5.6") == "openai:gpt-5.6"
