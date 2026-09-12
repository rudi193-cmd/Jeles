"""Verified-nugget corpus: storage, ranked ask/search, gap logging."""

from __future__ import annotations

import pytest


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    # _conn() keys its connection cache by the full resolved db path, so a
    # fresh WILLOW_STORE_ROOT per test is enough isolation without reload.
    monkeypatch.setenv("WILLOW_STORE_ROOT", str(tmp_path / "store"))
    from jeles import corpus as corpus_module

    return corpus_module


def _seed_grove(corpus):
    return corpus.put_nugget(
        question="What's the primary color in Grove?",
        answer="The primary color in Grove is #ffffff (white).",
        sources=["safe-library/themes/grove.json"],
        verified_by="designer",
        tags=["color", "grove", "primary"],
    )


def test_put_requires_core_fields(corpus):
    assert "error" in corpus.put_nugget("", "", [], "")
    assert "error" in corpus.put_nugget("Q?", "A.", [], "")


def test_put_and_get_roundtrip(corpus):
    result = _seed_grove(corpus)
    assert "id" in result
    nugget = corpus.get_nugget(result["id"])
    assert nugget["question"] == "What's the primary color in Grove?"
    assert nugget["verified_by"] == "designer"
    assert nugget["status"] == "verified"


def test_get_missing_returns_error(corpus):
    assert corpus.get_nugget("does-not-exist") == {"error": "not_found"}


def test_search_ranks_question_match_first(corpus):
    _seed_grove(corpus)
    corpus.put_nugget(
        question="What's the accent color in Nord?",
        answer="The accent color in Nord is #88c0d0 (ice blue).",
        sources=["safe-library/themes/nord.json"],
        verified_by="designer",
    )
    hits = corpus.search_nuggets("primary color Grove")
    assert hits
    assert hits[0]["question"].startswith("What's the primary color")


def test_weak_overlap_is_not_a_confident_ask(corpus):
    # "color" overlaps with the Grove nugget, so search_nuggets() (a loose
    # ranked lookup) may legitimately surface it — but that weak overlap
    # must not be enough for ask_corpus() to call it a confident answer.
    _seed_grove(corpus)
    asked = corpus.ask_corpus("What is the accent color in Tokyo Night?")
    assert asked["found"] is False


def test_ask_corpus_exact_match(corpus):
    _seed_grove(corpus)
    result = corpus.ask_corpus("What's the primary color in Grove?")
    assert result["found"] is True
    assert result["exact"] is True
    assert "white" in result["nugget"]["answer"].lower()


def test_ask_corpus_miss_logs_gap(corpus):
    result = corpus.ask_corpus("What is the accent color in Tokyo Night?")
    assert result["found"] is False
    gaps = corpus.list_gaps()
    assert len(gaps) == 1
    assert gaps[0]["question"] == "What is the accent color in Tokyo Night?"
    assert gaps[0]["asked_count"] == 1


def test_ask_corpus_repeated_miss_bumps_count_not_duplicates(corpus):
    corpus.ask_corpus("What is the accent color in Tokyo Night?")
    corpus.ask_corpus("what is the accent color in tokyo night")
    gaps = corpus.list_gaps()
    assert len(gaps) == 1
    assert gaps[0]["asked_count"] == 2


def test_search_nuggets_never_logs_a_gap(corpus):
    corpus.search_nuggets("some unmatched query")
    assert corpus.list_gaps() == []


def test_to_search_hit_shape(corpus):
    _seed_grove(corpus)
    nugget = corpus.list_nuggets()[0]
    hit = corpus.to_search_hit(nugget, 1)
    assert hit["source_id"] == "corpus"
    assert hit["confidence"] == "verified"
    assert hit["title"] == nugget["question"]
    assert hit["snippet"] == nugget["answer"]
    assert hit["url"] == "safe-library/themes/grove.json"


def test_evidence_is_stored_and_surfaced(corpus):
    """A verification done outside jeles — the strongest form of it — must not
    be flattened to a bare 'asserted' on the way in. `evidence` carries it
    through untouched, for a reader who does hold the key to check."""
    seal = {
        "mechanism": "seal_sig",
        "sig": "deadbeef",
        "chain": "nestor",
        "signer": "a-human-who-read-it",
    }
    rid = corpus.put_nugget(
        "Is X true?",
        "Yes.",
        ["s"],
        "the operator",
        verification_kind="asserted",
        evidence=seal,
    )["id"]

    nugget = corpus.get_nugget(rid)
    assert nugget["evidence"] == seal

    hit = corpus.to_search_hit(nugget)
    assert hit["evidence"] == seal
    # jeles never checks it — it is asserted/unverified either way.
    assert hit["confidence"] == "unverified"


def test_a_nugget_without_evidence_is_unchanged(corpus):
    """Optional and backward compatible: a write that never mentions evidence
    must behave exactly as it did before this field existed."""
    result = _seed_grove(corpus)
    nugget = corpus.get_nugget(result["id"])
    assert "evidence" not in nugget

    hit = corpus.to_search_hit(nugget)
    assert hit["evidence"] == {}


def test_evidence_must_be_a_dict(corpus):
    result = corpus.put_nugget("Is X true?", "Yes.", ["s"], "the operator", evidence="not-a-dict")
    assert "error" in result and "evidence" in result["error"]


def test_list_nuggets_most_recent_first(corpus):
    first = _seed_grove(corpus)
    second = corpus.put_nugget(
        question="What's the accent color in Nord?",
        answer="The accent color in Nord is #88c0d0 (ice blue).",
        sources=["safe-library/themes/nord.json"],
        verified_by="designer",
    )
    nuggets = corpus.list_nuggets()
    assert nuggets[0]["_id"] == second["id"]
    assert nuggets[1]["_id"] == first["id"]


def test_control_chars_stripped_at_write_boundary(corpus):
    # B-009 (shared with the-squirrel): C0 control chars have no place in a
    # stored nugget — a NUL that truncates C-string tooling, a BEL nobody can
    # retype. Tab/newline survive.
    nid = corpus.put_nugget(
        question="What is\x00 a Vespa?",
        answer="A scooter.\x07 Made by Piaggio.\nItalian design.",
        sources=["ex\x1fample.com"],
        verified_by="ed\x08itor",
    )
    n = corpus.get_nugget(nid["id"])
    assert "\x00" not in n["question"] and n["question"] == "What is a Vespa?"
    assert "\x07" not in n["answer"] and "\x08" not in n["verified_by"]
    assert "\nItalian design." in n["answer"]  # newline preserved
    assert n["sources"] == ["example.com"]  # cleaned inside the list too


def test_logged_gap_is_sanitized(corpus):
    corpus.log_gap("who is\x00 nobody?")
    g = corpus.list_gaps()[0]
    assert "\x00" not in g["question"]


# ── Answering wrongly is worse than not answering ───────────────────────────
#
# The old score was `matched / len(query_tokens)` — recall only. Two questions
# differing by the one word that changes the answer share every other word, so
# the distinguishing token was worth 1/N of the decision and `MIN_ASK_SCORE`
# (half overlap) waved them through. Each case below was verified returning
# `found: True` with the wrong nugget before the fix.


def _seed(corpus, question, answer, **kw):
    return corpus.put_nugget(
        question=question, answer=answer, sources=["s"], verified_by="human", **kw
    )


def test_a_different_environment_is_a_different_question(corpus):
    """The one that would page someone: staging credentials answering a
    production question. Old score 0.95, found=True."""
    _seed(
        corpus,
        "How do I rotate the staging database password?",
        "Run `ops rotate --env staging`.",
        tags=["production", "rotate"],
    )

    asked = corpus.ask_corpus("How do I rotate the production database password?")

    assert asked["found"] is False
    assert asked["nugget"] is None
    # Still surfaced as a near-miss — "I don't know, but this is close" is
    # useful; "here is your answer" is not.
    assert asked["candidates"], "a close nugget should still come back as a candidate"


def test_a_different_subject_is_a_different_question(corpus):
    """Old score 0.90, found=True — answered a covid question with flu advice."""
    _seed(
        corpus,
        "Is the flu vaccine safe during pregnancy?",
        "Yes - the inactivated flu vaccine is recommended.",
    )
    assert corpus.ask_corpus("Is the covid vaccine safe during pregnancy?")["found"] is False


def test_a_tag_cannot_carry_a_wrong_nugget_over_the_threshold(corpus):
    """Old score 0.60, found=True. Half the overlap was the generic word
    'policy'; the +0.1 that pushed it over came from a tag matching the very
    word that made the questions different."""
    _seed(
        corpus,
        "What is the privacy policy?",
        "We keep logs for 90 days.",
        tags=["policy", "refund"],
    )
    assert corpus.ask_corpus("What is the refund policy?")["found"] is False


def test_a_query_far_broader_than_the_nugget_is_not_confident(corpus):
    """Rule 1 alone would pass this: every query token *is* in the nugget. The
    symmetric measure is what refuses it — one word does not ask a specific
    question, and answering it invents the rest."""
    _seed(corpus, "Is the flu vaccine safe during pregnancy?", "Yes.")
    assert corpus.ask_corpus("vaccine")["found"] is False


@pytest.mark.parametrize(
    "question",
    [
        "What's the primary color in Grove?",  # exact
        "primary color in Grove",  # same tokens, no filler
        "What is the primary color in Grove???",  # punctuation only
    ],
)
def test_the_questions_it_should_answer_still_answer(corpus, question):
    """The fix must not buy correctness with uselessness."""
    _seed_grove(corpus)
    assert corpus.ask_corpus(question)["found"] is True


def test_a_more_general_question_still_reaches_a_specific_nugget(corpus):
    """Deliberate: the asker used no word the nugget lacks, and the overlap is
    strong, so the specific nugget answers. Documented because it is a genuine
    judgement call — the corpus knows only about staging and says so in the
    answer text."""
    _seed(
        corpus, "How do I rotate the staging database password?", "Run `ops rotate --env staging`."
    )
    assert corpus.ask_corpus("how do I rotate the database password?")["found"] is True


def test_ranking_and_answering_are_separate_decisions(corpus):
    """`search_nuggets` stays a loose ranked lookup — it should still surface a
    near-miss that `ask_corpus` refuses to answer with."""
    _seed(
        corpus,
        "What is the privacy policy?",
        "We keep logs for 90 days.",
        tags=["policy", "refund"],
    )

    assert corpus.search_nuggets("refund policy"), "search should still find it"
    assert corpus.ask_corpus("What is the refund policy?")["found"] is False


def test_a_lower_ranked_but_confident_nugget_is_not_lost(corpus):
    """Confidence is checked across candidates, not only the top-ranked one, so
    a near-miss that ranks higher cannot hide a nugget that actually answers."""
    _seed(corpus, "What is the refund policy?", "Refunds within 30 days.")
    _seed(
        corpus,
        "What is the privacy policy?",
        "We keep logs for 90 days.",
        tags=["refund", "refund", "refund"],
    )

    asked = corpus.ask_corpus("What is the refund policy?")
    assert asked["found"] is True
    assert asked["nugget"]["answer"] == "Refunds within 30 days."


# ── The write path ──────────────────────────────────────────────────────────
#
# `_put` used INSERT OR REPLACE, which deletes the row and inserts a new one —
# so every column it did not name reverted to its schema default. Each case
# below was verified broken before the fix.


def _raw(corpus, rid, column):
    return (
        corpus._conn(corpus.NUGGETS_COLLECTION)
        .execute(f"SELECT {column} FROM records WHERE id = ?", (rid,))
        .fetchone()[0]
    )


def test_a_jeles_write_preserves_willow_mcp_columns(corpus):
    """The module carries `deviation`/`action` precisely so the store stays
    usable from both sides. Every jeles write used to reset them, making it
    compatible in exactly one direction — which is not compatible."""
    rid = _seed_grove(corpus)["id"]
    corpus._conn(corpus.NUGGETS_COLLECTION).execute(
        "UPDATE records SET deviation = 0.87, action = 'escalate' WHERE id = ?", (rid,)
    )

    corpus.put_nugget(
        "What's the primary color in Grove?", "Updated.", ["s"], "designer", nugget_id=rid
    )

    assert _raw(corpus, rid, "deviation") == 0.87
    assert _raw(corpus, rid, "action") == "escalate"


def test_a_re_put_does_not_resurrect_a_soft_deleted_record(corpus):
    """`deleted` was hardcoded to 0 on every write, so anyone who could write
    could undo a delete."""
    rid = _seed_grove(corpus)["id"]
    corpus._conn(corpus.NUGGETS_COLLECTION).execute(
        "UPDATE records SET deleted = 1 WHERE id = ?", (rid,)
    )

    result = corpus.put_nugget(
        "What's the primary color in Grove?", "Sneaky.", ["s"], "designer", nugget_id=rid
    )

    assert _raw(corpus, rid, "deleted") == 1, "the tombstone must survive a write"
    assert corpus.get_nugget(rid).get("error"), "and the record stays invisible"
    # Not refused — refusing would let anyone who can soft-delete permanently
    # deny an id — but not reported as a create either.
    assert result["action"] == "updated_tombstoned"


def test_created_at_survives_an_update(corpus):
    rid = _seed_grove(corpus)["id"]
    created = corpus.get_nugget(rid)["_created"]
    corpus.put_nugget(
        "What's the primary color in Grove?", "Updated.", ["s"], "designer", nugget_id=rid
    )
    assert corpus.get_nugget(rid)["_created"] == created


def test_concurrent_asks_do_not_lose_gap_counts(corpus):
    """`log_gap` read the count and wrote count+1 in two separate transactions,
    so concurrent asks all read the same number: measured 14 after 50 calls.
    `list_gaps` sorts by this, so the growth queue was ordered by a number that
    undercounted exactly the questions being asked most."""
    import threading

    question = "what is the accent color in Tokyo Night?"
    threads = [threading.Thread(target=corpus.log_gap, args=(question,)) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    gap = next(g for g in corpus.list_gaps() if "Tokyo Night" in g["question"])
    assert gap["asked_count"] == 50


def test_concurrent_asks_produce_one_gap_not_fifty(corpus):
    import threading

    threads = [
        threading.Thread(target=corpus.log_gap, args=("one shared question",)) for _ in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len([g for g in corpus.list_gaps() if g["question"] == "one shared question"]) == 1


def test_the_store_is_in_wal_mode(corpus):
    """WAL lets a reader run while a writer holds the database. Without it, a
    willow-mcp reader and a jeles writer on the shared store raise
    `database is locked` out of functions documented to return dicts."""
    _seed_grove(corpus)
    mode = corpus._conn(corpus.NUGGETS_COLLECTION).execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_a_failed_write_rolls_back(corpus):
    """`_write` must not leave a half-applied transaction behind."""
    import pytest as _pytest

    conn = corpus._conn(corpus.NUGGETS_COLLECTION)
    before = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    with _pytest.raises(RuntimeError), corpus._write(conn):
        conn.execute(
            "INSERT INTO records (id, data, created_at, updated_at, deleted) "
            "VALUES ('x', '{}', 'n', 'n', 0)"
        )
        raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == before


# ── A write cannot claim more than it is entitled to ─────────────────────────
#
# Three rungs — human > machine > asserted — and the rung is the product. The
# threat is not another app's data; it is an agent that has just read the open
# web writing what it read into the settled layer. `corpus_put` is where that
# lands, but the invariant belongs here, because a rule enforced at one caller
# is enforced at none.


def test_an_asserted_nugget_does_not_read_as_verified(corpus):
    rid = corpus.put_nugget(
        "Is X true?",
        "Yes.",
        ["https://evil.example/p"],
        "the operator",
        verification_kind="asserted",
        written_by="some-mcp-client",
    )["id"]
    hit = corpus.to_search_hit(corpus.get_nugget(rid))
    assert hit["confidence"] == "unverified"
    assert hit["verification_kind"] == "asserted"
    # The line a reader actually sees must not say "Verified corpus", and must
    # name the app that wrote it rather than the name that app typed.
    assert "Verified corpus" not in hit["source"]
    assert "some-mcp-client" in hit["source"]
    assert corpus.get_nugget(rid)["status"] == "asserted"


def test_ask_corpus_does_not_answer_from_an_assertion(corpus):
    """`found: true` is the settled layer speaking. A caller that reads only
    `nugget["answer"]` — most of them — has no other way to tell."""
    corpus.put_nugget("Is X true?", "Yes.", ["s"], "the operator", verification_kind="asserted")

    asked = corpus.ask_corpus("Is X true?")
    assert asked["found"] is False
    # Reachable, just not authoritative.
    assert [n["answer"] for n in asked["candidates"]] == ["Yes."]
    assert corpus.search_nuggets("Is X true?")[0]["answer"] == "Yes."
    assert corpus.ask_corpus("Is X true?", include_asserted=True)["found"] is True


def test_ask_corpus_still_answers_from_machine_corroboration(corpus):
    """Only the bottom rung is excluded — conflict_scan's findings still answer."""
    corpus.put_nugget("Is Y true?", "Yes.", ["s"], "conflict_scan", verification_kind="machine")
    assert corpus.ask_corpus("Is Y true?")["found"] is True


def test_an_assertion_cannot_overwrite_a_verified_nugget(corpus):
    """Without this, every other protection is one `nugget_id=` away from being
    bypassed: the assertion lands on the verified nugget, keeping its id and its
    place in every search result. Verified before the fix — the human answer was
    replaced with "Actually it is #000000."."""
    real = _seed_grove(corpus)

    refused = corpus.put_nugget(
        "What's the primary color in Grove?",
        "Actually it is #000000.",
        ["https://evil.example/p"],
        "designer",
        nugget_id=real["id"],
        verification_kind="asserted",
    )

    assert refused["error"] == "kind_downgrade_refused"
    assert refused["existing_kind"] == "human" and refused["attempted_kind"] == "asserted"
    nugget = corpus.get_nugget(real["id"])
    assert nugget["answer"].endswith("#ffffff (white).")
    assert nugget["verification_kind"] == "human"


def test_machine_cannot_overwrite_human_either(corpus):
    real = _seed_grove(corpus)
    refused = corpus.put_nugget(
        "What's the primary color in Grove?",
        "#eeeeee.",
        ["s"],
        "conflict_scan",
        nugget_id=real["id"],
        verification_kind="machine",
    )
    assert refused["error"] == "kind_downgrade_refused"


def test_a_person_can_still_supersede_and_promote(corpus):
    """The rule is one-directional: writing at the same rung or a higher one is
    ordinary editing, and must not be caught by the guard."""
    asserted = corpus.put_nugget(
        "Is X true?", "Maybe.", ["s"], "client", verification_kind="asserted"
    )
    same = corpus.put_nugget(
        "Is X true?",
        "Still maybe.",
        ["s"],
        "client",
        nugget_id=asserted["id"],
        verification_kind="asserted",
    )
    assert same["action"] == "updated"

    promoted = corpus.put_nugget(
        "Is X true?", "Yes — checked.", ["s"], "designer", nugget_id=asserted["id"]
    )
    assert promoted["verification_kind"] == "human"
    assert corpus.to_search_hit(corpus.get_nugget(asserted["id"]))["confidence"] == "verified"


def test_a_refused_write_changes_nothing(corpus):
    """The rung check runs inside the write transaction, so a refusal is not a
    partial write — and cannot be raced past by a concurrent one."""
    real = _seed_grove(corpus)
    before = corpus.get_nugget(real["id"])["_updated"]
    corpus.put_nugget("q?", "a", ["s"], "x", nugget_id=real["id"], verification_kind="asserted")
    assert corpus.get_nugget(real["id"])["_updated"] == before


def test_an_unrecognised_kind_is_refused_not_promoted(corpus):
    """It used to be `"machine" if kind == "machine" else "human"`, so every
    typo — and every unknown future rung — landed at the top."""
    result = corpus.put_nugget("q?", "a", ["s"], "x", verification_kind="verified")
    assert "error" in result and "verification_kind" in result["error"]
    assert corpus.list_nuggets() == []


def test_a_garbled_stored_kind_is_treated_as_the_highest_rung(corpus):
    """Reading is protective in the other direction: an unrecognised value on
    an existing record must not make it overwritable by anything."""
    rid = _seed_grove(corpus)["id"]
    corpus._put(
        corpus.NUGGETS_COLLECTION,
        {**corpus.get_nugget(rid), "verification_kind": "?"},
        record_id=rid,
    )

    assert corpus.to_search_hit(corpus.get_nugget(rid))["confidence"] == "verified"
    refused = corpus.put_nugget("q?", "a", ["s"], "x", nugget_id=rid, verification_kind="asserted")
    assert refused["error"] == "kind_downgrade_refused"


def test_a_legacy_nugget_without_a_kind_is_human(corpus):
    """Nuggets written before the field existed were human-entered, and must not
    become overwritable by adding the field."""
    rid = _seed_grove(corpus)["id"]
    legacy = {k: v for k, v in corpus.get_nugget(rid).items() if k != "verification_kind"}
    corpus._put(corpus.NUGGETS_COLLECTION, legacy, record_id=rid)

    assert corpus.to_search_hit(corpus.get_nugget(rid))["confidence"] == "verified"
    assert (
        corpus.put_nugget("q?", "a", ["s"], "x", nugget_id=rid, verification_kind="machine")[
            "error"
        ]
        == "kind_downgrade_refused"
    )


# ── A question the corpus holds must be answerable in the language it is in ──
#
# `_tokens` was `[a-z0-9][a-z0-9_-]{2,}` minus stopwords: ASCII-only, three
# characters minimum. `_ranked` returns [] when a query yields no tokens, so
# `ask_corpus` could not answer such a question at all — it logged a gap
# instead. Measured before the fix, storing four nuggets and asking each back
# with the byte-identical string: three returned found=False and filed a gap,
# so the growth queue collected questions the corpus already answered.


@pytest.mark.parametrize(
    "question",
    [
        "什么是主色?",  # Chinese — unspaced, so zero ASCII tokens
        "主色は何ですか?",  # Japanese
        "주요 색상은 무엇입니까?",  # Korean
        "¿Cuál es el color primario?",  # accented Latin — "cuál" was cut at the á
        "Is it up?",  # every content word under three chars
        "AI vs ML?",
    ],
)
def test_a_stored_question_answers_itself(corpus, question):
    """The headline failure: a nugget asked back with its own text returned
    found=False and filed a gap for a question that was already in the corpus."""
    _seed(corpus, question, "The answer.")

    asked = corpus.ask_corpus(question)

    assert asked["found"] is True, f"tokenized to {corpus._tokens(question)}"
    assert corpus.list_gaps() == [], "an answerable question must not file a gap"


def test_accented_words_are_not_truncated(corpus):
    """`[a-z0-9]` stopped at the first non-ASCII byte, so "cuál" produced no
    token at all (the `c`+`u` prefix is one character short of the minimum)."""
    assert corpus._tokens("¿Cuál es el color primario?") == ["cuál", "color", "primario"]


def test_cjk_is_cut_into_character_bigrams(corpus):
    """CJK is not space-delimited, so there is no word boundary to tokenize on.
    Bigrams are what let `search_nuggets` give partial credit for a shared
    phrase rather than matching whole runs all-or-nothing."""
    assert corpus._tokens("什么是主色?") == ["什么", "么是", "是主", "主色"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("What's the primary color in Grove?", ["primary", "color", "grove"]),
        (
            "How do I rotate the staging database password?",
            ["rotate", "staging", "database", "password"],
        ),
        (
            "Is the flu vaccine safe during pregnancy?",
            ["flu", "vaccine", "safe", "during", "pregnancy"],
        ),
    ],
)
def test_the_short_word_fallback_only_fires_when_nothing_else_matched(corpus, text, expected):
    """The three-character minimum is not lowered globally, only fallen back
    from. Lowering it would re-tokenize every nugget and shift every ranking;
    falling back means the short words are reached only where there were no
    tokens at all, so an ASCII question that already tokenized is unchanged."""
    assert corpus._tokens(text) == expected


# The discrimination rule is unchanged in the newly-reachable languages:
# answering wrongly is still worse than not answering.


def test_a_different_cjk_question_is_still_refused(corpus):
    """ "什么是强调色" (accent colour) shares its leading bigrams with
    "什么是主色" (primary colour) and differs in the ones carrying the subject."""
    _seed(corpus, "什么是主色?", "白色。")
    assert corpus.ask_corpus("什么是强调色?")["found"] is False


def test_a_different_short_question_is_still_refused(corpus):
    _seed(corpus, "AI vs ML?", "Different fields.")
    assert corpus.ask_corpus("AI vs BI?")["found"] is False


def test_reaching_short_words_did_not_soften_the_english_discrimination(corpus):
    """The cases the repo already fixed, re-asserted against the new tokenizer:
    a token the nugget's question lacks is still disqualifying, and a query far
    broader than the nugget still scores under MIN_ASK_SCORE."""
    _seed(
        corpus,
        "How do I rotate the staging database password?",
        "Run `ops rotate --env staging`.",
        tags=["production", "rotate"],
    )
    _seed(corpus, "Is the flu vaccine safe during pregnancy?", "Yes.")

    assert corpus.ask_corpus("How do I rotate the production database password?")["found"] is False
    assert corpus.ask_corpus("Is the covid vaccine safe during pregnancy?")["found"] is False
    assert corpus.ask_corpus("vaccine")["found"] is False


# ── A gap's identity ────────────────────────────────────────────────────────
#
# `log_gap` keyed on `sorted(set(_tokens(question)))`. Sorting is what merges
# rephrasings — the feature — and it is also what merged two opposite
# migrations into gap d2ceaf6ce807 with asked_count 2, storing only the
# question asked last. The first question was erased from the record entirely.


def test_opposite_order_questions_are_separate_gaps(corpus):
    a = corpus.log_gap("how do I migrate from postgres to mysql?")
    b = corpus.log_gap("how do I migrate from mysql to postgres?")

    assert a["id"] != b["id"]
    assert a["asked_count"] == 1 and b["asked_count"] == 1
    # Both question texts survive — neither was overwritten by the other.
    assert {g["question"] for g in corpus.list_gaps()} == {
        "how do I migrate from postgres to mysql?",
        "how do I migrate from mysql to postgres?",
    }


def test_a_rephrasing_still_merges_into_one_gap(corpus):
    """Merging rephrasings is why the key is token-based at all. Moving a whole
    phrase leaves the content tokens adjacent in the same order, so it merges."""
    a = corpus.log_gap("Does drug A interact with X?")
    b = corpus.log_gap("With X, does drug A interact?")

    assert a["id"] == b["id"]
    assert b["asked_count"] == 2
    assert len(corpus.list_gaps()) == 1


def test_a_merged_gap_keeps_the_first_phrasing_and_records_the_rest(corpus):
    """What the key still merges must not cost a question. "v1"/"v2" are short
    codes, and the short-code segment is an unordered set, so these two do share
    a gap — but both phrasings are in the record."""
    corpus.log_gap("how do I migrate from v1 to v2?")
    corpus.log_gap("how do I migrate from v2 to v1?")

    gap = corpus.list_gaps()[0]
    assert gap["asked_count"] == 2
    assert gap["question"] == "how do I migrate from v1 to v2?"
    assert gap["variants"] == ["how do I migrate from v2 to v1?"]


def test_the_variant_list_is_bounded(corpus):
    """A gap record is a queue item, not an audit log: a question asked with a
    new phrasing every time must not grow its row without limit."""
    # Every prefix is made only of stopwords, so all twelve share one gap key.
    prefixes = [
        "what is",
        "can you show",
        "would you find",
        "did you have",
        "how about",
        "which was",
        "please tell",
        "who has",
        "why is",
        "when did",
        "where are",
        "should you give",
    ]
    for prefix in prefixes:
        corpus.log_gap(f"{prefix} the accent color in tokyo night?")

    assert len(corpus.list_gaps()) == 1
    gap = corpus.list_gaps()[0]
    assert gap["asked_count"] == 12
    assert gap["question"] == "what is the accent color in tokyo night?"
    assert len(gap["variants"]) == corpus._MAX_GAP_VARIANTS


# ── A short word can be the word that changes the answer ────────────────────
#
# `_tokens` uses short words only as a fallback, when a question yields nothing
# else — so a short word alongside long ones stayed invisible. Measured before
# the fix, and it is the bad direction of the pair:
#
#   stored 'Is the API down?'  ->  "outage since 14:00"
#   asked  'Is the API up?'    ->  found: True, confidence 0.667
#
# "up" is two characters and never tokenized; "down" is four and did. The asked
# set was a subset of the known set, so rule 1 saw nothing unmatched, and
# precision 1/2 still cleared the 0.5 threshold. `_ask_tokens` carries the short
# words into the *decision* only — ranking and gap keys still use `_tokens`.


def test_an_outage_does_not_answer_is_it_up(corpus):
    corpus.put_nugget("Is the API down?", "Yes - outage since 14:00.", ["s"], "designer")
    asked = corpus.ask_corpus("Is the API up?")
    assert asked["found"] is False, "an outage answered 'is it up?' with 'yes'"
    assert asked["candidates"], "still a near-miss worth showing"


def test_the_same_short_question_still_answers_itself(corpus):
    corpus.put_nugget("Is the API down?", "Yes.", ["s"], "designer")
    assert corpus.ask_corpus("Is the API down?")["found"] is True


def test_ranking_still_uses_the_narrow_token_set(corpus):
    """`_ask_tokens` is for deciding, not ordering. If it leaked into `_score`
    every existing nugget would re-rank on words like 'up' and 'v2'."""
    assert corpus._tokens("Is the API up?") == ["api"]
    assert corpus._ask_tokens("Is the API up?") == ["api", "up"]


def test_a_two_letter_preposition_is_not_a_content_word(corpus):
    """The cost of counting short words is that 'of' vs 'in' would refuse a
    pure rephrasing. They are stopped; 'up' and 'down' are not."""
    corpus.put_nugget("What is the primary color in Grove?", "White.", ["s"], "designer")
    assert corpus.ask_corpus("What is the primary color of Grove?")["found"] is True


def test_an_apostrophe_does_not_split_into_a_content_word(corpus):
    """Regression caught while fixing the above: "what's" split into "what"
    plus a bare "s", and `_ask_tokens` read that "s" as a content word the
    other phrasing lacked — so a contraction stopped matching its long form."""
    assert corpus._ask_tokens("What's the primary color in Grove?") == corpus._ask_tokens(
        "What is the primary color in Grove?"
    )
    corpus.put_nugget("What is the primary color in Grove?", "White.", ["s"], "designer")
    assert corpus.ask_corpus("What's the primary color in Grove?")["found"] is True


def test_single_letters_stay_meaning_bearing(corpus):
    """Also caught while fixing the above: putting "a" in the stop set would
    have broken "drug A" vs "drug B", which is the case log_gap's short-code
    segment exists for (tests/test_hardening.py)."""
    assert "a" not in corpus._STOP and "i" not in corpus._STOP
    a = corpus.log_gap("Does drug A interact with X?")
    b = corpus.log_gap("Does drug B interact with X?")
    assert a["id"] != b["id"]
    assert len(corpus.list_gaps()) == 2


# ── Closing a gap ───────────────────────────────────────────────────────────
#
# The gap queue was write-only: `log_gap` wrote it, `list_gaps` read it, and
# nothing in the package could ever say a question had been answered. Two
# things followed. The `status` field was written on every record and read by
# nobody, so it held one value forever; and `list_gaps` sorts by `asked_count`,
# which freezes once a gap is answered (`ask_corpus` starts hitting the nugget
# and stops logging) — so an answered question kept its historical count and
# outranked every newer open gap, the queue decaying exactly as the corpus
# improved.


def test_resolving_a_gap_takes_it_out_of_the_queue(corpus):
    logged = corpus.log_gap("What is the accent color in Tokyo Night?")
    assert len(corpus.list_gaps()) == 1

    result = corpus.resolve_gap(logged["id"], resolved_by="designer")
    assert result["status"] == "resolved"
    assert result["resolved_at"]
    assert corpus.list_gaps() == []


def test_a_resolved_gap_is_kept_not_deleted(corpus):
    """The record is the corpus's history of its own blind spots. It is worth
    more once the hole is filled than while it is open."""
    logged = corpus.log_gap("What is the accent color in Tokyo Night?")
    corpus.resolve_gap(logged["id"], resolved_by="designer", nugget_id="n1")

    kept = corpus.list_gaps(include_resolved=True)
    assert len(kept) == 1
    assert kept[0]["question"] == "What is the accent color in Tokyo Night?"
    assert kept[0]["status"] == "resolved"
    assert kept[0]["resolved_by"] == "designer"
    assert kept[0]["nugget_id"] == "n1"


def test_an_answered_gap_stops_outranking_newer_open_ones(corpus):
    """The bug this exists for. The resolved gap has the higher `asked_count`
    and would sit above the open one forever."""
    old = corpus.log_gap("an old question")
    for _ in range(9):
        corpus.log_gap("an old question")
    corpus.log_gap("a new question")

    assert corpus.list_gaps()[0]["question"] == "an old question"
    corpus.resolve_gap(old["id"])
    assert [g["question"] for g in corpus.list_gaps()] == ["a new question"]


def test_a_resolved_gap_asked_again_reopens(corpus):
    """A miss is proof the settled layer stopped covering it. The earlier
    resolution is kept so a recurring hole does not read as a new one."""
    logged = corpus.log_gap("What is the accent color in Tokyo Night?")
    corpus.resolve_gap(logged["id"], resolved_by="designer")
    assert corpus.list_gaps() == []

    again = corpus.log_gap("What is the accent color in Tokyo Night?")
    assert again["id"] == logged["id"]

    reopened = corpus.list_gaps()
    assert len(reopened) == 1
    assert reopened[0]["status"] == "open"
    assert reopened[0]["resolved_at"], "the earlier resolution is kept"
    assert reopened[0]["reopened_at"]
    assert reopened[0]["asked_count"] == 2


def test_resolving_again_after_a_reopen_clears_the_reopen_stamp(corpus):
    """Otherwise the record is dated to the last time it was open."""
    logged = corpus.log_gap("a question")
    corpus.resolve_gap(logged["id"])
    corpus.log_gap("a question")
    corpus.resolve_gap(logged["id"])

    record = corpus.list_gaps(include_resolved=True)[0]
    assert record["status"] == "resolved"
    assert "reopened_at" not in record


def test_resolve_requires_a_real_gap_id(corpus):
    assert corpus.resolve_gap("") == {"error": "gap_id required"}
    assert corpus.resolve_gap("does-not-exist") == {"error": "not_found"}


def test_a_gap_from_before_gaps_could_be_closed_counts_as_open(corpus):
    """Records written by earlier versions carry `status: "unverified"`. The
    rule is "anything not resolved is open", so they stay in the queue rather
    than vanishing from it on upgrade."""
    logged = corpus.log_gap("a question")
    gap = corpus.list_gaps()[0]
    assert gap["status"] == "open"

    # Rewrite it the way the old code did.
    import json

    with corpus._lock:
        conn = corpus._conn(corpus.GAPS_COLLECTION)
        record = dict(gap)
        for key in ("_id", "_created", "_updated"):
            record.pop(key, None)
        record["status"] = "unverified"
        with corpus._write(conn):
            conn.execute(
                "UPDATE records SET data = ? WHERE id = ?", (json.dumps(record), logged["id"])
            )

    assert len(corpus.list_gaps()) == 1
    assert corpus.list_gaps()[0]["status"] == "unverified"
