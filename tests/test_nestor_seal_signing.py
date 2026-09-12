"""jeles._nestor_seal, with a real `nestor` present — the signature checks
that actually need it: a valid seal verifies, a forged one refuses, a
transplant (same signature, a different question/answer/name) refuses, and
an unconfigured instance refuses rather than inheriting `nestor.signing`'s
own "nothing configured" legacy accept.

Skipped wholesale without the `[nestor]` extra (`pytest.importorskip` at
module scope, before any test — the `no-extras` CI leg installs base `jeles`
with no extras at all, same shape `test_corpus_server.py` handles for
`[mcp]`).
"""

from __future__ import annotations

import pytest

pytest.importorskip("nestor", reason="jeles[nestor] extra not installed")

from nestor import keyring as nestor_keyring
from nestor import signing as nestor_signing

from jeles import _nestor_seal


def _norm(question: str) -> str:
    """Sign the source the way Nestor really does.

    A seal covers `source_norm`, not the raw question (`nestor.memory`: "A
    seal signature covers (source_norm, target_text, verifier)"). Until
    2026-08-29 every test in this file signed the raw string and verified the
    raw string — self-consistent, and never once agreeing with Nestor, which
    is why `_nestor_seal` refused genuine seals for as long as it did. Signing
    through the same normalizer the module verifies through is what makes
    these tests about the bridge instead of about themselves.
    """
    from nestor.matcher import StringMatcher

    return StringMatcher().normalize(question)


@pytest.fixture(autouse=True)
def _clean_nestor_signing_state(monkeypatch):
    """Isolate every test here from whatever NESTOR_SEAL_KEY/NESTOR_KEYRING
    another test (or the ambient environment) left behind, and from
    `seal_is_valid`'s process-global "warned once" flag."""
    monkeypatch.delenv("NESTOR_SEAL_KEY", raising=False)
    monkeypatch.delenv("NESTOR_KEYRING", raising=False)
    monkeypatch.delenv("NESTOR_REQUIRE_SEAL_KEY", raising=False)
    nestor_keyring.set_keyring(None)
    monkeypatch.setattr(nestor_signing, "_warned_unsigned", False)
    yield
    nestor_keyring.set_keyring(None)


def _seal_evidence(sig: str) -> dict:
    return {"scheme": _nestor_seal.EVIDENCE_SCHEME, "seal_sig": sig}


# ── Nothing configured to verify against ────────────────────────────────────


def test_refuses_when_nothing_is_configured_to_verify_against(monkeypatch):
    """No NESTOR_SEAL_KEY, no keyring: `nestor.signing.seal_is_valid`'s OWN
    default is to warn once and then ACCEPT (its documented legacy
    behaviour, there so an existing unsigned Nestor deployment keeps
    working). This module must not inherit that default — a claim arriving
    at Jeles over a tool call has no unsigned-deployment history to be
    backward compatible with."""
    sig = nestor_signing.sign_seal(_norm("q?"), "a.", "rita", key=b"some-key-nobody-configured")
    ok, reason = _nestor_seal.verify_human_write("q?", "a.", "rita", _seal_evidence(sig))
    assert ok is False
    assert "configured" in reason

    # Confirm the premise: seal_is_valid's own bare call, with nothing
    # configured, really does accept (this is what point 3 in the module
    # docstring refuses to inherit).
    with pytest.warns(RuntimeWarning):
        assert nestor_signing.seal_is_valid("q?", "a.", "rita", "anything") is True


def test_refuses_a_forged_signature_under_a_shared_key(monkeypatch):
    """A tool caller that TYPES verified_by='rita' and a made-up hex string
    is refused — the exact forgery this give-back exists to close."""
    monkeypatch.setenv("NESTOR_SEAL_KEY", "the-deployment-secret")
    ok, reason = _nestor_seal.verify_human_write("q?", "a.", "rita", _seal_evidence("0" * 64))
    assert ok is False
    assert "does not verify" in reason


def test_a_real_seal_verifies_under_a_shared_key(monkeypatch):
    monkeypatch.setenv("NESTOR_SEAL_KEY", "the-deployment-secret")
    sig = nestor_signing.sign_seal(_norm("q?"), "a.", "rita")
    ok, reason = _nestor_seal.verify_human_write("q?", "a.", "rita", _seal_evidence(sig))
    assert (ok, reason) == (True, "ok")


# ── Per-verifier keyring — a signature is evidence about a *person* ────────


def test_a_real_seal_verifies_under_a_keyring_entry():
    """The full give-back path: `rita`'s own key, not a deployment-wide
    secret — a signature is evidence about *her*."""
    kr = nestor_keyring.Keyring()
    kr.add("rita", kind="hmac")
    nestor_keyring.set_keyring(kr)

    sig = nestor_signing.sign_seal(_norm("q?"), "a.", "rita")
    ok, reason = _nestor_seal.verify_human_write("q?", "a.", "rita", _seal_evidence(sig))
    assert (ok, reason) == (True, "ok")


def test_an_unregistered_verifier_cannot_seal():
    kr = nestor_keyring.Keyring()
    kr.add("rita", kind="hmac")
    nestor_keyring.set_keyring(kr)

    # "sam" has no key in this keyring — sign_seal for sam would raise before
    # producing anything, so the caller here has no real signature to try.
    # Simulate the attempted forgery directly: rita's own valid signature,
    # claimed under sam's name.
    rita_sig = nestor_signing.sign_seal(_norm("q?"), "a.", "rita")
    ok, reason = _nestor_seal.verify_human_write("q?", "a.", "sam", _seal_evidence(rita_sig))
    assert ok is False
    assert "does not verify" in reason


def test_a_revoked_compromised_key_no_longer_verifies():
    kr = nestor_keyring.Keyring()
    kr.add("rita", kind="hmac")
    sig = nestor_signing.sign_seal(_norm("q?"), "a.", "rita", key=kr.get("rita").key)
    kr.revoke("rita", compromised=True)
    nestor_keyring.set_keyring(kr)

    ok, reason = _nestor_seal.verify_human_write("q?", "a.", "rita", _seal_evidence(sig))
    assert ok is False
    assert "does not verify" in reason


# ── A signature cannot be transplanted onto a different claim ──────────────


def test_a_valid_seal_does_not_verify_a_different_answer():
    """The signature is bound to (question, answer, verified_by) as one
    unit — Nestor's frozen wire encoding folds all three into the signed
    bytes. Reusing a genuine seal on a different answer is a transplant, and
    must fail exactly like a forged signature does."""
    kr = nestor_keyring.Keyring()
    kr.add("rita", kind="hmac")
    nestor_keyring.set_keyring(kr)

    sig = nestor_signing.sign_seal(_norm("q?"), "the real answer.", "rita")
    ok, reason = _nestor_seal.verify_human_write(
        "q?", "a different answer entirely.", "rita", _seal_evidence(sig)
    )
    assert ok is False
    assert "does not verify" in reason


def test_a_valid_seal_does_not_verify_a_different_question():
    kr = nestor_keyring.Keyring()
    kr.add("rita", kind="hmac")
    nestor_keyring.set_keyring(kr)

    sig = nestor_signing.sign_seal(_norm("what colour?"), "a.", "rita")
    ok, reason = _nestor_seal.verify_human_write("what shape?", "a.", "rita", _seal_evidence(sig))
    assert ok is False
    assert "does not verify" in reason


def test_a_valid_seal_does_not_verify_a_different_claimed_name():
    """Rita's real seal, replayed with `verified_by` changed to someone
    else's name — the message includes `verifier`, so this is caught the
    same way as the answer/question transplants above."""
    kr = nestor_keyring.Keyring()
    kr.add("rita", kind="hmac")
    kr.add("sam", kind="hmac")
    nestor_keyring.set_keyring(kr)

    sig = nestor_signing.sign_seal(_norm("q?"), "a.", "rita")
    ok, reason = _nestor_seal.verify_human_write("q?", "a.", "sam", _seal_evidence(sig))
    assert ok is False
    assert "does not verify" in reason


# ── The bridge: a seal made by Nestor, checked through Jeles ────────────────


def test_a_seal_over_the_normalized_source_verifies_a_raw_question():
    """The regression this file existed for and did not cover.

    Nestor signs `source_norm`. A caller passes `corpus_put` the question as
    typed — capitals, punctuation and all. If this module checks the raw
    string, a genuine seal is refused and the `human` rung is unreachable by
    the one mechanism built to reach it. Measured 2026-08-29 against a real
    ed25519 seal from the operator's own store: raw returned False,
    normalized returned True.
    """
    kr = nestor_keyring.Keyring()
    kr.add("rita", kind="hmac")
    nestor_keyring.set_keyring(kr)

    asked = "What colour is Grove's primary?"
    answer = "White (#ffffff)."
    sig = nestor_signing.sign_seal(_norm(asked), answer, "rita")

    ok, reason = _nestor_seal.verify_human_write(asked, answer, "rita", _seal_evidence(sig))
    assert (ok, reason) == (True, "ok"), (
        "a seal signed the way Nestor signs must verify a question as typed"
    )


def test_punctuation_and_case_do_not_change_the_verdict():
    """Normalization is what makes the seal survive the difference between
    how a fact was sealed and how it is later asked."""
    kr = nestor_keyring.Keyring()
    kr.add("rita", kind="hmac")
    nestor_keyring.set_keyring(kr)

    sig = nestor_signing.sign_seal(_norm("what colour is grove"), "White.", "rita")
    for phrasing in ("What colour is Grove?", "what colour is grove", "What  colour   is Grove!"):
        ok, _ = _nestor_seal.verify_human_write(phrasing, "White.", "rita", _seal_evidence(sig))
        assert ok is True, f"{phrasing!r} normalizes to the sealed source"


def test_a_different_question_still_refuses_after_normalizing():
    """Normalizing must not blur two questions into one. Only formatting is
    discarded; a different content word is still a different claim."""
    kr = nestor_keyring.Keyring()
    kr.add("rita", kind="hmac")
    nestor_keyring.set_keyring(kr)

    sig = nestor_signing.sign_seal(_norm("what colour is Grove?"), "White.", "rita")
    ok, reason = _nestor_seal.verify_human_write(
        "what colour is Tokyo Night?", "White.", "rita", _seal_evidence(sig)
    )
    assert ok is False and "does not verify" in reason
