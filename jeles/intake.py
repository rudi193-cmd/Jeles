"""Load intake JSON into the corpus, probe for gaps, resolve answered gaps.

Intake files live outside this package by default (willow-memory/jeles-intake/).
Run via ``scripts/jeles-intake.py`` from the Jeles checkout, or
``willow-memory/scripts/jeles-intake.py`` — both call :func:`main`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SKIP_LOAD = frozenset({"probe-questions.json"})
NOISE_GAP_QUESTION = "some unknown phrase"


def _jeles_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def find_intake_dir() -> Path:
    """Where probe-questions.json and *-local.json live."""
    override = os.environ.get("JELES_INTAKE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    root = _jeles_repo_root()
    for candidate in (
        root / "jeles-intake",
        root.parent.parent / "willow-memory" / "jeles-intake",
    ):
        if (candidate / "probe-questions.json").is_file():
            return candidate.resolve()
    return (root.parent.parent / "willow-memory" / "jeles-intake").resolve()


def bootstrap_fleet_env() -> None:
    """Set WILLOW_STORE_ROOT et al. from fleet.env when not already exported."""
    if os.environ.get("WILLOW_STORE_ROOT"):
        return
    root = _jeles_repo_root()
    candidates = []
    wh = os.environ.get("WILLOW_HOME", "").strip()
    if wh:
        candidates.append(Path(wh) / "fleet.env")
    candidates.extend(
        [
            root.parent.parent / "willow-memory" / ".willow" / "fleet.env",
            Path.home() / "github" / "willow-memory" / ".willow" / "fleet.env",
        ]
    )
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if line.startswith("umask "):
                continue
            if line.startswith("set "):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            val = os.path.expanduser(os.path.expandvars(val))
            os.environ.setdefault(key, val)
        return


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def intake_files(intake_dir: Path) -> list[Path]:
    return sorted(p for p in intake_dir.glob("*.json") if p.name not in SKIP_LOAD and p.is_file())


def load_file(path: Path, *, dry_run: bool, intake_dir: Path) -> dict[str, int]:
    from jeles import corpus

    data = _load_json(path)
    domain = data.get("domain", path.stem)
    counts = {"created": 0, "updated": 0, "existing": 0, "errors": 0, "commons": 0, "novel": 0}
    rel = path.relative_to(intake_dir) if path.is_relative_to(intake_dir) else path.name

    from .commons import intake_nugget_id, verification_for_intake

    for pair in data.get("pairs", []):
        question = pair["source_text"].strip()
        answer = pair["target_text"].strip()
        sources = [str(s) for s in (pair.get("sources") or [f"jeles-intake/{rel}"])]
        tags = [domain, "willow-intake"]
        if pair.get("reason"):
            tags.append("has-reason")

        kind, verified_by, extra_tags = verification_for_intake(
            domain=domain,
            pair=pair,
            data=data,
            sources=sources,
        )
        tags.extend(extra_tags)

        if dry_run:
            counts["created"] += 1
            if kind == "machine":
                counts["commons"] += 1
            else:
                counts["novel"] += 1
            continue

        try:
            result = corpus.put_nugget(
                question=question,
                answer=answer,
                sources=sources,
                verified_by=verified_by,
                verification_kind=kind,
                written_by="jeles-intake",
                tags=tags,
                nugget_id=intake_nugget_id(domain, question),
            )
            action = result.get("action", "unknown")
            if action in ("created", "updated"):
                counts["created"] += 1
                if kind == "machine":
                    counts["commons"] += 1
                else:
                    counts["novel"] += 1
            else:
                counts["existing"] += 1
        except Exception as exc:
            print(f"  error: {question[:60]!r}: {exc}", file=sys.stderr)
            counts["errors"] += 1

    return counts


def load_all(intake_dir: Path, *, dry_run: bool) -> dict[str, int]:
    totals = {"created": 0, "updated": 0, "existing": 0, "errors": 0}
    for path in intake_files(intake_dir):
        print(f"== load {path.name}")
        counts = load_file(path, dry_run=dry_run, intake_dir=intake_dir)
        print(
            f"  created={counts['created']} updated={counts.get('updated', 0)} "
            f"commons={counts.get('commons', 0)} novel={counts.get('novel', 0)} "
            f"existing={counts['existing']} errors={counts['errors']}"
        )
        for k in totals:
            totals[k] += counts[k]
    return totals


def probe(intake_dir: Path, *, dry_run: bool) -> dict[str, list[str]]:
    from jeles import corpus

    probe_file = intake_dir / "probe-questions.json"
    if not probe_file.is_file():
        print(f"missing {probe_file}", file=sys.stderr)
        return {"hits": [], "misses": []}

    data = _load_json(probe_file)
    hits: list[str] = []
    misses: list[str] = []

    for block in data.get("topics", []):
        topic = block.get("topic", "")
        for question in block.get("questions", []):
            q = question.strip()
            if not q:
                continue
            if dry_run:
                misses.append(q)
                continue
            result = corpus.ask_corpus(q)
            if result.get("found"):
                hits.append(q)
            else:
                misses.append(q)
                print(f"  GAP [{topic}] {q}")

    return {"hits": hits, "misses": misses}


def _list_gaps(limit: int = 200, *, include_resolved: bool = False) -> list[dict]:
    from jeles import corpus

    rows = corpus.list_gaps(limit=limit, include_resolved=include_resolved)
    return rows if isinstance(rows, list) else rows.get("items", [])


def _nugget_for_question(question: str) -> dict | None:
    from jeles import corpus

    candidates = corpus.search_nuggets(question, limit=5)
    return next((c for c in candidates if c.get("question") == question), None)


def resolve_gaps(*, dry_run: bool) -> int:
    from jeles import corpus

    if dry_run:
        return 0

    resolved = 0
    for row in _list_gaps(include_resolved=False):
        q = row.get("question", "")
        if q == NOISE_GAP_QUESTION:
            corpus.resolve_gap(row["_id"], resolved_by="jeles-intake")
            print(f"  resolved noise gap: {row['_id']}")
            resolved += 1
            continue

        match = _nugget_for_question(q)
        if not match or row.get("status") != "open":
            continue
        corpus.resolve_gap(
            row["_id"],
            resolved_by="jeles-intake",
            nugget_id=match.get("_id", ""),
        )
        print(f"  resolved gap: {q[:72]}…")
        resolved += 1
    return resolved


def corpus_list_open() -> list[dict]:
    items = _list_gaps(limit=100, include_resolved=False)
    open_items = [r for r in items if r.get("status") == "open"]
    open_items.sort(key=lambda r: (-int(r.get("asked_count") or 0), r.get("question", "")))
    return open_items


def main(argv: list[str] | None = None) -> int:
    bootstrap_fleet_env()
    intake_dir = find_intake_dir()
    print(f"intake: {intake_dir}")
    store = os.environ.get("WILLOW_STORE_ROOT", "(unset — default ~/.willow/store)")
    print(f"store:  {store}")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--load-only", action="store_true")
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args(argv)

    if not intake_files(intake_dir) and not args.probe_only:
        print(f"no intake files in {intake_dir}", file=sys.stderr)
        return 1

    if args.probe_only:
        print("== probe (corpus_ask → gaps on miss)")
        out = probe(intake_dir, dry_run=args.dry_run)
        print(f"  hits: {len(out['hits'])}, new gaps: {len(out['misses'])}")
        if not args.dry_run:
            open_gaps = corpus_list_open()
            print(f"\n== open Jeles gaps ({len(open_gaps)} total)")
            for row in open_gaps[:25]:
                print(f"  [{row.get('asked_count', 1):>3}x] {row.get('question', '')[:100]}")
        return 0

    totals = load_all(intake_dir, dry_run=args.dry_run)

    if not args.dry_run:
        print("== resolve gaps with loaded asserted answers")
        n = resolve_gaps(dry_run=False)
        print(f"  resolved {n} gap(s)")

    if args.load_only:
        return 0 if totals["errors"] == 0 else 1

    print("== probe (corpus_ask → gaps on miss)")
    out = probe(intake_dir, dry_run=args.dry_run)
    print(f"  hits: {len(out['hits'])}, gaps logged: {len(out['misses'])}")

    if not args.dry_run:
        open_gaps = corpus_list_open()
        print(f"\n== open Jeles gaps ({len(open_gaps)} total, research queue)")
        for row in open_gaps[:25]:
            print(f"  [{row.get('asked_count', 1):>3}x] {row.get('question', '')[:100]}")

    return 0 if totals["errors"] == 0 else 1
