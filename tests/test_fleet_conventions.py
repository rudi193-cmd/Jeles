"""This tree is held to the fleet's published conventions — read, not restated.

Fleet plan decision 4: every repo carries this file, and its rules come from
the one document the fleet publishes (`reconciler conventions --json`,
willow-reconciler), never from a second hand-typed copy here. A restated rule
is a fork waiting to drift; a read one changes when the document does.

**How the document is read here: vendored, pinned, and checked live when the
publisher is importable.** The alternative — `willow-reconciler` in the dev
extra and `from reconciler.conventions import conventions` — would make this
file skip on CI's no-extras leg, which installs only `pytest` and is exactly
the leg that proves the dependency-free install shape. A convention check that
skips on one of three legs is held to on two. So `tests/fleet_conventions.json`
is the document as willow-reconciler 0.6.0 emits it, byte for byte, and
`test_the_vendored_document_is_the_published_one` pins its sha256; a fresh
`reconciler conventions --json` must equal it (`test_the_vendored_document_
matches_the_live_publisher`) whenever the reconciler happens to be installed,
and that check skips — visibly, by name — when it is not. Re-vendor by
overwriting the file with the publisher's output and updating the pin and the
version it names, never by editing the JSON.

**What this repo's tree says, rule by rule** (measured 2026-09-12):

* `release-please.yml` arms auto-merge (`gh pr merge --auto`), so
  `pr-title.yml` is required; it exists.
* The config's hidden set is exactly the published one, and its un-hidden set
  is exactly the published release-cutting set.
* Both required reasoning comments are in `release-please-config.json` —
  in its package block, beside the settings they explain, which is where the
  fleet's consumer test looks. They used to sit at the file's top level (the
  values are unchanged; only the keys moved), and the port would have failed
  on that alone, so the move rides this PR rather than a widened helper.
* `contributing_must_name_test_command`: the repo had no CONTRIBUTING.md at
  all before this file, so the rule had nothing to read; CONTRIBUTING.md now
  names the command CI's no-extras job runs, and `TEST_COMMAND` here is that
  exact string.
* `required_when_pile_exists`: this repo keeps a numbered pile at
  `docs/ideas.md` (fleet plan Wave 3, E3-piles), so `.github/workflows/
  trailers.yml` is required and exists (E3-trailers). Before the pile existed
  this rule was vacuous here and the test said so; it now checks the real
  requirement.

Every real-tree check below has a planted twin that runs the same helper
against a synthetic tree or document carrying the violation, so a helper that
stopped checking could not pass this file by accident.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The published document, vendored. Source: willow-reconciler 0.6.0,
#: `reconciler conventions --json`, saved verbatim (indent=2, trailing newline).
DOCUMENT = REPO_ROOT / "tests" / "fleet_conventions.json"
DOCUMENT_SOURCE = "willow-reconciler 0.6.0 `reconciler conventions --json`"
DOCUMENT_SHA256 = "8c2ba122a7100141200d8c76ad086339f984446ab7e90dd9c27a092dbf7f5335"

RULES: dict = json.loads(DOCUMENT.read_text(encoding="utf-8"))

RELEASE_PLEASE = ".github/workflows/release-please.yml"
RELEASE_CONFIG = "release-please-config.json"
CONTRIBUTING = "CONTRIBUTING.md"
#: The numbered idea pile `reconciler run --repo ./ --doc docs/ideas.md` reads.
PILE = "docs/ideas.md"
ARMS_AUTOMERGE = "gh pr merge --auto"
#: The exact command CONTRIBUTING.md names — CI's no-extras job, verbatim.
TEST_COMMAND = "python -m pytest tests/ -q"


# ── the helpers the checks and the plants share ──────────────────────────────


def _arms_automerge(root: Path) -> bool:
    workflow = root / RELEASE_PLEASE
    return workflow.exists() and ARMS_AUTOMERGE in workflow.read_text(encoding="utf-8")


def _missing_when_armed(root: Path, required: list[str]) -> list[str]:
    if not _arms_automerge(root):
        return []
    return [f for f in required if not (root / f).exists()]


def _config_sections(config_text: str) -> list[dict]:
    return json.loads(config_text)["packages"]["."]["changelog-sections"]


def _config_hidden_types(config_text: str) -> set[str]:
    return {s["type"] for s in _config_sections(config_text) if s.get("hidden")}


def _config_visible_types(config_text: str) -> set[str]:
    return {s["type"] for s in _config_sections(config_text) if not s.get("hidden")}


def _config_missing_comments(config_text: str, required: list[str]) -> list[str]:
    package = json.loads(config_text)["packages"]["."]
    return [c for c in required if c not in package]


def _missing_when_pile_exists(root: Path, required: list[str]) -> list[str]:
    if not (root / PILE).exists():
        return []
    return [f for f in required if not (root / f).exists()]


def _names_test_command(contributing_text: str) -> bool:
    return TEST_COMMAND in contributing_text


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── the document itself ──────────────────────────────────────────────────────


def test_the_vendored_document_is_the_published_one():
    """The pin. A changed hash means either the publisher moved (re-vendor
    from it and update `DOCUMENT_SOURCE` and this constant together) or
    someone edited the JSON by hand, which is the fork this file forbids."""
    assert RULES["schema"] == "willow-fleet-conventions/1"
    assert _sha256(DOCUMENT) == DOCUMENT_SHA256, (
        f"tests/fleet_conventions.json no longer matches {DOCUMENT_SOURCE}: re-vendor it "
        "with `reconciler conventions --json > tests/fleet_conventions.json` and update "
        "DOCUMENT_SOURCE and DOCUMENT_SHA256 together — never edit the JSON by hand"
    )


def test_the_vendored_document_matches_the_live_publisher():
    """Equality against the real publisher, whenever it is installed. Skips
    by name otherwise: the pin above still holds, and a skip here is the
    visible reminder that the live check did not run on this leg."""
    conventions = pytest.importorskip(
        "reconciler.conventions", reason="willow-reconciler not installed on this leg"
    ).conventions
    assert conventions() == RULES, "the vendored document has drifted from the publisher's"


# ── the real tree ────────────────────────────────────────────────────────────


def test_pr_title_guard_is_present_wherever_automerge_is_armed():
    assert _arms_automerge(REPO_ROOT), "release-please.yml no longer arms auto-merge?"
    required = RULES["required_when_release_please_arms_automerge"]
    assert _missing_when_armed(REPO_ROOT, required) == []


def test_the_configs_hidden_set_equals_the_published_set():
    text = (REPO_ROOT / RELEASE_CONFIG).read_text(encoding="utf-8")
    assert _config_hidden_types(text) == set(RULES["hidden_types"])


def test_the_configs_visible_set_equals_the_published_release_cutting_set():
    """Every un-hidden type cuts a release on its own, so the set is closed
    and named — and it must be the fleet's set, not a superset of it."""
    text = (REPO_ROOT / RELEASE_CONFIG).read_text(encoding="utf-8")
    assert _config_visible_types(text) == set(RULES["release_cutting_types"])


def test_the_config_carries_every_required_reasoning_comment():
    text = (REPO_ROOT / RELEASE_CONFIG).read_text(encoding="utf-8")
    assert _config_missing_comments(text, RULES["required_config_comments"]) == []


def test_contributing_names_the_test_command():
    assert RULES["contributing_must_name_test_command"] is True
    contributing = REPO_ROOT / CONTRIBUTING
    assert contributing.exists(), "CONTRIBUTING.md is what the PR's Evidence line quotes from"
    assert _names_test_command(contributing.read_text(encoding="utf-8"))


def test_trailers_workflow_is_present_because_a_pile_exists():
    """`required_when_pile_exists` names `.github/workflows/trailers.yml`: a
    repo with a numbered pile runs `reconciler verify` in CI, because rule 2a
    asserts LANDED from a trailer ahead of every other signal and a dangling
    one is worse than none. This repo has the pile, so the gate is required."""
    assert (REPO_ROOT / PILE).exists(), "the pile moved? update PILE"
    assert _missing_when_pile_exists(REPO_ROOT, RULES["required_when_pile_exists"]) == []


# ── the plants ───────────────────────────────────────────────────────────────


def _tree(tmp_path: Path, label: str, *, arms: bool, files: tuple[str, ...] = ()) -> Path:
    root = tmp_path / label
    (root / ".github" / "workflows").mkdir(parents=True)
    body = "jobs:\n  release-please:\n    steps:\n      - run: |\n"
    body += f'          {ARMS_AUTOMERGE} "$pr"\n' if arms else "          gh pr list\n"
    (root / RELEASE_PLEASE).write_text(body, encoding="utf-8")
    for f in files:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text("# planted\n", encoding="utf-8")
    return root


def test_the_armed_tree_check_fires_on_a_planted_tree_missing_the_guard(tmp_path):
    required = RULES["required_when_release_please_arms_automerge"]
    assert _missing_when_armed(_tree(tmp_path, "bare", arms=True), required) == required
    guarded = _tree(tmp_path, "guarded", arms=True, files=tuple(required))
    assert _missing_when_armed(guarded, required) == []
    assert _missing_when_armed(_tree(tmp_path, "manual", arms=False), required) == []


def test_the_hidden_set_check_catches_a_planted_config_that_unhides_ci():
    planted = json.dumps(
        {
            "packages": {
                ".": {
                    "changelog-sections": [
                        {"type": "feat", "section": "Added"},
                        {"type": "docs", "section": "Docs", "hidden": True},
                        {"type": "test", "section": "Tests", "hidden": True},
                        {"type": "ci", "section": "CI"},
                        {"type": "chore", "section": "Chores", "hidden": True},
                    ],
                    "$comment-what-cuts-a-release": "kept",
                }
            }
        }
    )
    assert _config_hidden_types(planted) == {"chore", "docs", "test"}
    assert _config_visible_types(planted) == {"ci", "feat"}, "the unhidden ci: would cut releases"
    assert _config_missing_comments(planted, RULES["required_config_comments"]) == [
        "$comment-hidden-rule"
    ]


def test_the_pile_check_fires_on_a_planted_tree_with_a_pile_and_no_verify_gate(tmp_path):
    required = RULES["required_when_pile_exists"]
    with_pile = _tree(tmp_path, "pile", arms=False, files=(PILE,))
    assert _missing_when_pile_exists(with_pile, required) == required
    gated = _tree(tmp_path, "gated", arms=False, files=(PILE, *required))
    assert _missing_when_pile_exists(gated, required) == []


def test_the_contributing_check_catches_a_planted_contributing_without_the_command():
    assert not _names_test_command("# Contributing\n\nRun the tests before pushing.\n")
    assert _names_test_command(f"```sh\n{TEST_COMMAND}\n```\n")


def test_the_pin_catches_a_planted_one_byte_change_to_the_document(tmp_path):
    """Planted: the document with one byte changed must not hash to the pin,
    and the real file still must — or the pin proves nothing."""
    planted = tmp_path / "fleet_conventions.json"
    original = DOCUMENT.read_bytes()
    planted.write_bytes(original[:-2] + bytes([original[-2] ^ 1]) + original[-1:])
    assert _sha256(planted) != DOCUMENT_SHA256
    assert _sha256(DOCUMENT) == DOCUMENT_SHA256
