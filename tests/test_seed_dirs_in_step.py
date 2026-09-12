"""`corpus/seed/` and `jeles/seed/` are two copies of the same 74 files, and
nothing before this test pinned them together.

`jeles/seed/` is the one that ships: `pyproject.toml`'s
`[tool.hatch.build.targets.wheel]` packages `["jeles"]`, and hatchling includes
non-Python files under a packaged directory by default, so `jeles/seed/*.json`
rides the wheel and `jeles.seed_loader.SEED_DIR` points at it. `corpus/seed/`
is the authoring copy `corpus/compose.py` reads from and `corpus/README.md`
describes — outside `jeles/`, so it is not packaged. The two are meant to be
the same content; only one of them is ever installed. Without a test, a fix
applied to one (a corrected pair, a re-run adversarial pass) can drift from
the other silently, and whichever copy a reader happens to look at would lie
about what a fresh `pip install jeles` actually ships.

A scan that has never fired has not been shown to check anything: the last
test here plants a single changed byte in a copy of one file and asserts the
comparison catches it, so a future edit to the comparison itself that stopped
checking content (only names, say) would fail this file, not silently pass
everything.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_CORPUS_SEED = _REPO / "corpus" / "seed"
_JELES_SEED = _REPO / "jeles" / "seed"


def _seed_names(seed_dir: Path) -> set[str]:
    return {p.name for p in seed_dir.glob("*.json")}


def _diverging_files(a: Path, b: Path) -> list[str]:
    """Names present in both `a` and `b` whose bytes differ."""
    common = _seed_names(a) & _seed_names(b)
    return sorted(
        name for name in common
        if (a / name).read_bytes() != (b / name).read_bytes()
    )


def test_both_seed_dirs_have_the_same_74_files():
    assert len(_seed_names(_CORPUS_SEED)) == 74
    assert _seed_names(_CORPUS_SEED) == _seed_names(_JELES_SEED)


def test_every_file_is_byte_identical_between_the_two_seed_dirs():
    assert _diverging_files(_CORPUS_SEED, _JELES_SEED) == []


def test_a_one_byte_divergence_is_caught(tmp_path):
    """Plant a single changed byte in a copy and prove the comparison fires."""
    copy_dir = tmp_path / "jeles_seed_copy"
    shutil.copytree(_JELES_SEED, copy_dir)

    some_file = sorted(copy_dir.glob("*.json"))[0]
    original = some_file.read_bytes()
    planted = bytearray(original)
    planted[0] ^= 0xFF  # flip one byte; still the same length, still exists
    some_file.write_bytes(bytes(planted))

    assert _diverging_files(_CORPUS_SEED, copy_dir) == [some_file.name]
