#!/usr/bin/env python3
"""The CI Python matrix, derived from pyproject.toml's classifiers.

`.github/workflows/tests.yml` used to type its matrix by hand, and the
comment beside it recorded the failure that produces: the classifiers
advertised 3.10-3.13 on PyPI while CI ran 3.10 and 3.12, so two of the four
versions an installer was told about had never been executed. A matrix typed
in two places drifts; a matrix derived from the one place the versions are
declared cannot.

This reads `Programming Language :: Python :: 3.N` classifiers and emits the
list every Linux leg runs, plus the floor and ceiling the Windows leg runs
(fleet plan decision 5, the CI floor). It refuses to guess: a pyproject with
no such classifiers exits non-zero rather than inventing a matrix from
`requires-python`, because the classifiers are what PyPI shows and the point
is to test exactly what is advertised.

    python tools/python_versions.py                     # print the outputs
    python tools/python_versions.py --github-output     # also append them to $GITHUB_OUTPUT

`tests/test_ci_floor.py` holds the workflow to this: the matrix expression
must be the derived output, and this derivation must equal the classifiers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 only; the runner uses 3.12
    import tomli as tomllib  # type: ignore[no-redef]

CLASSIFIER_RE = re.compile(r"^Programming Language :: Python :: (3\.\d+)$")


def classifier_minors(pyproject_text: str) -> list[str]:
    """Every `3.N` the classifiers declare, ascending, deduplicated. Raises
    `ValueError` when there are none — a matrix must never be guessed."""
    data = tomllib.loads(pyproject_text)
    found: set[str] = set()
    for classifier in data.get("project", {}).get("classifiers", []):
        m = CLASSIFIER_RE.match(classifier)
        if m:
            found.add(m.group(1))
    if not found:
        raise ValueError(
            "pyproject.toml declares no `Programming Language :: Python :: 3.N` "
            "classifier; declare the supported minors there — the matrix is derived "
            "from them, never guessed from requires-python"
        )
    return sorted(found, key=lambda v: tuple(int(part) for part in v.split(".")))


def outputs(versions: list[str]) -> dict[str, str]:
    """The workflow outputs: the full list, its floor and ceiling, and the
    two-element list the Windows leg runs."""
    return {
        "matrix": json.dumps(versions),
        "edges": json.dumps([versions[0], versions[-1]]),
        "floor": versions[0],
        "ceiling": versions[-1],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pyproject", default="pyproject.toml")
    ap.add_argument(
        "--github-output",
        action="store_true",
        help="also append `key=value` lines to the file $GITHUB_OUTPUT names",
    )
    args = ap.parse_args(argv)

    try:
        versions = classifier_minors(Path(args.pyproject).read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    lines = [f"{key}={value}" for key, value in outputs(versions).items()]
    print("\n".join(lines))
    if args.github_output:
        target = os.environ.get("GITHUB_OUTPUT")
        if not target:
            print("::error::--github-output given but $GITHUB_OUTPUT is not set", file=sys.stderr)
            return 1
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
