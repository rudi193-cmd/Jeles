"""gap_triage — proposing which gaps are the same question.

Network-free and model-free throughout: `similarity` is pure token maths and
the judge, where a test uses one, is a stub. Nothing here touches the store,
because nothing in the module can.
"""

from __future__ import annotations

from jeles.reactions import gap_triage


def _gap(question, count=1, gid=None):
    return {"question": question, "asked_count": count, "_id": gid or question[:8]}


def _judge(verdict):
    def judge(system, history, text):
        return verdict

    return judge


# ── similarity ──────────────────────────────────────────────────────────────


def test_the_same_question_reworded_scores_high():
    s = gap_triage.similarity(
        "What is the accent colour in Tokyo Night?", "Tokyo Night accent colour?"
    )
    assert s > 0.6


def test_two_questions_sharing_only_a_skeleton_score_low():
    """The failure Nestor measured with character difflib: question-shaped text
    shares a skeleton that scores high while saying nothing. Content tokens
    drop the skeleton before anything is compared."""
    s = gap_triage.similarity(
        "did a jeles gap reach willow-mcp's backlog?", "did a kart task reach the sandbox policy?"
    )
    assert s < 0.6


def test_a_short_question_does_not_match_a_long_one_it_sits_inside():
    """The asymmetry guard. Without it, one word matches everything containing
    that word."""
    s = gap_triage.similarity(
        "vaccines?",
        "What is the recommended vaccine schedule for pregnant patients in the second trimester?",
    )
    assert s < 0.5


def test_nothing_in_common_is_zero():
    assert gap_triage.similarity("Grove accent colour", "habeas corpus") == 0.0


def test_an_empty_question_scores_zero_rather_than_raising():
    assert gap_triage.similarity("", "anything") == 0.0


# ── triage ──────────────────────────────────────────────────────────────────


def test_rephrasings_of_one_question_are_proposed_as_one_group():
    out = gap_triage.triage(
        [
            _gap("What is the accent colour in Tokyo Night?", 5),
            _gap("Tokyo Night accent colour?", 3),
            _gap("How do I rotate the production database password?", 2),
        ]
    )
    assert len(out["groups"]) == 1
    assert out["singletons"] == 1
    assert out["groups"][0]["asked_total"] == 8, (
        "demand is the sum once the phrasings stop being counted separately"
    )


def test_the_most_asked_phrasing_represents_the_group():
    out = gap_triage.triage(
        [
            _gap("Tokyo Night accent colour?", 2),
            _gap("What is the accent colour in Tokyo Night?", 9),
        ]
    )
    assert out["groups"][0]["representative"] == "What is the accent colour in Tokyo Night?"


def test_unrelated_gaps_stay_apart():
    out = gap_triage.triage(
        [
            _gap("What is the accent colour in Tokyo Night?"),
            _gap("How do I rotate the production database password?"),
            _gap("Which court decided Marbury?"),
        ]
    )
    assert out["groups"] == [] and out["singletons"] == 3


def test_groups_do_not_chain():
    """A joining B and B joining C must not put A with C. Single-link
    clustering walks a queue of loosely-related questions into one blob."""
    out = gap_triage.triage(
        [
            _gap("rotate the production database password", 3),
            _gap("rotate the production database password now", 2),
            _gap("database password", 1),
        ]
    )
    for group in out["groups"]:
        rep = group["representative"]
        for member in group["members"][1:]:
            assert (
                gap_triage.similarity(rep, member["question"]) >= gap_triage.DEFAULT_MIN_SIMILARITY
            )


def test_it_writes_nothing_and_returns_the_gaps_it_was_given():
    """Propose, do not execute. Merging is destructive — log_gap records what
    it cost the last time a phrasing was overwritten."""
    a = _gap("What is the accent colour in Tokyo Night?", 5)
    b = _gap("Tokyo Night accent colour?", 3)
    before = [dict(a), dict(b)]
    out = gap_triage.triage([a, b])
    assert [a, b] == before, "the input gaps are untouched"
    assert out["groups"][0]["members"][0] is a, "the originals are handed back"


def test_a_gap_with_no_question_is_skipped_not_fatal():
    out = gap_triage.triage([_gap("a real question", 1), {"asked_count": 4}])
    assert out["singletons"] == 1


# ── the judge may veto a pairing, never create one ──────────────────────────


def test_the_judge_can_refuse_a_pairing_the_tokens_proposed():
    out = gap_triage.triage(
        [
            _gap("What is the accent colour in Tokyo Night?", 5),
            _gap("Tokyo Night accent colour?", 3),
        ],
        judge=_judge("DIFFERENT"),
    )
    assert out["groups"] == []
    assert len(out["vetoed"]) == 1
    assert out["vetoed"][0]["score"] >= gap_triage.DEFAULT_MIN_SIMILARITY, (
        "a veto is recorded with the score it overrode, so the bar stays calibratable"
    )


def test_the_judge_cannot_group_questions_the_tokens_kept_apart():
    """Its only power is to veto. A model talked into seeing a resemblance has
    no way to act on it."""
    out = gap_triage.triage(
        [
            _gap("What is the accent colour in Tokyo Night?"),
            _gap("Which court decided Marbury?"),
        ],
        judge=_judge("SAME"),
    )
    assert out["groups"] == [] and out["singletons"] == 2


def test_a_broken_judge_leaves_the_proposal_alone():
    def explode(system, history, text):
        raise RuntimeError("ollama is down")

    out = gap_triage.triage(
        [
            _gap("What is the accent colour in Tokyo Night?", 5),
            _gap("Tokyo Night accent colour?", 3),
        ],
        judge=explode,
    )
    assert len(out["groups"]) == 1, "an outage falls back to the no-judge behaviour"
    assert out["vetoed"] == []


def test_an_unreadable_verdict_does_not_veto():
    out = gap_triage.triage(
        [
            _gap("What is the accent colour in Tokyo Night?", 5),
            _gap("Tokyo Night accent colour?", 3),
        ],
        judge=_judge("hmm, hard to say"),
    )
    assert len(out["groups"]) == 1 and out["vetoed"] == []


def test_the_measured_margin_holds():
    """The numbers recorded on DEFAULT_MIN_SIMILARITY, pinned so they cannot
    quietly stop being true. Five probe gaps differing only by a correlation
    id against seven distinct fleet questions, from willow-mcp's backlog."""
    probe = "[fleet-seams-{}] did a jeles gap reach willow-mcp's backlog?"
    same = gap_triage.similarity(
        probe.format("3428649-1786406488"), probe.format("3418786-1786404648")
    )
    skeleton = gap_triage.similarity(
        "Should there be a policy-audience one-pager distinct from the operator guide?",
        "Where should a test point when a feature is a flag?",
    )
    assert same > 0.8, "one question with a differing id must still be one question"
    assert skeleton < 0.2, "a shared question skeleton must contribute nothing"
    assert skeleton < gap_triage.DEFAULT_MIN_SIMILARITY < same, (
        "the bar must sit inside the measured margin, not at an edge"
    )
