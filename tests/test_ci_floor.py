"""The fleet CI floor, held in place structurally (fleet plan decision 5).

`.github/workflows/tests.yml` is the one file that decides what a green PR
proves, and nothing checked its shape: the matrix was typed by hand and had
drifted from the classifiers (PyPI advertised 3.10-3.13; CI ran 3.10 and 3.12),
and the aggregate gate's `if: always()` plus its explicit skipped/cancelled
check are load-bearing lines a tidy-up could delete without a single test
noticing. This file reads the workflow and pyproject.toml and asserts the
floor:

* the Linux matrix is the output of `tools/python_versions.py`, and that tool's
  reading of the classifiers equals an independent one here;
* a Windows leg runs the floor and ceiling Pythons, from the same derivation;
* ruff is pinned to an exact release and `ruff format --check` runs beside
  `ruff check`;
* the aggregate `test` job needs every other job, runs `if: always()`, and
  rejects `failure`, `cancelled` and `skipped` by name.

CodeQL (python and actions) runs from GitHub's default setup for this
repository, not a workflow file — it appears as the `Analyze (...)` checks on
every PR — so there is nothing in the tree for this file to hold; the
workflow's header comment says so, and `test_the_workflow_says_where_codeql_runs`
keeps that sentence from quietly disappearing.

Every helper that reads the workflow has a planted twin: a synthetic workflow
carrying the violation (a hardcoded matrix, an unpinned ruff, a gate that
tolerates `skipped`, a gate missing a job) must be reported, or the real-tree
check above it has not been shown to check anything.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML needed to read the workflow")

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tools"))
python_versions = pytest.importorskip("python_versions")

_WORKFLOW = _REPO / ".github" / "workflows" / "tests.yml"
_PYPROJECT = _REPO / "pyproject.toml"

_GATE = "test"
_VERSIONS_JOB = "python-versions"
_LINUX_JOB = "test-matrix"
_WINDOWS_JOB = "windows"
_LINT_JOB = "lint"
_DERIVED_MATRIX = "${{ fromJSON(needs.python-versions.outputs.matrix) }}"
_DERIVED_EDGES = "${{ fromJSON(needs.python-versions.outputs.edges) }}"
_REJECTED = frozenset({"failure", "cancelled", "skipped"})

_CLASSIFIER_RE = re.compile(r'"Programming Language :: Python :: (3\.\d+)"')
_RUFF_PIN_RE = re.compile(r"\bruff==(\d+\.\d+\.\d+)\b")
_RUFF_INSTALL_RE = re.compile(r"pip install[^\n]*\bruff\b(?!==\d)")
_RESULT_RE = re.compile(r"contains\(needs\.\*\.result,\s*'([a-z]+)'\)")


# ── readers ──────────────────────────────────────────────────────────────────


def _jobs(workflow_text: str) -> dict:
    return yaml.safe_load(workflow_text)["jobs"]


def _runs(job: dict) -> list[str]:
    return [str(step.get("run", "")) for step in job.get("steps", [])]


def _classifier_minors(pyproject_text: str) -> list[str]:
    """An independent reading of the classifiers — a regex over the raw text,
    not the tool's TOML parse — so the real-tree check compares two routes to
    the same list rather than the tool against itself."""
    found = sorted(
        set(_CLASSIFIER_RE.findall(pyproject_text)),
        key=lambda v: tuple(int(part) for part in v.split(".")),
    )
    return found


def _matrix_expression(jobs: dict, job: str) -> str:
    return str(jobs[job]["strategy"]["matrix"]["python-version"])


def _derivation_runs_the_tool(jobs: dict) -> bool:
    return any("tools/python_versions.py" in run for run in _runs(jobs[_VERSIONS_JOB]))


def _ruff_pin(jobs: dict) -> str | None:
    for run in _runs(jobs[_LINT_JOB]):
        m = _RUFF_PIN_RE.search(run)
        if m:
            return m.group(1)
    return None


def _installs_ruff_unpinned(jobs: dict) -> bool:
    return any(_RUFF_INSTALL_RE.search(run) for run in _runs(jobs[_LINT_JOB]))


def _format_check_present(jobs: dict) -> bool:
    return any("ruff format --check" in run for run in _runs(jobs[_LINT_JOB]))


def _gate_missing_needs(jobs: dict) -> list[str]:
    needs = jobs[_GATE].get("needs") or []
    return [job for job in jobs if job != _GATE and job not in needs]


def _gate_runs_always(jobs: dict) -> bool:
    return str(jobs[_GATE].get("if", "")).strip() == "always()"


def _gate_rejected_results(jobs: dict) -> frozenset[str]:
    """The `needs.*.result` values the gate's failing step names — the
    conclusions it treats as failure. A gate that names only `failure` lets a
    skipped leg through, which is the silent state this file exists to
    forbid."""
    found: set[str] = set()
    for step in jobs[_GATE].get("steps", []):
        found.update(_RESULT_RE.findall(str(step.get("if", ""))))
    return frozenset(found)


# ── the real tree ────────────────────────────────────────────────────────────


def test_the_linux_matrix_is_derived_from_the_classifiers():
    jobs = _jobs(_WORKFLOW.read_text(encoding="utf-8"))
    assert _matrix_expression(jobs, _LINUX_JOB) == _DERIVED_MATRIX, (
        "the Linux matrix must be the derived output, never typed into the workflow"
    )
    assert _derivation_runs_the_tool(jobs)

    pyproject = _PYPROJECT.read_text(encoding="utf-8")
    declared = _classifier_minors(pyproject)
    assert declared, "pyproject.toml must declare `Programming Language :: Python :: 3.N`"
    assert python_versions.classifier_minors(pyproject) == declared


def test_the_windows_leg_runs_the_floor_and_ceiling():
    jobs = _jobs(_WORKFLOW.read_text(encoding="utf-8"))
    assert jobs[_WINDOWS_JOB]["runs-on"].startswith("windows")
    assert _matrix_expression(jobs, _WINDOWS_JOB) == _DERIVED_EDGES

    declared = _classifier_minors(_PYPROJECT.read_text(encoding="utf-8"))
    assert json.loads(python_versions.outputs(declared)["edges"]) == [declared[0], declared[-1]]


def test_ruff_is_pinned_to_an_exact_release_and_format_is_checked():
    jobs = _jobs(_WORKFLOW.read_text(encoding="utf-8"))
    assert _ruff_pin(jobs) is not None, "the lint job must `pip install ruff==X.Y.Z`"
    assert not _installs_ruff_unpinned(jobs)
    assert _format_check_present(jobs), "`ruff format --check` must run beside `ruff check`"


def test_the_aggregate_gate_needs_every_job_runs_always_and_rejects_non_success():
    jobs = _jobs(_WORKFLOW.read_text(encoding="utf-8"))
    assert _GATE in jobs, "branch protection requires a check named `test`"
    assert _gate_missing_needs(jobs) == [], "every job must feed the gate"
    assert _gate_runs_always(jobs), "`if: always()` is what lets the gate see a skipped leg"
    assert _gate_rejected_results(jobs) == _REJECTED


def _names_codeql_home(workflow_text: str) -> bool:
    """True when the workflow's own comments say CodeQL runs from default
    setup — the sentence a tidy-up would otherwise delete first."""
    return "default setup" in workflow_text and "CodeQL" in workflow_text


def test_the_workflow_says_where_codeql_runs():
    """No CodeQL workflow file exists here on purpose — default setup runs it —
    and the reason has to stay written down where the next person looks."""
    assert _names_codeql_home(_WORKFLOW.read_text(encoding="utf-8"))
    assert not (_REPO / ".github" / "workflows" / "codeql.yml").exists(), (
        "a workflow-based CodeQL config conflicts with default setup; pick one"
    )


def test_the_codeql_sentence_check_catches_a_planted_header_without_it():
    """Planted: a header that mentions CodeQL but not where it runs, and one
    that mentions neither, must both be reported."""
    assert not _names_codeql_home("# CodeQL runs somewhere\njobs: {}\n")
    assert not _names_codeql_home("# nothing about scanning\njobs: {}\n")
    assert _names_codeql_home("# CodeQL runs from default setup here\njobs: {}\n")


# ── the plants ───────────────────────────────────────────────────────────────


def _workflow(*, matrix: str, lint_install: str, gate_if: str, gate_needs: list[str]) -> str:
    """A minimal workflow carrying exactly the shape under test."""
    needs = ", ".join(gate_needs)
    return f"""
jobs:
  python-versions:
    runs-on: ubuntu-latest
    steps:
      - run: python tools/python_versions.py --github-output
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: {lint_install}
      - run: ruff check jeles tests
  test-matrix:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: {matrix}
  windows:
    runs-on: windows-latest
    strategy:
      matrix:
        python-version: {_DERIVED_EDGES!r}
  test:
    needs: [{needs}]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - if: {gate_if!r}
        run: exit 1
"""


_FULL_GATE_IF = (
    "${{ contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled') "
    "|| contains(needs.*.result, 'skipped') }}"
)
_ALL_JOBS = ["python-versions", "lint", "test-matrix", "windows"]
_TOLERANT_GATE_IF = (
    "${{ contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled') }}"
)


def test_the_matrix_check_catches_a_planted_hardcoded_matrix():
    planted = _jobs(
        _workflow(
            matrix='["3.10", "3.13"]',
            lint_install="pip install ruff==0.16.7 bandit",
            gate_if=_FULL_GATE_IF,
            gate_needs=_ALL_JOBS,
        )
    )
    assert _matrix_expression(planted, _LINUX_JOB) != _DERIVED_MATRIX
    assert _matrix_expression(planted, _WINDOWS_JOB) == _DERIVED_EDGES, "the control"


def test_the_derivation_catches_planted_classifiers():
    """The tool and the independent reader must agree on a planted set, and
    the tool must refuse a pyproject with none rather than guess."""
    planted = (
        '[project]\nname = "x"\nrequires-python = ">=3.9"\nclassifiers = [\n'
        '  "Programming Language :: Python :: 3",\n'
        '  "Programming Language :: Python :: 3.11",\n'
        '  "Programming Language :: Python :: 3.9",\n'
        '  "Programming Language :: Python :: 3.10",\n'
        '  "Programming Language :: Python :: 3.10",\n'
        "]\n"
    )
    assert python_versions.classifier_minors(planted) == ["3.9", "3.10", "3.11"]
    assert _classifier_minors(planted) == ["3.9", "3.10", "3.11"]
    assert json.loads(python_versions.outputs(["3.9", "3.10", "3.11"])["edges"]) == [
        "3.9",
        "3.11",
    ]

    bare = '[project]\nname = "x"\nrequires-python = ">=3.10"\n'
    with pytest.raises(ValueError, match="never guessed"):
        python_versions.classifier_minors(bare)
    assert _classifier_minors(bare) == []


def test_the_pin_check_catches_a_planted_unpinned_ruff():
    unpinned = _jobs(
        _workflow(
            matrix=repr(_DERIVED_MATRIX),
            lint_install="pip install ruff bandit",
            gate_if=_FULL_GATE_IF,
            gate_needs=_ALL_JOBS,
        )
    )
    assert _ruff_pin(unpinned) is None
    assert _installs_ruff_unpinned(unpinned)
    assert not _format_check_present(unpinned), "the planted lint job runs no format check"

    pinned = _jobs(
        _workflow(
            matrix=repr(_DERIVED_MATRIX),
            lint_install="pip install ruff==0.16.7 bandit",
            gate_if=_FULL_GATE_IF,
            gate_needs=_ALL_JOBS,
        )
    )
    assert _ruff_pin(pinned) == "0.16.7"
    assert not _installs_ruff_unpinned(pinned)


def test_the_gate_check_catches_a_planted_gate_that_tolerates_skipped():
    tolerant = _jobs(
        _workflow(
            matrix=repr(_DERIVED_MATRIX),
            lint_install="pip install ruff==0.16.7 bandit",
            gate_if=_TOLERANT_GATE_IF,
            gate_needs=_ALL_JOBS,
        )
    )
    assert _gate_rejected_results(tolerant) == frozenset({"failure", "cancelled"})
    assert "skipped" not in _gate_rejected_results(tolerant)
    assert _gate_runs_always(tolerant), "the control: `if: always()` is still there"

    forgetful = _jobs(
        _workflow(
            matrix=repr(_DERIVED_MATRIX),
            lint_install="pip install ruff==0.16.7 bandit",
            gate_if=_FULL_GATE_IF,
            gate_needs=["python-versions", "lint", "test-matrix"],
        )
    )
    assert _gate_missing_needs(forgetful) == ["windows"]

    strict = _jobs(
        _workflow(
            matrix=repr(_DERIVED_MATRIX),
            lint_install="pip install ruff==0.16.7 bandit",
            gate_if=_FULL_GATE_IF,
            gate_needs=_ALL_JOBS,
        )
    )
    assert _gate_rejected_results(strict) == _REJECTED
    assert _gate_missing_needs(strict) == []
