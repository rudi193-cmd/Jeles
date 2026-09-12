"""jeles._nestor_seal — the cryptographic gate on the `human` corpus rung.

Shape checks and the "extra not installed" refusal, all independent of
whether `nestor` is actually available in this environment. The tests that
need a *working* nestor (a real signature verifying, a forged one refusing, a
transplant refusing) live in `test_nestor_seal_signing.py`, which
`pytest.importorskip("nestor", ...)`s at the top the same way
`test_corpus_server.py` does for `[mcp]` — kept in a separate file so that
skip guard can sit before any test is defined, rather than in the middle of
this one (ruff's E402 catches an import dropped after code has already run at
module scope, and a guard placed after a dozen test functions earns exactly
that flag).

`no_nestor` mirrors `test_willow_mcp_client.py`'s `no_willow_mcp` fixture:
``None`` in `sys.modules` is the import system's own "this import must fail"
sentinel, so it forces the "extra not installed" path deterministically
regardless of whether `nestor` happens to be present in the environment
running this suite (it is, via the `dev` extra's `jeles[nestor]` — see
pyproject.toml — everywhere except the `no-extras` CI leg, where it already
isn't).
"""

from __future__ import annotations

import sys

import pytest

from jeles import _nestor_seal


@pytest.fixture
def no_nestor(monkeypatch):
    monkeypatch.setitem(sys.modules, "nestor", None)


# ── Shape checks — independent of whether nestor is installed ──────────────


def test_refuses_with_no_evidence_at_all():
    ok, reason = _nestor_seal.verify_human_write("q?", "a.", "rita", None)
    assert ok is False
    assert "no evidence" in reason


def test_refuses_when_evidence_is_not_a_dict():
    ok, reason = _nestor_seal.verify_human_write("q?", "a.", "rita", "human, i promise")
    assert ok is False
    assert "no evidence" in reason


def test_refuses_an_unrecognised_scheme():
    ok, reason = _nestor_seal.verify_human_write(
        "q?", "a.", "rita", {"scheme": "trust-me", "seal_sig": "deadbeef"}
    )
    assert ok is False
    assert "scheme" in reason


def test_refuses_a_missing_seal_sig():
    ok, reason = _nestor_seal.verify_human_write(
        "q?", "a.", "rita", {"scheme": _nestor_seal.EVIDENCE_SCHEME}
    )
    assert ok is False
    assert "seal_sig" in reason


def test_refuses_an_empty_seal_sig():
    ok, reason = _nestor_seal.verify_human_write(
        "q?", "a.", "rita", {"scheme": _nestor_seal.EVIDENCE_SCHEME, "seal_sig": ""}
    )
    assert ok is False
    assert "seal_sig" in reason


def test_refuses_a_non_string_seal_sig():
    ok, reason = _nestor_seal.verify_human_write(
        "q?", "a.", "rita", {"scheme": _nestor_seal.EVIDENCE_SCHEME, "seal_sig": 12345}
    )
    assert ok is False
    assert "seal_sig" in reason


# ── The `[nestor]` extra is genuinely optional ──────────────────────────────


def test_refuses_when_the_nestor_extra_is_not_installed(no_nestor):
    """The forgeable path this closes (box scan B6): a tool caller that
    knows JELES_CORPUS_TRUST_TOOL_WRITES=1 is set, and types a plausible
    evidence dict, still cannot mint `human` without a real signature — and
    "the verifier can't even check" must mean refuse, not trust."""
    ok, reason = _nestor_seal.verify_human_write(
        "q?", "a.", "rita", {"scheme": _nestor_seal.EVIDENCE_SCHEME, "seal_sig": "not-checkable"}
    )
    assert ok is False
    assert "nestor extra not installed" in reason


# ── describe(): the same refusals, asked before a write instead of after ────
#
# `describe()` exists so a caller can learn the `human` rung is unreachable
# without attempting a write to find out. That is only worth anything if the
# two agree: a status saying "ready" over a write that refuses, or two
# different explanations for one condition, would be worse than no status at
# all. These pin them together rather than trusting the docstring's word.


def _reason_a_write_would_give():
    """What `verify_human_write` refuses with in this environment, given
    evidence shaped well enough to get past the shape checks."""
    ok, reason = _nestor_seal.verify_human_write(
        "q?", "a.", "rita", {"scheme": _nestor_seal.EVIDENCE_SCHEME, "seal_sig": "deadbeef"}
    )
    assert ok is False
    return reason


def _fake_nestor(monkeypatch, **signing_attrs):
    import types

    module = types.ModuleType("nestor")
    module.signing = types.SimpleNamespace(**signing_attrs)
    monkeypatch.setitem(sys.modules, "nestor", module)
    return module.signing


def test_describe_reports_a_missing_extra_exactly_as_a_write_would(no_nestor):
    described = _nestor_seal.describe()
    assert described["installed"] is False
    assert described["ready"] is False
    assert described["reason"] == _reason_a_write_would_give()


def test_describe_reports_an_unconfigured_instance_exactly_as_a_write_would(monkeypatch):
    _fake_nestor(
        monkeypatch,
        signing_enabled=lambda: False,
        seal_is_valid=lambda *a, **k: pytest.fail(
            "an unconfigured instance must be refused before seal_is_valid"
        ),
    )
    described = _nestor_seal.describe()
    assert described["installed"] is True
    assert described["signing_enabled"] is False
    assert described["ready"] is False
    assert described["reason"] == _reason_a_write_would_give()


def test_describe_is_ready_only_when_something_could_be_checked(monkeypatch):
    _fake_nestor(
        monkeypatch,
        signing_enabled=lambda: True,
        seal_is_valid=lambda *a, **k: pytest.fail("describe() must verify nothing"),
    )
    assert _nestor_seal.describe() == {
        "scheme": _nestor_seal.EVIDENCE_SCHEME,
        "installed": True,
        "signing_enabled": True,
        "ready": True,
        "reason": "ok",
    }


def test_describe_reports_a_failing_check_rather_than_raising(monkeypatch):
    """A caller asking only for a status must not be handed an exception."""

    def _boom():
        raise RuntimeError("keyring unreadable")

    _fake_nestor(monkeypatch, signing_enabled=_boom)
    described = _nestor_seal.describe()
    assert described["ready"] is False
    assert "RuntimeError" in described["reason"]


def test_describe_names_no_key_material(monkeypatch):
    """Every field is about *configuration*, never about what it contains."""
    import json

    monkeypatch.setenv("NESTOR_SEAL_KEY", "s3cr3t-must-not-leak")
    monkeypatch.setenv("NESTOR_KEYRING", "/home/someone/.nestor/keyring.json")
    _fake_nestor(monkeypatch, signing_enabled=lambda: True)

    rendered = json.dumps(_nestor_seal.describe())
    assert "s3cr3t-must-not-leak" not in rendered
    assert "keyring.json" not in rendered
