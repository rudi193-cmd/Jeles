"""Commons vs novel knowledge — classification for intake and seed loading.

Commons (π, Newton's laws, core "way things work" domains) land at the
``machine`` rung when citable sources exist — no human seal required.
Novel synthesis (operator research, institutional genealogy) stays ``asserted``
until a human verifies or conflict_scan corroborates.
"""

from __future__ import annotations

import hashlib
from typing import Any

COMMONS_DOMAIN_PREFIXES = ("core-",)
COMMONS_VERIFIED_BY = "commons:bootstrap"
INTAKE_VERIFIED_BY = "operator-seat"
NOVEL_VERIFIED_BY = "research-agent"

__all__ = [
    "COMMONS_DOMAIN_PREFIXES",
    "COMMONS_VERIFIED_BY",
    "classification_for_pair",
    "domain_is_commons",
    "file_classification",
    "has_citable_source",
    "intake_nugget_id",
    "is_commons_domain",
    "pair_classification",
    "promote_commons_in_store",
    "seed_nugget_id",
    "seed_pair_verification",
    "verification_for_intake",
]


def file_classification(data: dict[str, Any]) -> str:
    """File-level default: ``commons`` or ``novel`` (default ``novel``)."""
    raw = (data.get("classification") or "novel").strip().lower()
    return raw if raw in ("commons", "novel") else "novel"


def pair_classification(pair: dict[str, Any], default: str) -> str:
    raw = (pair.get("classification") or default).strip().lower()
    return raw if raw in ("commons", "novel") else default


def is_commons_domain(domain: str) -> bool:
    d = (domain or "").strip().lower()
    return any(d.startswith(prefix) for prefix in COMMONS_DOMAIN_PREFIXES)


def domain_is_commons(domain: str, data: dict[str, Any] | None = None) -> bool:
    if data is not None and file_classification(data) == "commons":
        return True
    return is_commons_domain(domain)


def has_citable_source(sources: list[str]) -> bool:
    if not sources:
        return False
    for s in sources:
        text = str(s).strip()
        if not text:
            continue
        if text.startswith(("http://", "https://")):
            return True
        if text.startswith("jeles-intake/"):
            continue
        return True
    return False


def classification_for_pair(
    *,
    domain: str,
    pair: dict[str, Any],
    data: dict[str, Any] | None,
    sources: list[str],
) -> str:
    pair_cls = (pair.get("classification") or "").strip().lower()
    file_cls = file_classification(data or {})
    if pair_cls == "novel":
        return "novel"
    if pair_cls == "commons":
        return "commons" if has_citable_source(sources) else "novel"
    if file_cls == "novel" and not is_commons_domain(domain):
        return "novel"
    if not has_citable_source(sources):
        return "novel"
    if is_commons_domain(domain) or file_cls == "commons":
        return "commons"
    return "novel"


def seed_pair_verification(
    *,
    domain: str,
    pair: dict[str, Any],
    data: dict[str, Any],
    sources: list[str],
    seal_sig: str,
) -> tuple[str, str]:
    """Return (verification_kind, verified_by) for a seed pair."""
    if seal_sig:
        claimant = (pair.get("verified_by") or data.get("verified_by") or "").strip()
        return "human", claimant or NOVEL_VERIFIED_BY
    bucket = classification_for_pair(
        domain=domain,
        pair=pair,
        data=data,
        sources=sources,
    )
    if bucket == "commons" and domain_is_commons(domain, data):
        return "machine", COMMONS_VERIFIED_BY
    return "asserted", pair.get("verified_by") or NOVEL_VERIFIED_BY


def verification_for_intake(
    *,
    domain: str,
    pair: dict[str, Any],
    data: dict[str, Any],
    sources: list[str],
) -> tuple[str, str, list[str]]:
    """Return (verification_kind, verified_by, extra_tags)."""
    bucket = classification_for_pair(
        domain=domain,
        pair=pair,
        data=data,
        sources=sources,
    )
    tags: list[str] = []
    if bucket == "commons":
        tags.append("commons")
        return "machine", COMMONS_VERIFIED_BY, tags
    tags.append("novel")
    return "asserted", INTAKE_VERIFIED_BY, tags


def seed_nugget_id(domain: str, question: str) -> str:
    digest = hashlib.sha256(f"jeles-seed\0{domain}\0{question}".encode()).hexdigest()
    return f"s{digest[:10]}"


def intake_nugget_id(domain: str, question: str) -> str:
    digest = hashlib.sha256(f"jeles-intake\0{domain}\0{question}".encode()).hexdigest()
    return f"i{digest[:10]}"


def promote_commons_in_store(*, dry_run: bool = False) -> dict[str, int]:
    """Upgrade asserted nuggets in commons domains to machine when sourced."""
    from jeles import corpus

    counts = {"promoted": 0, "skipped": 0, "errors": 0}
    for nugget in corpus.list_nuggets(limit=20000):
        kind = nugget.get("verification_kind") or "human"
        if kind != "asserted":
            counts["skipped"] += 1
            continue
        tags = list(nugget.get("tags") or [])
        domain = tags[0] if tags else ""
        if not is_commons_domain(domain):
            counts["skipped"] += 1
            continue
        sources = [str(s) for s in (nugget.get("sources") or [])]
        if not has_citable_source(sources):
            counts["skipped"] += 1
            continue
        question = (nugget.get("question") or "").strip()
        answer = (nugget.get("answer") or "").strip()
        if not question or not answer:
            counts["skipped"] += 1
            continue
        if dry_run:
            counts["promoted"] += 1
            continue
        if "commons" not in tags:
            tags.append("commons")
        result = corpus.put_nugget(
            question=question,
            answer=answer,
            sources=sources,
            verified_by=COMMONS_VERIFIED_BY,
            tags=tags,
            nugget_id=nugget.get("_id", ""),
            verification_kind="machine",
            written_by=nugget.get("written_by") or "jeles-seed",
            evidence={
                **dict(nugget.get("evidence") or {}),
                "commons_promote": {"method": "promote_commons_in_store"},
            },
        )
        if result.get("error"):
            counts["errors"] += 1
        else:
            counts["promoted"] += 1
    return counts
