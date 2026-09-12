"""seed_loader — install the bundled seed corpus, one rung per signature.

`pip install jeles` ships a starting corpus: 74 files of adversarially
reviewed question/answer pairs, in `jeles/seed/`, alongside the 84 host cards.
This loads them into a store.

**A seed pair is `asserted` unless a signature says otherwise.** The pairs were
produced by research agents running challenge / factcheck / steelman rounds —
real work, and still a machine's claim. That is the bottom rung, and shipping
them there is not a hedge: `ask_corpus` will not answer from an asserted
nugget, so an unsigned seed installs as *candidates* and a reader is told
plainly that nobody has checked them.

A pair carrying a valid Nestor seal lands at `human`. The seal is checked here,
on the installing machine, against a keyring that machine trusts — so "verified"
is something the recipient can confirm rather than something this package
asserts about itself. That distinction is the whole point of shipping seals
rather than a `verified` flag: a flag is a claim by the sender, and a signature
is evidence a stranger can check without trusting the sender at all.

Signing happens elsewhere and cannot happen here. `jeles._nestor_seal` verifies
and never signs; `nestor.keyring` refuses to produce a seal from a public-key
entry ("Signing happens where the private key lives"). So this module can grant
the `human` rung only where a person, out of process, has already earned it.

Not run on import, and not on first use. Seeding writes to a user's store, and
an unrequested write is exactly what `corpus_put`'s gate exists to refuse — so
it is a command someone types (`jeles-seed`), never a side effect of installing.

Related but different from `corpus/compose.py` in the repo, which is the
*authoring* tool: it takes arbitrary research JSON from a pipeline. This is the
*install* path for the batch that ships in the wheel.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jeles import corpus

from .commons import (
    seed_nugget_id,
    seed_pair_verification,
)

__all__ = ["SEED_DIR", "load_all", "load_file", "main", "seed_files"]

#: Where the bundled seed lives inside the installed package.
SEED_DIR = Path(__file__).resolve().parent / "seed"

#: Stamped on every seeded nugget's `written_by`, so a reader can tell a seeded
#: row from one a host wrote later. `verified_by` is a separate question — it
#: names whoever is *claiming* the answer, and for a sealed pair it must be the
#: signer, because that is the name the signature is bound to.
WRITTEN_BY = "jeles-seed"

#: What an unsigned pair claims. A research agent produced it; saying so is the
#: honest `verified_by`, and it pairs with `verification_kind: "asserted"`.
UNSIGNED_CLAIMANT = "research-agent"


def seed_files(seed_dir: Path | None = None) -> list[Path]:
    """Every bundled seed file, sorted. Empty if the seed was not installed."""
    directory = seed_dir or SEED_DIR
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def _verify(question: str, answer: str, verified_by: str, seal_sig: str) -> tuple[bool, str]:
    """Whether this pair's seal earns the `human` rung on THIS machine.

    Imported lazily so the loader works on a base install: without the
    `[nestor]` extra there is nothing to verify against, every pair lands at
    `asserted`, and that is a correct outcome rather than an error.
    """
    from jeles import _nestor_seal

    return _nestor_seal.verify_human_write(
        question,
        answer,
        verified_by,
        {"scheme": _nestor_seal.EVIDENCE_SCHEME, "seal_sig": seal_sig},
    )


def load_file(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Load one seed file. Returns per-rung counts and the refusal reasons.

    ``{domain, human, asserted, existing, errors, refusals}`` — `refusals`
    counts why seals did not verify, keyed by reason, because "no seal was
    offered" and "a seal was offered and did not check out" are different
    facts and only the second is a problem worth showing someone.
    """
    data = json.loads(path.read_text(encoding="utf-8"))

    # Not every file in the batch is a pair set. Two of the shipped 74 are
    # lists of adversarial *challenge* records — {id, severity, source_claim,
    # challenge, evidence} — which are evidence ABOUT the corpus rounds, not
    # entries in the corpus. Turning a challenge into a nugget would file an
    # objection as though it were an answer. Skipped, counted, and named, so
    # "not a pair set" never reads as "loaded nothing".
    if not isinstance(data, dict) or "pairs" not in data:
        return {
            "domain": path.stem,
            "human": 0,
            "machine": 0,
            "asserted": 0,
            "existing": 0,
            "errors": 0,
            "not_pairs": 0,
            "refusals": {},
            "skipped": "not a pair set",
        }

    domain = data.get("domain") or path.stem
    evidence_by_source: dict[str, list[str]] = {}
    for ev in data.get("evidence", []):
        evidence_by_source.setdefault(ev.get("pair_source", ""), []).append(ev.get("locator", ""))

    out: dict[str, Any] = {
        "domain": domain,
        "human": 0,
        "asserted": 0,
        "machine": 0,
        "existing": 0,
        "errors": 0,
        "not_pairs": 0,
        "refusals": {},
        "skipped": "",
    }

    for pair in data.get("pairs", []):
        question = (pair.get("source_text") or "").strip()
        answer = (pair.get("target_text") or "").strip()
        if not question or not answer:
            # Not malformed — a different artifact under the same key. 36 of
            # the shipped entries are the adversarial rounds' *reasoning*,
            # sharing `pairs` without the pair shape, in four distinct forms:
            #
            #   12  category, conventional_frame, direction, figure, steelman,
            #       what_this_does_NOT_claim
            #   12  argument, role_in_corpus, steelman_type, target, title
            #    8  argument, counterweight, source_profile, subject, thesis
            #    4  attack_argument, attack_thesis, defense_argument,
            #       defense_thesis, source_profile, subject
            #
            # Look at `what_this_does_NOT_claim`. Filed as a nugget, that field
            # becomes a verified answer stating what is explicitly *not* being
            # claimed — the corpus asserting the negation of its own
            # disclaimer, and under a human's signature if the batch is sealed.
            # These are reasoning about claims, not claims.
            #
            # Counted apart from `errors`, because "this row is broken" and
            # "this row is not a Q/A pair" call for different responses and
            # only the first is a defect.
            out["not_pairs"] += 1
            continue

        pair_sources = [u for u in evidence_by_source.get(question, []) if u]

        seal_sig = (pair.get("seal_sig") or "").strip()
        kind = "asserted"
        claimant = pair.get("verified_by") or UNSIGNED_CLAIMANT
        evidence: dict[str, Any] | None = None

        if seal_sig:
            claimant = (
                pair.get("verified_by") or data.get("verified_by") or UNSIGNED_CLAIMANT
            ).strip()
            ok, reason = _verify(question, answer, claimant, seal_sig)
            if ok:
                kind = "human"
            else:
                kind = "asserted"
                out["refusals"][reason] = out["refusals"].get(reason, 0) + 1
            evidence = {"scheme": "nestor-seal-v1", "seal_sig": seal_sig}
        else:
            kind, claimant = seed_pair_verification(
                domain=domain,
                pair=pair,
                data=data,
                sources=pair_sources,
                seal_sig="",
            )

        tags = [domain, "jeles-seed"]
        if kind == "machine":
            tags.append("commons")

        if dry_run:
            out[kind if kind in out else "asserted"] += 1
            continue

        try:
            result = corpus.put_nugget(
                question=question,
                answer=answer,
                sources=pair_sources,
                verified_by=claimant,
                verification_kind=kind,
                written_by=WRITTEN_BY,
                tags=tags,
                nugget_id=seed_nugget_id(domain, question),
                **({"evidence": evidence} if evidence else {}),
            )
            action = result.get("action", "unknown")
            if action == "created" or action == "updated":
                out[kind if kind in out else "asserted"] += 1
            else:
                out["existing"] += 1
        except Exception as exc:  # a bad row must not abandon the rest
            print(f"  WARN {domain}: {exc}", file=sys.stderr)
            out["errors"] += 1

    return out


def load_all(paths: list[Path] | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    """Load every bundled seed file (or the ones given). Returns totals."""
    files = paths if paths is not None else seed_files()
    totals: dict[str, Any] = {
        "files": len(files),
        "skipped": 0,
        "not_pairs": 0,
        "human": 0,
        "machine": 0,
        "asserted": 0,
        "existing": 0,
        "errors": 0,
        "refusals": {},
    }
    for path in files:
        r = load_file(path, dry_run=dry_run)
        if r.get("skipped"):
            totals["skipped"] += 1
        for key in ("human", "machine", "asserted", "existing", "errors", "not_pairs"):
            totals[key] += r[key]
        for reason, n in r["refusals"].items():
            totals["refusals"][reason] = totals["refusals"].get(reason, 0) + n
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jeles-seed",
        description="Load Jeles' bundled seed corpus into the store at "
        "$WILLOW_STORE_ROOT. Pairs carrying a Nestor seal that "
        "verifies on this machine land as 'verified'; everything "
        "else lands as an unchecked assertion.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be written, write nothing"
    )
    parser.add_argument(
        "files", nargs="*", type=Path, help="seed files to load (default: the bundled set)"
    )
    args = parser.parse_args(argv)

    files = args.files or seed_files()
    if not files:
        print(f"No seed files found in {SEED_DIR}.", file=sys.stderr)
        return 1

    totals = load_all(list(files), dry_run=args.dry_run)
    verb = "would load" if args.dry_run else "loaded"
    print(
        f"{verb} {totals['files']} file(s): "
        f"{totals['human']} verified, {totals['machine']} commons, "
        f"{totals['asserted']} asserted, "
        f"{totals['existing']} already present, {totals['errors']} error(s)"
        + (f", {totals['not_pairs']} not Q/A pairs" if totals["not_pairs"] else "")
        + (f", {totals['skipped']} file(s) not a pair set" if totals["skipped"] else "")
    )

    if totals["refusals"]:
        print("\nseals offered that did not verify here:")
        for reason, n in sorted(totals["refusals"].items(), key=lambda kv: -kv[1]):
            print(f"  {n:4}  {reason}")
        print(
            "\nThis is not necessarily a fault in the seed. A seal verifies "
            "only\nwhere the signer's key is trusted; without that keyring "
            "the pair is\nstill loaded, at the rung it can prove."
        )

    if not args.dry_run and totals["human"] == 0 and totals["machine"] == 0 and totals["asserted"]:
        print(
            "\nEverything landed as 'asserted', so corpus_ask will answer "
            "from none\nof it — asserted nuggets come back as candidates. "
            "That is the correct\nbehaviour for claims nobody has checked."
        )
    elif not args.dry_run and totals["machine"]:
        print(
            "\nCommons domains (core-*) with sources landed as 'machine' — "
            "corpus_ask serves them\nwithout a human seal. Novel domains "
            "remain asserted until verified."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
