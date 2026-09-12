"""Vendored code is pinned to its canonical home, so a re-sync cannot be missed.

`tools/changelog_dedup.py` is vendored: its canonical home is forge-play/Forge's
`tools/changelog_dedup.py`. Only the module docstring is local; the *code body*
— everything from `from __future__ import annotations` to the end of the file —
is meant to be Forge's byte for byte, and nothing checked that it was. Measured
2026-09-12: this copy's body was 239 lines (sha256 `7313b99b…`) against Forge's
284 (sha256 `e3f31ef1…`), and the 45-line gap was two latent defects, not
tidying — see `tests/test_changelog_dedup.py`'s "two latent defects" block.
A pin turns the next drift from a discovery into a failing test.

The pin is a hash of the body, not a copy of it: a second copy of 284 lines
here would be one more thing to keep in step. The failure message says what to
do, in both directions — re-sync, or record a *named* local override here. A
silent fork is the one outcome this file exists to forbid.

The last test plants a one-byte change in a copy of the body and proves the
comparison catches it, so an edit to `_body()` or to the hashing that quietly
stopped comparing content could not pass this file by accident.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TOOL = _REPO / "tools" / "changelog_dedup.py"

_BODY_STARTS_AT = "from __future__ import annotations"

#: forge-play/Forge `tools/changelog_dedup.py`, body from `from __future__` to
#: EOF, measured 2026-09-12. Update this constant *only* alongside a body
#: re-synced from Forge, or record a named local override below.
FORGE_CHANGELOG_DEDUP_BODY_SHA256 = (
    "e3f31ef11105ae37c495c1745a94c6992ceb587c549cd016f24e83c778fd1320"
)

#: Deliberate, named divergences from Forge's body. Empty today: the body is
#: Forge's exactly. An entry here is `(what differs, why)` and comes with a
#: matching change to the constant above; that is what distinguishes an
#: override from a fork.
LOCAL_OVERRIDES: tuple[tuple[str, str], ...] = ()


def _body(source: str) -> str:
    """The vendored part: from the `from __future__` line to end of file. The
    module docstring above it is local and stays out of the hash."""
    return source[source.index(_BODY_STARTS_AT):]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_changelog_dedup_body_is_forges():
    actual = _sha256(_body(_TOOL.read_text(encoding="utf-8")))
    assert actual == FORGE_CHANGELOG_DEDUP_BODY_SHA256, (
        f"tools/changelog_dedup.py body sha256 is {actual[:16]}…, expected "
        f"{FORGE_CHANGELOG_DEDUP_BODY_SHA256[:16]}…: re-sync from forge-play/Forge "
        "tools/changelog_dedup.py (body from `from __future__` to EOF), or record a "
        "named local override here"
    )


def test_an_override_is_named_or_there_is_none():
    """Either the body is Forge's and there are no overrides, or every
    override says what and why. An empty *what* or *why* is a fork wearing an
    override's label."""
    for what, why in LOCAL_OVERRIDES:
        assert what.strip() and why.strip(), f"an override must name what and why: {(what, why)!r}"


def test_a_one_byte_change_to_the_body_is_caught(tmp_path):
    """Planted: the body with one byte flipped must not hash to the pin."""
    source = _TOOL.read_text(encoding="utf-8")
    body = _body(source)
    planted = body[:-2] + chr(ord(body[-2]) ^ 1) + body[-1:]
    assert planted != body and len(planted) == len(body)

    copy = tmp_path / "changelog_dedup.py"
    copy.write_text(source[: len(source) - len(body)] + planted, encoding="utf-8")

    assert _sha256(_body(copy.read_text(encoding="utf-8"))) != FORGE_CHANGELOG_DEDUP_BODY_SHA256
    assert _sha256(_body(source)) == FORGE_CHANGELOG_DEDUP_BODY_SHA256, (
        "the unplanted body must still match, or this plant proves nothing"
    )
