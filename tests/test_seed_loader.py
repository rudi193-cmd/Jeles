"""seed_loader — the batch that ships in the wheel, and the rung it lands at.

Offline throughout. The signing tests use nestor's HMAC keyring, which needs
the `[nestor]` extra; they skip without it, the same shape
`test_nestor_seal_signing.py` uses.
"""

from __future__ import annotations

import json

import pytest

from jeles import seed_loader


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("WILLOW_STORE_ROOT", str(tmp_path / "store"))
    from jeles import corpus as corpus_module

    return corpus_module


def _write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _pairs(*pairs, domain="test-domain", **extra):
    return {"domain": domain, "pairs": list(pairs), **extra}


# ── The seed actually ships ─────────────────────────────────────────────────


def test_the_seed_is_inside_the_installed_package():
    """The packaging defect this module exists for.

    `pip install jeles` shipped ZERO seed facts until 2026-08-29: the wheel is
    built from `packages = ["jeles"]`, and the corpus lived in `corpus/` at the
    repo root — beside the package, not inside it. `cards/` and `persona/` rode
    along because they are subdirectories of `jeles/`; 74 files of
    adversarially reviewed research did not. Anyone installing got the organ
    with an empty corpus and no way to know that was not intended.
    """
    files = seed_loader.seed_files()
    assert seed_loader.SEED_DIR.is_dir(), (
        "the seed must live inside jeles/ or the wheel will not carry it"
    )
    assert len(files) > 50, f"expected the bundled batch, found {len(files)}"


def test_seed_files_is_empty_rather_than_raising_when_absent(tmp_path):
    """A base install with no seed is a valid install, not a crash."""
    assert seed_loader.seed_files(tmp_path / "nothing-here") == []


# ── An unsigned pair is an assertion, and says so ───────────────────────────


def test_an_unsigned_pair_lands_at_the_bottom_rung(store, tmp_path):
    p = _write(
        tmp_path,
        "s.json",
        _pairs({"source_text": "What colour is Grove?", "target_text": "White."}),
    )
    out = seed_loader.load_file(p)

    assert (out["asserted"], out["human"]) == (1, 0)
    nugget = store.search_nuggets("Grove colour", limit=1)[0]
    assert nugget["verification_kind"] == "asserted"
    assert nugget["verified_by"] == seed_loader.UNSIGNED_CLAIMANT
    assert nugget["written_by"] == seed_loader.WRITTEN_BY


def test_an_unsigned_seed_cannot_answer_a_question(store, tmp_path):
    """The reason shipping at `asserted` is not a hedge: ask_corpus refuses to
    answer from an unchecked claim, so an unsigned seed installs as candidates
    and the reader is told nobody has checked it."""
    _write(
        tmp_path,
        "s.json",
        _pairs({"source_text": "What colour is Grove?", "target_text": "White."}),
    )
    seed_loader.load_file(tmp_path / "s.json")

    result = store.ask_corpus("What colour is Grove?")
    assert result["found"] is False
    assert len(result["candidates"]) == 1, "it is offered, not asserted as true"


# ── The rounds' other artifacts are not nuggets ─────────────────────────────


def test_a_challenge_list_is_skipped_not_loaded(store, tmp_path):
    """Two shipped files are lists of adversarial challenges. A challenge
    filed as a nugget would answer a question with an objection."""
    p = _write(tmp_path, "c.json", [{"id": "c1", "challenge": "is it though?"}])
    out = seed_loader.load_file(p)
    assert out["skipped"] == "not a pair set"
    assert (out["asserted"], out["errors"]) == (0, 0)


def test_a_steelman_record_is_counted_apart_from_an_error(store, tmp_path):
    """36 shipped entries are steelman arguments sharing the `pairs` key
    without the pair shape. Broken and not-a-pair call for different
    responses, so they are counted apart."""
    p = _write(
        tmp_path,
        "s.json",
        _pairs(
            {"id": "s1", "steelman": "the strongest version", "thesis": "..."},
            {"source_text": "A real question?", "target_text": "A real answer."},
        ),
    )
    out = seed_loader.load_file(p)
    assert out["not_pairs"] == 1
    assert out["errors"] == 0, "a steelman is not a defect"
    assert out["asserted"] == 1, "the real pair beside it still loads"


def test_dry_run_writes_nothing(store, tmp_path):
    p = _write(
        tmp_path,
        "s.json",
        _pairs({"source_text": "What colour is Grove?", "target_text": "White."}),
    )
    out = seed_loader.load_file(p, dry_run=True)
    assert out["asserted"] == 1
    assert store.list_nuggets() == [], "a dry run must not touch the store"


# ── A seal, checked on the installing machine ───────────────────────────────

nestor_signing = pytest.importorskip("nestor.signing", reason="jeles[nestor] extra not installed")
nestor_keyring = pytest.importorskip("nestor.keyring")


@pytest.fixture()
def rita_keyring():
    kr = nestor_keyring.Keyring()
    kr.add("rita", kind="hmac")
    nestor_keyring.set_keyring(kr)
    yield kr
    nestor_keyring.set_keyring(None)


def _seal(question, answer, verifier="rita"):
    from nestor.matcher import StringMatcher

    return nestor_signing.sign_seal(StringMatcher().normalize(question), answer, verifier)


def test_a_verifying_seal_earns_the_human_rung(store, tmp_path, rita_keyring):
    """What the whole shipment is for: `verified` becomes something the
    RECIPIENT confirms against a keyring they trust, rather than a flag the
    sender set."""
    q, a = "What colour is Grove's primary?", "White (#ffffff)."
    p = _write(
        tmp_path,
        "s.json",
        _pairs(
            {"source_text": q, "target_text": a, "verified_by": "rita", "seal_sig": _seal(q, a)}
        ),
    )

    out = seed_loader.load_file(p)
    assert (out["human"], out["asserted"]) == (1, 0)

    nugget = store.search_nuggets("Grove primary colour", limit=1)[0]
    assert nugget["verification_kind"] == "human"
    assert nugget["verified_by"] == "rita"
    assert nugget["evidence"]["seal_sig"], "the signature travels with the row"


def test_a_sealed_pair_can_answer_where_an_unsigned_one_cannot(store, tmp_path, rita_keyring):
    q, a = "What colour is Grove's primary?", "White (#ffffff)."
    _write(
        tmp_path,
        "s.json",
        _pairs(
            {"source_text": q, "target_text": a, "verified_by": "rita", "seal_sig": _seal(q, a)}
        ),
    )
    seed_loader.load_file(tmp_path / "s.json")

    assert store.ask_corpus(q)["found"] is True


def test_a_seal_that_does_not_verify_lands_asserted_and_is_reported(store, tmp_path, rita_keyring):
    """A forged or foreign seal does not silently vanish and does not silently
    promote. The row loads at the rung it can prove, the signature travels for
    a reviewer, and the refusal is counted with its reason."""
    q, a = "What colour is Grove?", "White."
    p = _write(
        tmp_path,
        "s.json",
        _pairs({"source_text": q, "target_text": a, "verified_by": "rita", "seal_sig": "0" * 64}),
    )

    out = seed_loader.load_file(p)
    assert (out["human"], out["asserted"]) == (0, 1)
    assert sum(out["refusals"].values()) == 1
    assert any("does not verify" in r for r in out["refusals"])

    nugget = store.search_nuggets("Grove colour", limit=1)[0]
    assert nugget["verification_kind"] == "asserted"
    assert nugget["evidence"]["seal_sig"] == "0" * 64


def test_a_seal_for_a_different_answer_cannot_be_transplanted(store, tmp_path, rita_keyring):
    q = "What colour is Grove?"
    p = _write(
        tmp_path,
        "s.json",
        _pairs(
            {
                "source_text": q,
                "target_text": "Black.",
                "verified_by": "rita",
                "seal_sig": _seal(q, "White."),
            }
        ),
    )
    assert seed_loader.load_file(p)["human"] == 0


def test_the_signer_named_in_the_pair_is_the_one_checked(store, tmp_path, rita_keyring):
    """`verified_by` is bound into the signature, so a pair cannot borrow
    rita's seal and claim someone more impressive signed it."""
    q, a = "What colour is Grove?", "White."
    p = _write(
        tmp_path,
        "s.json",
        _pairs(
            {
                "source_text": q,
                "target_text": a,
                "verified_by": "someone eminent",
                "seal_sig": _seal(q, a, verifier="rita"),
            }
        ),
    )
    assert seed_loader.load_file(p)["human"] == 0
