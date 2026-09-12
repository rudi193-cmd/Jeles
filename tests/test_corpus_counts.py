"""The pair counts quoted in the two READMEs must match the seed tree.

`corpus/README.md` claimed "1369 pairs when composed" — a number nobody was
computing from anything, and wrong by exactly 401. The top-level `README.md`'s
"74 files, 968 question/answer pairs" turned out to be the correct composed
count, but nothing checked *that* either; it was correct by luck, not by test.

This file computes both numbers straight from `corpus/seed/` (74 JSON files,
byte-identical to `jeles/seed/` — see `tests/test_seed_dirs_in_step.py`) and
asserts each README's claim against the computation, rather than against each
other or against a second hand-typed literal here.

  raw entries    every record shipped in the tree, whether it lives in a
                 file's `pairs` list or (for the two adversarial-challenge
                 files) *is* the file's top-level list. 1028.

  composed count human + machine + asserted nuggets `jeles-seed --dry-run`
                 would actually write — i.e. real question/answer pairs, with
                 the 36 non-Q/A "reasoning" records and the 2 non-pair-set
                 files' contents excluded. 968 (149 commons + 819 asserted).

A scan that has never fired has not been shown to check anything: each helper
below that reads the tree or parses a README's claim has a test that plants a
wrong number, or a synthetic tree, and proves the helper reports it. The
README-parsing is in helpers rather than inline in the tests on purpose —
`tests/test_scans_fire.py` holds every scan-shaped helper to a plant in the
same file, and a regex run inline in a test body is a scan it cannot see.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jeles import seed_loader

_REPO = Path(__file__).resolve().parents[1]
_SEED_DIR = _REPO / "corpus" / "seed"
_CORPUS_README = _REPO / "corpus" / "README.md"
_TOP_README = _REPO / "README.md"

_RAW_RE = re.compile(r"\*\*(\d+)\*\*\s+raw pair-shaped entries")
_COMPOSED_RE = re.compile(r"composing to \*\*(\d+)\*\*\s+question/answer nuggets")
_TOP_RE = re.compile(r"\*\*(\d+) files, (\d+) question/answer pairs\*\*")


def _count_raw_entries(seed_dir: Path) -> int:
    """Every record in the tree: a file's `pairs` list, or the file itself."""
    total = 0
    for path in sorted(seed_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            total += len(data)
        elif isinstance(data, dict) and "pairs" in data:
            total += len(data["pairs"])
    return total


def _composed_count(seed_dir: Path) -> int:
    """Real question/answer nuggets `jeles-seed --dry-run` would write."""
    totals = seed_loader.load_all(seed_loader.seed_files(seed_dir), dry_run=True)
    return totals["human"] + totals["machine"] + totals["asserted"]


def _corpus_readme_claims(text: str) -> tuple[int, int]:
    """`(raw, composed)` as `corpus/README.md` claims them, parsed out of its
    own sentence rather than restated here."""
    return int(_RAW_RE.search(text).group(1)), int(_COMPOSED_RE.search(text).group(1))


def _top_readme_claim(text: str) -> tuple[int, int]:
    """`(files, pairs)` as the top-level `README.md` claims them."""
    files, pairs = _TOP_RE.search(text).groups()
    return int(files), int(pairs)


def test_the_tree_has_74_files():
    assert len(list(_SEED_DIR.glob("*.json"))) == 74


def test_corpus_readme_counts_match_the_tree():
    raw_claimed, composed_claimed = _corpus_readme_claims(
        _CORPUS_README.read_text(encoding="utf-8")
    )
    assert raw_claimed == _count_raw_entries(_SEED_DIR) == 1028
    assert composed_claimed == _composed_count(_SEED_DIR) == 968


def test_top_level_readme_count_matches_the_tree():
    files_claimed, pairs_claimed = _top_readme_claim(_TOP_README.read_text(encoding="utf-8"))
    assert files_claimed == len(list(_SEED_DIR.glob("*.json")))
    assert pairs_claimed == _composed_count(_SEED_DIR)


def test_a_stale_readme_count_is_caught_by_this_check():
    """Plant the corpus README's old, wrong number and prove it fails the check.

    This is the same check as `test_corpus_readme_counts_match_the_tree`, run
    against text carrying the stale "1369" claim it replaced. If this ever
    stopped catching the mismatch, that check would have stopped meaning
    anything too.
    """
    real_text = _CORPUS_README.read_text(encoding="utf-8")
    planted = _COMPOSED_RE.sub("composing to **1369** question/answer nuggets", real_text)
    assert planted != real_text  # the substitution actually landed

    _, planted_claim = _corpus_readme_claims(planted)
    assert planted_claim != _composed_count(_SEED_DIR)


def test_a_stale_top_level_readme_count_is_caught_by_this_check():
    """Planted: the top-level README's sentence with a wrong pair count must
    parse to a number the tree does not compute to."""
    real_text = _TOP_README.read_text(encoding="utf-8")
    planted = _TOP_RE.sub("**74 files, 1369 question/answer pairs**", real_text)
    assert planted != real_text

    files_claimed, pairs_claimed = _top_readme_claim(planted)
    assert files_claimed == 74
    assert pairs_claimed != _composed_count(_SEED_DIR)


def test_the_raw_entry_count_catches_each_planted_shape(tmp_path):
    """Planted: a tree with one file of each shape the counter claims to read
    — a top-level list (the adversarial-challenge shape), a dict with a
    `pairs` list, and a dict with no `pairs` at all, which must count for
    nothing rather than crash or count its other keys."""
    (tmp_path / "a_list.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    (tmp_path / "b_pairs.json").write_text(json.dumps({"pairs": [1, 2]}), encoding="utf-8")
    (tmp_path / "c_other.json").write_text(json.dumps({"meta": [1, 2, 3, 4]}), encoding="utf-8")
    assert _count_raw_entries(tmp_path) == 5
