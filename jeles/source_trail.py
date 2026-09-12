"""source_trail — single-source claim verification against the jeles registry.

Ported from willow-2.0's ``core/source_trail.py`` (b20: SRCTL1). Takes a block
of prose nobody has cited yet — a draft, a pasted article, a chat message —
finds its verifiable factual claims, and checks each one against
:mod:`jeles.sources`, the same source registry :mod:`jeles.institutional`
fans out to and whose citations :mod:`jeles.verify` corroborates.

Two-tier verification, upstream's own vocabulary:

* **academic** — peer-reviewed and institutional sources (OpenAlex, PubMed,
  the Library of Congress, …): everything in ``sources.SOURCES`` not listed
  in :data:`PRESS_SOURCES`.
* **press** — HTML/API adapters over trade press and specialty databases
  (Psychiatric Times, the FBI Vault, Ig Nobel, …): corroborating, not
  peer-reviewed, and marked as such in the output.

**How this differs from `jeles.verify`, which also checks claims against
sources.** The two sit on opposite sides of the same answer and read
different inputs — see `jeles.verify`'s own module docstring for the
`conflict_scan` half of this comparison:

* `verify.verify_claims` runs *after* an answer already carries numbered
  citations someone else retrieved and tagged with an institution. It counts
  *distinct institutions* per claim and asks "is this corroborated" — never
  touching the network itself.
* `source_trail.verify_text` runs *before* any citation exists. Given raw,
  uncited prose, it finds the claims itself and, for each one, searches
  `jeles.sources` live, keeping only the single highest-*ranked* hit. It
  asks "is anything backing this claim at all", not "how many institutions
  agree" — the two-institution bar `verify.py` applies would round a lone FBI
  Vault or Ig Nobel hit down to nothing, and this module exists for the case
  where that lone hit is exactly what a reader wants to see.

Claim extraction is an injected `llm_respond` callable —
``callable(system, history, input_text) -> str`` — the same seam
`jeles.verify.verify_claims` uses, and for the same reason: it keeps this
module itself off any model client or network stack. The only network this
module reaches is indirect, through `verify_claim`/`verify_text` calling
`jeles.sources.search`, which is already routed through `jeles._egress`;
nothing in this file opens a socket or an LLM connection directly. Upstream's
``core/llm_edge.respond`` carried a provider-agnostic router (Ollama → Gemini
→ Groq → fleet) baked into its import — porting that would have meant either
vendoring a fleet-specific router or adding an HTTP client dependency this
package promises not to have, so the call becomes a parameter here instead.

Public API:
    extract_claims(text, llm_respond) -> list[str]
    verify_claim(claim, sources=None, limit=2, judge=None) -> dict
    verify_text(text, llm_respond, sources=None, limit=2) -> dict

Output schema per claim:
    {claim, matched, title, url, date, source, tier, source_rank, overlap,
     institution, relevance}

`source_rank` is the publisher's rank, never the match's quality; `overlap` is
how much of the claim the returned document actually says. See `verify_claim`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from jeles import corpus as _corpus
from jeles import sources as _sources

log = logging.getLogger("jeles.source_trail")

__all__ = ["PRESS_SOURCES", "extract_claims", "verify_claim", "verify_text"]

# Registry keys treated as trade press / specialty databases rather than
# peer-reviewed or institutional sources, for the `tier` field below. Ported
# verbatim from upstream's `_PRESS_SOURCES` (core/source_trail.py). Not every
# key here has a live adapter in this package yet — `stat_news` and
# `medscape` were willow-2.0 scrapers this package never vendored — which is
# harmless: membership is only ever checked against source IDs
# `sources.search` actually returned, so an unregistered key simply never
# matches.
PRESS_SOURCES: frozenset[str] = frozenset(
    {
        "psychiatric_times",
        "stat_news",
        "medscape",
        "ig_nobel",
        "fbi_vault",
        "isfdb",
        "omdb",
    }
)

_EXTRACT_SYSTEM = (
    "Extract the distinct verifiable factual claims from the passage below. "
    "A verifiable claim is a specific, checkable statement: a statistic, a named event, "
    "a study result, a direct quote with attribution, or a stated fact. "
    "Return one claim per line. No bullets, numbers, or preamble. "
    "Skip vague opinions, metaphors, and normative judgements. "
    "Maximum 10 claims. If fewer than 3 verifiable claims exist, return only those."
)


def extract_claims(text: str, llm_respond: Callable[..., str]) -> list[str]:
    """Extract verifiable factual claims from `text` via the injected model call.

    `text` is truncated to 4000 characters before it reaches the model —
    upstream's own bound, kept as-is rather than re-derived, since it is
    sized to one edge-model call rather than to anything this module itself
    computes.

    Anything `llm_respond` raises is caught and turned into an empty list
    rather than propagated: a model outage should degrade "no claims found"
    for the caller, not take down whatever pipeline called this. `verify_text`
    already treats an empty claim list as its own short-circuit for the same
    reason a raise would be the wrong shape here — the two cases (nothing to
    extract; extraction failed) are deliberately indistinguishable from the
    caller's side, since a corpus of raw prose neither one should ever throw.
    """
    try:
        raw = llm_respond(_EXTRACT_SYSTEM, [], text[:4000])
        lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
        return lines[:10]
    except Exception as exc:
        log.warning("extract_claims failed: %s", exc)
        return []


_JUDGE_SYSTEM = (
    "You decide whether a document is about a claim. You will be given a CLAIM "
    "and a DOCUMENT (its title, and sometimes a short description).\n"
    "Answer with exactly one word:\n"
    "SUPPORTS - the document is about this claim's specific subject.\n"
    "UNRELATED - the document is about something else, even if it shares "
    "vocabulary with the claim.\n"
    "Sharing general words is not enough. A paper about function calling is "
    "UNRELATED to a claim about a specific named product unless it names that "
    "product. When unsure, answer UNRELATED.\n"
    "One word. No punctuation, no explanation."
)


def _ask_judge(claim: str, hit: dict[str, Any], judge: Callable[..., str]) -> str:
    """Put one claim and one document to the injected model. Never raises.

    Returns ``"supports"``, ``"unrelated"``, or ``"unjudged"`` — the last for
    a model that errored, timed out, or answered something this cannot read.
    Kept as its own value rather than folded into either verdict because
    "the judge said no" and "the judge never answered" are different facts,
    and collapsing them is how a model outage turns into a silent policy
    change. Same reasoning `institutional.py` applies to `failed` versus
    `skipped` versus `timed_out`.
    """
    doc = f"{hit.get('title') or ''}\n{(hit.get('snippet') or '')[:400]}".strip()
    if not doc:
        return "unjudged"
    try:
        raw = judge(_JUDGE_SYSTEM, [], f"CLAIM: {claim}\n\nDOCUMENT: {doc}")
    except Exception as exc:
        log.warning("relevance judge failed: %s", exc)
        return "unjudged"
    answer = (raw or "").strip().upper()
    if "UNRELATED" in answer:
        return "unrelated"
    if "SUPPORTS" in answer:
        return "supports"
    log.warning("relevance judge gave an unreadable verdict: %r", raw[:80])
    return "unjudged"


def _overlap(claim: str, hit: dict[str, Any]) -> float:
    """How much of the claim the returned document actually says.

    Deliberately reported and *not* enforced. `verify_claim` picks its winner
    by source rank alone, with no relevance check between the claim and the
    document that comes back — so a claim built from common academic
    vocabulary matches something in a high-ranked journal every time. Measured
    2026-08-28: "Gemma 4 ships with native function calling trained into the
    model" returned an Elsevier paper and was reported `matched: true` at
    0.9, a claim about a model that does not exist. The distinguishing tokens
    — "gemma", "4" — were exactly the ones the document lacked, which is what
    this number counts.

    Scored with `corpus._ask_tokens` on purpose rather than a second
    tokenizer: this package has twice shipped two modules disagreeing about
    what a word is (`sources.question_to_query` deleting lone capitals that
    `corpus` deliberately keeps; `search_zenodo` calling a dict an
    institution). One tokenizer, one rule.

    Coverage only — `corpus._confidence`'s symmetric F1 is the right shape for
    question-vs-question, where both sides are one sentence, and the wrong one
    here: a title is far longer than a claim, so precision collapses and every
    claim scores 0.00, true ones included. Measured across three runs before
    this was written.

    No threshold is applied because none has been earned. Separating three
    false positives from one true positive, over a fan-out that returned a
    different answer for the same CRISPR claim on three consecutive runs, is
    not an evaluation set. A caller that wants to gate on this must choose its
    own bar and say so.
    """
    asked = set(_corpus._ask_tokens(claim))
    if not asked:
        return 0.0
    doc = f"{hit.get('title') or ''} {hit.get('snippet') or ''}"
    return round(len(asked & set(_corpus._ask_tokens(doc))) / len(asked), 2)


def verify_claim(
    claim: str,
    sources: Sequence[str] | None = None,
    limit: int = 2,
    judge: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Verify a single claim against `jeles.sources`.

    `sources=None` (or an empty sequence — matching upstream's `if sources`
    check bug-for-bug) auto-routes via `sources.route_sources(claim)`;
    passing an explicit, non-empty list searches only those.

    Every hit `sources.search` returns is a candidate, and the **single**
    highest-ranked one wins — this is not a corroboration count (that is
    `jeles.verify`'s job). The rank is looked up per source ID from
    `sources._SOURCE_CONFIDENCE`, the same table `sources.py` itself ranks
    adapters by: primary institutions over peer-reviewed aggregators over
    community-maintained catalogs. An ID absent from that table (a press
    adapter added without an entry) falls back to 0.70 rather than being
    skipped, matching upstream.

    **The rank field is called `source_rank`, and it used to be called
    `confidence`.** That name was a lie of exactly the kind this package
    exists to prevent. It is a property of the *publisher*, not of the match:
    0.9 means "Elsevier is a highly-ranked source", never "this claim is 90%
    supported". Nothing here compares the claim to the document that comes
    back, so a claim assembled from common academic vocabulary matches
    something in a high-ranked journal every time and was reported at 0.9 —
    a number every consumer reads as endorsement, propagating up a pipeline
    and gaining credibility at each stage. Renaming it is not cosmetic: the
    field is consumed by things that decide what to believe.

    `overlap` is reported beside it as the missing half — how much of the
    claim the document actually says (see `_overlap`). It is **reported, not
    enforced**: no threshold on it has been earned. Counting shared words was
    not enough to separate the measured false positive from a true one (0.50
    against 0.57 on a live run), because a paper about function calling and a
    claim about a named product that does nothing else share real vocabulary.

    `judge` is the answer to that, and it is optional. Pass a callable with
    `extract_claims`' signature — ``judge(system, history, text) -> str`` —
    and the single winning hit is put to it as "is this document about this
    claim". The verdict lands in `relevance` as ``supports``, ``unrelated``,
    or ``unjudged``. Omit it and nothing changes: no model, no network, and
    `relevance` stays ``unjudged``, so the base install keeps its promise of
    zero runtime dependencies and this function stays as pure as it was.

    **The judge may only demote.** ``unrelated`` flips `matched` to False;
    nothing it can say will flip a False to True. That asymmetry is the whole
    safety argument for putting a model here at all: the confidence ladder
    stays decided by arithmetic, and a model that has been talked into
    agreeing with a claim cannot promote anything — it can only refuse. It is
    also where the errors actually are: the three failures measured on
    2026-08-28 were all false positives, none false negatives.

    A judge that errors, times out, or answers unreadably returns
    ``unjudged`` and changes nothing, so a model outage cannot quietly become
    a policy that rejects everything.

    Returns the output schema dict — `matched=False` and empty fields if
    nothing in the fan-out backs the claim.
    """
    routed = list(sources) if sources else _sources.route_sources(claim)

    raw = _sources.search(claim, routed, limit)
    hits = raw.get("results", {})

    best: dict[str, Any] | None = None
    best_conf = 0.0

    for source_id, source_hits in hits.items():
        conf = _sources._SOURCE_CONFIDENCE.get(source_id, 0.70)
        for hit in source_hits:
            if conf > best_conf:
                best_conf = conf
                best = {
                    "claim": claim,
                    "matched": True,
                    "title": (hit.get("title") or "").strip(),
                    "url": hit.get("url", ""),
                    "date": hit.get("date", ""),
                    "source": source_id,
                    "institution": hit.get("institution", source_id),
                    "tier": "press" if source_id in PRESS_SOURCES else "academic",
                    "source_rank": conf,
                    "overlap": _overlap(claim, hit),
                    "relevance": "unjudged",
                }

    if best:
        if judge is not None:
            best["relevance"] = _ask_judge(claim, best, judge)
            if best["relevance"] == "unrelated":
                # Demote only. The judge can take a match away; it can never
                # hand one out, and the fields describing what was found stay
                # put so "nothing came back" and "something came back and was
                # rejected" remain different answers.
                best["matched"] = False
        return best

    return {
        "claim": claim,
        "matched": False,
        "title": "",
        "url": "",
        "date": "",
        "source": "",
        "institution": "",
        "tier": "",
        "source_rank": 0.0,
        "overlap": 0.0,
        "relevance": "unjudged",
    }


def verify_text(
    text: str,
    llm_respond: Callable[..., str],
    sources: Sequence[str] | None = None,
    limit: int = 2,
) -> dict[str, Any]:
    """Extract factual claims from `text` and verify each against `jeles.sources`.

    Returns::

        {
          "claims":  [verify_claim(...) result, ...],
          "total":   int,
          "matched": int,
        }

    An empty extraction short-circuits to that shape with an added `"note"`
    key rather than calling `verify_claim` zero times silently — a caller
    reading `total == 0` should be able to tell "nothing to check" from "text
    with no output at all" without inspecting `claims`.
    """
    claims = extract_claims(text, llm_respond)
    if not claims:
        return {"claims": [], "total": 0, "matched": 0, "note": "No verifiable claims found."}

    results = [verify_claim(c, sources, limit) for c in claims]
    matched = sum(1 for r in results if r.get("matched"))
    return {"claims": results, "total": len(results), "matched": matched}
