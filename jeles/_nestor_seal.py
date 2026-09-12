"""_nestor_seal — the cryptographic gate on the `human` corpus rung.

Give-back from `Nestor` (the sibling verified-match organ, whose whole reason
to exist is "a row that merely *says* `status='sealed'` will not serve").
Before this module, Jeles had the same forgery Nestor#2 found and fixed: the
`human` rung is supposed to mean "a person checked this," but the only tool
caller allowed to *mint* it — `corpus_server.corpus_put`, when the operator
has set `JELES_CORPUS_TRUST_TOOL_WRITES=1` for a session where they really are
the one typing — asked for nothing more than a plain string in `verified_by`.
Box scan `Willow/design/box-scan-2026-07-24.md` B6 named the shape of this
(a machine claim rendering identically to a human one); the residual gap after
`verification_kind` shipped is that the *trust switch* alone still let a tool
caller type its way onto the top rung. This module closes that: with the
switch on, `verified_by="human"` also needs a signature that verifies against
a keyring, the same mechanism Nestor uses for its own seals.

Deliberately its own module, not folded into `corpus.py`. `corpus.py` is
Jeles' pure core — stdlib only, no MCP, no network, and `tests/test_import_purity.py`
enforces it (design principle 2 in `README.md`). Nestor is a peer organ, not a
hard dependency: base `jeles` keeps zero runtime dependencies, and `nestor`
lives behind its own extra (`pip install "jeles[nestor]"`,
`nestor @ git+https://github.com/Die-Namic-Systems/Nestor@v0.2.0`, pinned to a tag
rather than a branch for the same reason every git dependency here is —
see README's "Prefer a released version" note). Every import of `nestor` in
this module is therefore lazy, inside the one function that needs it: `import
jeles.corpus` — and even `import jeles._nestor_seal` — must stay cheap and
optional-extra-free, so `jeles[mcp]` alone (no `[nestor]`) keeps working
exactly as before, just refusing the `human` rung instead of granting it.

What this module does NOT do: sign anything. Jeles never holds a private key
and never produces a seal — only a human, out-of-process, with `nestor`'s own
signing tools (or Nestor's own keyring-backed client), can do that. This
module only verifies, which is the same asymmetry Nestor's own README leans
on for its client-signed seals (Nestor#17): the party that can check a
signature is not thereby able to forge one.
"""

from __future__ import annotations

from typing import Any

__all__ = ["EVIDENCE_SCHEME", "describe", "verify_human_write"]

#: The only `evidence["scheme"]` this module currently understands. `evidence`
#: itself is a free-form dict (see `corpus.py`'s comment above `_KIND_RANK` —
#: "the mechanism is deliberately unnamed in the schema"); this is jeles'
#: choice of *its* scheme name, not a claim about the field in general.
EVIDENCE_SCHEME = "nestor-seal-v1"


def _normalize_source(question: str):
    """The question as Nestor signed it, or `None` if `nestor` is absent.

    **A seal covers the NORMALIZED source, not the raw text.** `nestor.memory`
    states the contract where the pair is built — "A seal signature covers
    (source_norm, target_text, verifier)" — and stores `source_text` and
    `source_norm` as two separate columns. Passing the raw question to
    `seal_is_valid` therefore checks a signature over a string nobody signed.

    That was this module's behaviour until 2026-08-29, and it meant a real,
    valid seal was refused: measured against a genuine ed25519 seal from the
    operator's own Nestor store, raw returned False and normalized returned
    True. So `corpus_put` could never accept a seal, and the `human` rung was
    unreachable by the one mechanism built to reach it. Nestor's own decision
    e7837efc ("Can a bridged nugget be sealed through the human surface?" —
    "Not correctly, today") recorded the symptom; this is the cause.

    The tests did not catch it because they signed the raw string and verified
    the raw string — self-consistent, and never once agreeing with Nestor.

    Normalization is a property of the *matcher* that sealed the pair, and
    `StringMatcher` is Nestor's default and what its own decision store uses.
    A pair sealed under a different matcher will not verify here, which is the
    honest outcome: this module cannot know which matcher signed, and guessing
    would mean accepting a signature over something other than what it checked.
    Imported rather than reimplemented for the reason this package has learned
    twice over — two copies of one rule drift, and the drift is silent.
    """
    try:
        from nestor.matcher import StringMatcher
    except ImportError:
        return None
    return StringMatcher().normalize(question)


def _import_nestor():
    """`nestor.signing`, or `None` if the `[nestor]` extra is not installed.

    Never raises. A missing extra is exactly as much a reason to refuse the
    `human` rung as a bad signature is — the caller does not get to tell the
    two apart from the outside, and neither should silently degrade into
    trusting the claim.
    """
    try:
        from nestor import signing
    except ImportError:
        return None
    return signing


def describe() -> dict[str, Any]:
    """Whether this instance could verify a seal at all — asking nothing of a
    caller and verifying nothing.

    Returns ``{scheme, installed, signing_enabled, ready, reason}``. ``ready``
    is True only when a real seal *could* be checked here; ``reason`` is
    ``"ok"`` then, and otherwise repeats — verbatim — the refusal
    :func:`verify_human_write` would give for the same condition, so the two
    can never disagree about why the `human` rung is out of reach.

    This exists because the only way to discover a missing extra or an
    unconfigured keyring used to be to *attempt a write* and read the rung it
    landed at. A caller learning "this instance cannot mint `human`" by writing
    a nugget it did not want has been told the truth by the most expensive
    route available.

    Never raises, and never names a key, a path, or any key material — the same
    rule :func:`verify_human_write` follows. ``signing_enabled`` is a boolean
    about configuration, not a hint about what the configuration is.
    """
    signing = _import_nestor()
    if signing is None:
        return {
            "scheme": EVIDENCE_SCHEME,
            "installed": False,
            "signing_enabled": False,
            "ready": False,
            "reason": 'nestor extra not installed (pip install "jeles[nestor]")',
        }

    try:
        enabled = bool(signing.signing_enabled())
    except Exception as exc:
        # Mirrors verify_human_write's own posture: an error asking whether we
        # can verify is a "no", reported, not an exception thrown at a caller
        # who only wanted a status.
        return {
            "scheme": EVIDENCE_SCHEME,
            "installed": True,
            "signing_enabled": False,
            "ready": False,
            "reason": f"signature check raised {type(exc).__name__}: {exc}",
        }

    return {
        "scheme": EVIDENCE_SCHEME,
        "installed": True,
        "signing_enabled": enabled,
        "ready": enabled,
        "reason": "ok" if enabled else "no NESTOR_SEAL_KEY or keyring configured on this instance",
    }


def verify_human_write(
    question: str,
    answer: str,
    verified_by: str,
    evidence: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Whether `evidence` proves `verified_by` is a real signer, not merely a
    string a tool caller typed into the `verified_by` argument.

    Returns ``(ok, reason)``. ``reason`` is always set — on success it is
    ``"ok"``, on failure it says which check failed, for `corpus_server` to
    log or surface without ever having to re-derive why. It never leaks key
    material: every failure path here names a *check*, not a key or a secret.

    ``ok`` is True only when every one of these holds:

    1. ``evidence`` is a dict carrying ``scheme == "nestor-seal-v1"`` and a
       non-empty string ``seal_sig``. Checked first, and independent of
       whether `nestor` happens to be installed, so a missing/malformed
       evidence dict is refused the same way in every environment.
    2. The `nestor` package is importable (the `[nestor]` extra is installed).
    3. **This instance actually has something configured to verify against**
       — a keyring (`NESTOR_KEYRING`) or a shared secret (`NESTOR_SEAL_KEY`).
       This is the one place this module deliberately does NOT delegate to
       `nestor.signing.seal_is_valid` for the answer, because that function's
       own "nothing configured" default is to warn once and then *accept*
       every signature (`nestor.signing.seal_is_valid`'s documented legacy
       behavior, there so an existing unsigned Nestor deployment does not
       break). That default exists for NESTOR'S OWN store, which was already
       trusting every `status="sealed"` row before HMAC seals existed — a
       backward-compatibility seam, not a security posture. A claim arriving
       at Jeles over a tool call has no such history to preserve; for it,
       "cannot verify" and "refuse" must be the same outcome, so this checks
       `signing_enabled()` first and refuses outright if it is False, never
       reaching the code path that would accept an unconfigured instance.
    4. ``nestor.signing.seal_is_valid(source_norm, answer, verified_by,
       seal_sig)`` returns True — an HMAC or ed25519 signature over exactly
       ``(source_norm, answer, verified_by)``, checked under the key
       registered to ``verified_by`` (or the shared key, with no keyring
       installed). Note the **first** field: the signature covers the question
       *normalized*, not as typed, and this module normalizes before checking
       (see `_normalize_source`, and the bug it records).
       Binding the signature to all three fields is what refuses a
       *transplanted* signature: a valid seal signed for a different
       question, a different answer, or a different name will not verify
       here, because Nestor's wire encoding (`nestor.signing._message`) folds
       all three into the signed bytes — changing any one of them produces
       different bytes, and a signature over different bytes does not verify.
    """
    # Shape checks first, and independent of whether `nestor` is even
    # installed: they are pure Python, and putting them ahead of the import
    # means "no evidence" and "malformed evidence" fail the same way whether
    # or not the `[nestor]` extra happens to be present in this environment.
    if not isinstance(evidence, dict):
        return False, "no evidence supplied"
    if evidence.get("scheme") != EVIDENCE_SCHEME:
        return False, f"evidence.scheme is not {EVIDENCE_SCHEME!r}"
    seal_sig = evidence.get("seal_sig")
    if not seal_sig or not isinstance(seal_sig, str):
        return False, "evidence.seal_sig missing or not a string"

    signing = _import_nestor()
    if signing is None:
        return False, 'nestor extra not installed (pip install "jeles[nestor]")'

    try:
        if not signing.signing_enabled():
            # See point 3 above: refuse here rather than letting
            # seal_is_valid's own unconfigured-instance default (accept,
            # with a warning) decide for us.
            return False, "no NESTOR_SEAL_KEY or keyring configured on this instance"
        source_norm = _normalize_source(question)
        if source_norm is None:
            return False, "nestor.matcher unavailable; cannot normalize the source"
        # The normalized source, never the raw question — see `_normalize_source`.
        ok = signing.seal_is_valid(source_norm, answer, verified_by, seal_sig)
    except Exception as exc:  # keyring/key errors are refusals, not crashes
        return False, f"signature check raised {type(exc).__name__}: {exc}"

    if not ok:
        return False, "signature does not verify under verified_by's key"
    return True, "ok"
