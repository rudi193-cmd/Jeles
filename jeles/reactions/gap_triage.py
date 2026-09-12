"""gap_triage — group the gap queue's near-duplicates, and write nothing.

`corpus.log_gap` merges repeat asks by `_gap_key` — the question's content
tokens, the adjacent pairs among them, and its short codes — so the *same*
question asked twice bumps `asked_count` instead of duplicating. What it
cannot merge is the same question asked *differently*: "what colour is the
Grove accent" and "Grove's accent colour?" tokenize to different keys and
become two gaps, each with a count that understates real demand. `list_gaps`
sorts by that count, so the queue's ordering degrades exactly where a question
is being asked often enough to matter and phrased freely enough to scatter.

This proposes groupings. It never merges, never writes, and never touches the
store — the same discipline `conflict_scan` follows, and for a sharper reason
here: merging is destructive. `log_gap`'s own docstring records what that cost
last time, when the newer phrasing overwrote the older one — "someone working
the queue answered the surviving phrasing and the other was gone, still
unanswered, its count folded into a number that then overstated demand for the
one left." A proposal a person reads cannot do that. A merge can.

**Why token overlap and not string similarity.** Nestor measured the obvious
approach and it failed: character difflib over question-shaped text floods,
because questions share a skeleton ("Should the X be Y?", "did a jeles gap
reach...") that scores high while saying nothing. Its all-pairs triage produced
674 false contradictions at a 0.45 bar before a `--calibrate` sweep moved the
knee to 0.55 (Nestor decision 94fb95ce, sealed). Scoring `corpus._ask_tokens`
sets instead sidesteps that class outright: the skeleton is stop words, and
stop words are already dropped before anything is compared.

**Why the symmetric score here, when `verify_claim` could not use it.**
`corpus._confidence`'s harmonic mean of precision and recall is built for
question-versus-question, where both sides are one sentence. Applied to a claim
against a document title it collapses — measured 0.00 on every claim, true ones
included. Here both sides really are questions, which is the case it was
written for, so it is the right instrument in the place it actually fits.
Rule 1 (an unmatched token is disqualifying) is deliberately *not* applied: it
is correct when deciding whether to answer, and far too strict when deciding
whether two questions are about the same thing, which is a question two people
will always phrase with at least one word between them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from jeles import corpus as _corpus

log = logging.getLogger("jeles.reactions.gap_triage")

__all__ = ["DEFAULT_MIN_SIMILARITY", "similarity", "triage"]

#: The bar two questions must clear to be proposed as the same question.
#:
#: **Checked, not calibrated** — the difference matters and is stated rather
#: than blurred. Run against twelve real gaps from willow-mcp's backlog on
#: 2026-08-28: the five `fleet-seams-probe` entries, which are one question
#: carrying a different correlation id each time, scored 0.857 against each
#: other and grouped; the worst-scoring pair among seven genuinely distinct
#: fleet questions scored 0.167. A five-fold gap, with this bar sitting in the
#: middle of it and nothing near either edge.
#:
#: That is evidence the *approach* works, not a calibrated threshold. Nestor
#: found its own knee with a `--calibrate` sweep over its whole corpus and
#: recorded the number with the sweep that produced it (decision 94fb95ce);
#: twelve hand-picked questions are not that. A host with a real gap corpus
#: should sweep and pass its own.
#:
#: The 0.167 is the number worth keeping. It is the same skeleton problem that
#: flooded Nestor's difflib triage with 674 false contradictions — two
#: questions both shaped "Should there be...?" — scoring almost nothing here,
#: because the skeleton is stop words and stop words are dropped before
#: anything is compared.
DEFAULT_MIN_SIMILARITY = 0.6

_JUDGE_SYSTEM = (
    "You decide whether two questions are asking the same thing.\n"
    "Answer with exactly one word:\n"
    "SAME - answering one would answer the other.\n"
    "DIFFERENT - they are about different things, even if they share words or "
    "sentence shape.\n"
    "Two questions about different subjects are DIFFERENT no matter how "
    "similarly they are worded. When unsure, answer DIFFERENT.\n"
    "One word. No punctuation, no explanation."
)


def similarity(a: str, b: str) -> float:
    """How much two questions are the same question, from 0.0 to 1.0.

    The harmonic mean of the two directions' overlap over `corpus._ask_tokens`
    sets, so a short question that happens to sit inside a long one scores low
    rather than matching it — the same asymmetry guard `corpus._confidence`
    applies, for the same reason.
    """
    ta, tb = set(_corpus._ask_tokens(a)), set(_corpus._ask_tokens(b))
    if not ta or not tb:
        return 0.0
    shared = len(ta & tb)
    if not shared:
        return 0.0
    precision, recall = shared / len(tb), shared / len(ta)
    return round(2 * precision * recall / (precision + recall), 3)


def _same_question(a: str, b: str, judge: Callable[..., str]) -> bool:
    """Ask the model whether a proposed pairing is real. Never raises.

    Only ever consulted about a pair the tokens *already* matched, and its only
    power is to veto. It cannot propose a grouping of its own, so a model
    talked into seeing a resemblance has no way to act on it — the same
    one-way rule `source_trail`'s relevance judge follows.

    A model that errors or answers unreadably returns True, leaving the
    token-based proposal exactly as it was. An outage must not silently become
    a policy, and the fallback is the behaviour the caller would have had with
    no judge at all.
    """
    try:
        raw = judge(_JUDGE_SYSTEM, [], f"QUESTION A: {a}\n\nQUESTION B: {b}")
    except Exception as exc:
        log.warning("gap triage judge failed: %s", exc)
        return True
    answer = (raw or "").strip().upper()
    if "DIFFERENT" in answer:
        return False
    if "SAME" in answer:
        return True
    log.warning("gap triage judge gave an unreadable verdict: %r", raw[:80])
    return True


def triage(
    gaps: Sequence[dict[str, Any]],
    *,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    judge: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Propose which gaps are the same question. Writes nothing.

    `gaps` is what `corpus.list_gaps()` returns, or anything with `question`
    and optionally `_id` and `asked_count`.

    Returns ``{groups, singletons, vetoed}``:

    * ``groups`` — proposed groupings of two or more, each
      ``{representative, members, asked_total, scores}``. The representative is
      the member with the highest `asked_count`, since that is the phrasing the
      queue has actually been asked in most, not the first one seen.
    * ``singletons`` — how many gaps grouped with nothing. A count, not a list:
      a caller wanting them has the input. Nestor's sealed decision f4dbb62b
      settled the same question for its own triage — suppress singletons in the
      human rendering, keep the full data for callers.
    * ``vetoed`` — pairs the tokens proposed and the judge refused. Kept rather
      than dropped so a rejected pairing is visible: a bar that silently
      discards its own near-misses cannot be calibrated, which is the whole
      reason Nestor's sweep was possible.

    ``asked_total`` is the sum of the group's counts and is the number worth
    sorting a review queue by — it is what demand for a question actually looks
    like once its phrasings stop being counted separately.

    Comparison is against each group's representative rather than every member,
    so groups cannot chain: A joining B and B joining C does not put A with C
    unless A matches the representative directly. Single-link clustering would
    walk a queue of loosely-related questions into one useless blob.
    """
    ordered = sorted(gaps, key=lambda g: int(g.get("asked_count") or 0), reverse=True)
    groups: list[dict[str, Any]] = []
    vetoed: list[dict[str, Any]] = []

    for gap in ordered:
        question = (gap.get("question") or "").strip()
        if not question:
            continue
        for group in groups:
            score = similarity(group["representative"], question)
            if score < min_similarity:
                continue
            if judge is not None and not _same_question(group["representative"], question, judge):
                vetoed.append(
                    {
                        "representative": group["representative"],
                        "question": question,
                        "score": score,
                    }
                )
                continue
            group["members"].append(gap)
            group["scores"].append(score)
            group["asked_total"] += int(gap.get("asked_count") or 0)
            break
        else:
            groups.append(
                {
                    "representative": question,
                    "members": [gap],
                    "scores": [1.0],
                    "asked_total": int(gap.get("asked_count") or 0),
                }
            )

    real = [g for g in groups if len(g["members"]) > 1]
    return {
        "groups": sorted(real, key=lambda g: g["asked_total"], reverse=True),
        "singletons": len(groups) - len(real),
        "vetoed": vetoed,
    }
