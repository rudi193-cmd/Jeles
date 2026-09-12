"""nugget_draft — turn an answered gap into a proposed nugget. Writes nothing.

The growth queue's last mile. `corpus.log_gap` records what the corpus was
asked and could not answer; the outward hops eventually find documents that
bear on it; and then someone has to sit down and write the question and answer
out. This drafts that pair from evidence already retrieved, so the person is
reviewing prose instead of composing it.

**The model may phrase. It may not source.** That division is the whole design,
and it is not a style preference — measured 2026-08-28, a local model with no
tool access, asked what colour Tokyo Night's accent is, answered "teal, based
on readily available information including multiple sources like design blogs
and community discussions". An invented fact wearing invented provenance. So
`sources` here is never parsed out of what the model wrote: it is assembled by
this module from the evidence records the caller passed in. A drafted nugget
cannot cite a document that was not retrieved, because the model's output is
not consulted about citations at all.

**And it drafts nothing from nothing.** With no evidence, this refuses rather
than asking the model to try — the persona's no-unsourced-output rule as
control flow instead of prose. A gap with no evidence behind it stays an open
gap, which is the correct state for a question nobody has answered yet.

Nothing here writes. The draft is a dict shaped for `corpus.put_nugget`, and
whoever passes it there is making that decision themselves. It carries
`verification_kind: "asserted"` and cannot carry anything else — the rung is
`corpus_put`'s to decide and, for `human`, a signature's (`jeles._nestor_seal`).
A draft is the bottom of the ladder by construction, which is what makes it
safe to generate one automatically.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

log = logging.getLogger("jeles.reactions.nugget_draft")

__all__ = ["INSUFFICIENT", "draft"]

#: What the model is told to say when the evidence does not answer the
#: question. Checked for as a prefix, so a model that adds a sentence of
#: explanation after it is still understood to be declining.
INSUFFICIENT = "INSUFFICIENT"

_DRAFT_SYSTEM = (
    "You write one short factual answer to a question, using ONLY the numbered "
    "documents provided. \n"
    "Rules:\n"
    "- Use only what the documents say. Do not add facts from memory.\n"
    "- Do not mention the documents, their numbers, or cite anything. Sources "
    "are recorded separately.\n"
    "- Two or three sentences at most.\n"
    f"- If the documents do not answer the question, reply with exactly: "
    f"{INSUFFICIENT}\n"
    "Answer only. No preamble, no heading."
)


def _evidence_block(evidence: Sequence[dict[str, Any]]) -> str:
    lines = []
    for i, e in enumerate(evidence, 1):
        title = (e.get("title") or "").strip()
        snippet = (e.get("snippet") or "").strip()[:400]
        lines.append(f"[{i}] {title}\n{snippet}".strip())
    return "\n\n".join(lines)


def draft(
    question: str,
    evidence: Sequence[dict[str, Any]],
    respond: Callable[..., str],
    *,
    drafted_by: str = "",
) -> dict[str, Any]:
    """Propose a nugget answering `question` from `evidence`. Writes nothing.

    `evidence` is retrieval output — anything with `title`, `url`, and
    optionally `snippet`: `corpus_institutional_search` hits,
    `source_trail.verify_claim` results, `corpus_web_search` hits.

    Returns either a proposal::

        {drafted: True, question, answer, sources, verified_by,
         verification_kind: "asserted", evidence_count}

    or a refusal::

        {drafted: False, reason, question}

    with ``reason`` one of ``no_evidence``, ``insufficient`` (the model read
    the documents and said they do not answer it), or ``model_failed``. Three
    reasons rather than one because they call for different next steps —
    retrieve more, retrieve better, or check the model is up — and collapsing
    them into a bare `False` hides which.

    ``sources`` is built here from the evidence's URLs. The model's text is
    never scanned for citations and never contributes one, so a draft cannot
    reference a document that was not retrieved. That is the one property this
    module exists to guarantee.

    ``verified_by`` records who is *claiming* it, defaulting to a name that
    says a machine drafted it. It is a claim, not a credential: `corpus_put`
    stamps `written_by` from the calling app and refuses the `human` rung to
    anything without a valid seal, so a drafter cannot promote itself by
    filling this field in confidently.
    """
    question = (question or "").strip()
    usable = [e for e in (evidence or []) if (e.get("title") or e.get("snippet"))]
    if not question or not usable:
        return {"drafted": False, "reason": "no_evidence", "question": question}

    try:
        raw = respond(
            _DRAFT_SYSTEM, [], f"QUESTION: {question}\n\nDOCUMENTS:\n{_evidence_block(usable)}"
        )
    except Exception as exc:
        log.warning("nugget draft failed: %s", exc)
        return {"drafted": False, "reason": "model_failed", "question": question}

    answer = (raw or "").strip()
    if not answer or answer.upper().startswith(INSUFFICIENT):
        return {"drafted": False, "reason": "insufficient", "question": question}

    return {
        "drafted": True,
        "question": question,
        "answer": answer,
        # Assembled here, never read out of `answer`.
        "sources": [u for u in (e.get("url") or "" for e in usable) if u],
        "verified_by": drafted_by or "machine draft (unreviewed)",
        "verification_kind": "asserted",
        "evidence_count": len(usable),
    }
