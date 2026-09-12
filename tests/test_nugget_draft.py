"""nugget_draft — proposing a nugget from retrieved evidence.

Model-free and network-free: `respond` is a stub everywhere. Nothing here
writes, because nothing in the module can.
"""

from __future__ import annotations

from jeles.reactions import nugget_draft


def _respond(text):
    def respond(system, history, user):
        respond.saw = user
        return text

    respond.saw = ""
    return respond


EVIDENCE = [
    {
        "title": "Grove theme tokens",
        "url": "https://safe-library/grove.json",
        "snippet": "The primary colour is #ffffff.",
    },
    {
        "title": "Grove design notes",
        "url": "https://safe-library/notes",
        "snippet": "White was chosen for contrast.",
    },
]


# ── the model may phrase, never source ──────────────────────────────────────


def test_sources_come_from_the_evidence_not_from_what_the_model_wrote():
    """The property this module exists to guarantee. A model with no tool
    access invented both a fact and its provenance when measured; here it is
    not consulted about citations at all."""
    out = nugget_draft.draft(
        "What is Grove's primary colour?",
        EVIDENCE,
        _respond("It is #ffffff. Source: https://evil.example/made-up"),
    )
    assert out["sources"] == ["https://safe-library/grove.json", "https://safe-library/notes"]
    assert "https://evil.example/made-up" not in out["sources"]


def test_a_draft_cannot_cite_a_document_that_was_not_retrieved():
    out = nugget_draft.draft("q?", [EVIDENCE[0]], _respond("An answer."))
    assert out["sources"] == ["https://safe-library/grove.json"]


def test_the_model_only_sees_the_evidence_it_was_given():
    r = _respond("An answer.")
    nugget_draft.draft("What is Grove's primary colour?", EVIDENCE, r)
    assert "Grove theme tokens" in r.saw and "#ffffff" in r.saw
    assert "What is Grove's primary colour?" in r.saw


# ── it drafts nothing from nothing ──────────────────────────────────────────


def test_no_evidence_refuses_without_calling_the_model():
    def explode(system, history, user):
        raise AssertionError("the model must not be asked to invent one")

    out = nugget_draft.draft("What is Grove's primary colour?", [], explode)
    assert out == {
        "drafted": False,
        "reason": "no_evidence",
        "question": "What is Grove's primary colour?",
    }


def test_evidence_with_no_content_is_not_evidence():
    out = nugget_draft.draft("q?", [{"url": "https://x/1"}], _respond("An answer."))
    assert out["drafted"] is False and out["reason"] == "no_evidence"


def test_an_empty_question_refuses():
    assert nugget_draft.draft("  ", EVIDENCE, _respond("x"))["drafted"] is False


# ── the three refusals stay apart ───────────────────────────────────────────


def test_a_model_that_reads_the_documents_and_declines_is_its_own_reason():
    out = nugget_draft.draft("What is the capital of Mars?", EVIDENCE, _respond("INSUFFICIENT"))
    assert out["drafted"] is False and out["reason"] == "insufficient"


def test_a_decline_with_an_explanation_after_it_still_counts():
    out = nugget_draft.draft(
        "q?", EVIDENCE, _respond("INSUFFICIENT - the documents are about colour.")
    )
    assert out["reason"] == "insufficient"


def test_a_broken_model_is_distinguishable_from_a_declining_one():
    """Retrieve more, retrieve better, and check the model is up are three
    different next steps."""

    def explode(system, history, user):
        raise RuntimeError("ollama is down")

    out = nugget_draft.draft("q?", EVIDENCE, explode)
    assert out["drafted"] is False and out["reason"] == "model_failed"


def test_an_empty_answer_is_a_refusal_not_an_empty_nugget():
    out = nugget_draft.draft("q?", EVIDENCE, _respond("   "))
    assert out["drafted"] is False and out["reason"] == "insufficient"


# ── a draft is the bottom of the ladder by construction ─────────────────────


def test_a_draft_is_always_asserted():
    out = nugget_draft.draft("q?", EVIDENCE, _respond("An answer."))
    assert out["verification_kind"] == "asserted"


def test_the_drafter_cannot_promote_itself_by_naming_a_person():
    """`verified_by` is a claim, not a credential — corpus_put refuses the
    human rung to anything without a valid seal whatever this says."""
    out = nugget_draft.draft("q?", EVIDENCE, _respond("An answer."), drafted_by="a human, honestly")
    assert out["verified_by"] == "a human, honestly"
    assert out["verification_kind"] == "asserted"


def test_the_default_says_plainly_that_a_machine_wrote_it():
    out = nugget_draft.draft("q?", EVIDENCE, _respond("An answer."))
    assert "machine" in out["verified_by"] and "unreviewed" in out["verified_by"]


def test_the_draft_is_shaped_for_put_nugget():
    out = nugget_draft.draft("q?", EVIDENCE, _respond("An answer."))
    for field in ("question", "answer", "sources", "verified_by"):
        assert out.get(field), f"put_nugget requires {field}"
    assert out["evidence_count"] == 2
