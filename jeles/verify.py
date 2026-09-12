"""verify — is this *claim* corroborated, or did the search merely return results?

Ported from willow-2.0's ``core/jeles_verify.py``, which ran on every synthesized
Ask-Jeles answer. The distinction it exists to draw: "the answer cites six
sources" and "every sentence in the answer is backed by two of them" are
different statements, and only the second is worth anything to a reader. The
synthesis step is exactly where a sentence nothing cited actually says gets
written, and a citation list at the bottom of the answer cannot see it — the
list is just as long either way.

So the answer is decomposed into atomic claims, each claim is attributed back to
the numbered sources the answer was built from, and each claim gets its own
verdict:

* ``corroborated``  — at least ``min_institutions`` distinguishable institutions
  back it.
* ``single_source`` — something backs it, but fewer than that many institutions
  can be told apart. See :func:`_verdict`; this is not only the one-institution
  case.
* ``unsupported``   — no cited source backs it at all.

``llm_respond`` is injected, exactly as :mod:`jeles.reactions.conflict_scan`'s
``searcher`` is. That is not only for testability: it is what keeps this module
off the egress path entirely. There is no URL here, no ``urllib.request``, and
therefore nothing for :mod:`jeles._egress` to guard — the host wires whatever
model it already has, and this module's contribution is the bookkeeping around
it. ``tests/test_verify.py`` asserts that absence rather than trusting this
paragraph, because a docstring describing a protection that is not running is
the failure shape this package keeps finding.

**How this differs from ``reactions.conflict_scan``, which also counts to two.**
Same bar, opposite ends of the pipeline, different evidence; neither subsumes the
other:

* conflict_scan runs *before* anything is written. Given one claim it goes and
  searches for what refutes or supersedes it, counts the distinct *sites* that
  came back, and proposes a corpus write. Its evidence is URLs it fetched itself.
* verify runs *after* an answer exists. It takes many claims — which it has to
  *find* first, because nobody listed them — and counts the distinct
  *institutions* among citations someone else already retrieved. It writes
  nothing; the output is a report.

What they genuinely share — the two-source bar, and the domain identity behind
"distinct" — is imported from :mod:`jeles._independence` rather than restated
here.

**The numbering hazard, which nothing here can detect.** The numbers the model
sees in ``sources_block`` must be the citations' own ``n`` values. Every hit
builder in this package numbers from **zero** (``enumerate(...)`` in
``institutional.search_institutional``, ``corpus.to_search_hit``'s caller, and
``corpus_server._web_hit``'s), so a block a host renders as "1. …, 2. …"
attributes every claim to the institution one slot over. Both sides are
well-formed and the report looks entirely normal. Render the block from ``n``.

**The labelling hazard, which nothing here can detect either.** ``_identity``
reads a citation's ``source`` field first and falls back to ``institution``
only when ``source`` is empty. That is exactly right for
``jeles.institutional``'s citations, where ``source`` *is* the institution
name. It is exactly wrong for ``jeles.sources``' raw ``_result()`` records:
there, ``source`` is the **registry key** (``"openalex"``, ``"pubmed"``, …,
a constant per adapter) and ``institution`` is the per-record institution —
the field that actually varies and the one this module exists to count. Feed
those records into :func:`verify_claims` unchanged and every claim they back
gets attributed to *adapters*, not institutions: two institutions reached
through one adapter read as one source (under-counted), and one institution
reached through two adapters reads as two (over-counted — the worse
direction, since it manufactures a false ``corroborated``). A host sitting
between ``jeles.sources.search`` and this module must re-label each citation
first — ``{**hit, "source": hit["institution"] or hit["source"]}`` — before
calling :func:`verify_claims`. See ``sources._result``'s docstring for the
other half of this.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from ._independence import MIN_INDEPENDENT_SOURCES, registrable_domain

__all__ = ["DEFAULT_MIN_INSTITUTIONS", "verify_claims"]

#: The corroboration bar, in this module's vocabulary. The number and the reason
#: it is *not* the constitution's Independent Witness live in `_independence`,
#: shared with `conflict_scan`, so the two cannot drift apart silently
#: (`tests/test_verify.py` pins them together).
DEFAULT_MIN_INSTITUTIONS = MIN_INDEPENDENT_SOURCES

_VERIFY_SYSTEM = (
    "You are a meticulous fact-checker. Below is an ANSWER and the numbered SOURCES "
    "it was built from. Break the ANSWER into atomic factual claims — each a single "
    "verifiable statement. For EACH claim output exactly one line in this format:\n"
    "CLAIM: <the claim> || SOURCES: <comma-separated source numbers that directly "
    "support it, or NONE>\n"
    "Rules:\n"
    "- A source supports a claim only if its excerpt directly states or clearly implies it.\n"
    "- Use only the provided source numbers; never invent numbers.\n"
    "- Output only CLAIM: lines — no preamble, no commentary."
)

# Matched on a word boundary so `Reclaim:` and `disclaimer:` stay prose. The
# boundary is also why the skip test and the cut are one regex rather than two:
# the original tested `"claim:" in line.lower()` and then cut with `re.split`,
# which agreed only while both stayed literal. Tightening either one alone leaves
# a line that passes the guard and finds nothing to split — an IndexError on
# `[1]`, raised out of a helper whose caller catches nothing.
_CLAIM_RE = re.compile(r"(?i)\bclaim\s*:")
#: `sources?` because a small model writing one number often writes "SOURCE:".
_SOURCES_RE = re.compile(r"(?i)\bsources?\s*:")
_DIGITS_RE = re.compile(r"\d+")
_WHITESPACE_RE = re.compile(r"\s+")


def _parse_claim_lines(raw: str) -> list[tuple[str, list[int]]]:
    """Parse ``CLAIM: … || SOURCES: 1,3`` lines into ``(claim, [source_nums])``.

    Deliberately tolerant of small-model drift, because the alternative to
    tolerance is *silent deletion*: a dropped claim is invisible in the report,
    where a mangled one is at least legible and countable. Handled — a missing
    ``SOURCES`` clause (no sources), ``NONE`` or an empty clause (no sources),
    numbers written ``[1]`` or ``1.`` (digits extracted), the same number twice
    (counted once), and preamble or trailing commentary lines (skipped).

    A missing ``||`` is handled too, and separately, because the separator is
    the single most droppable token in the format — the label survives it. When
    there is no ``||`` the ``SOURCES`` label itself is the cut. When there *is*
    one, the label is only looked for after it, so a claim whose own text reads
    "the paper lists its sources: three" is not truncated at its own word.
    """
    out: list[tuple[str, list[int]]] = []
    for line in (raw or "").splitlines():
        head = _CLAIM_RE.search(line)
        if head is None:
            continue
        body = line[head.end() :]
        claim_part, separator, tail = body.partition("||")
        if not separator:
            marker = _SOURCES_RE.search(body)
            if marker is None:
                claim_part, tail = body, ""
            else:
                claim_part, tail = body[: marker.start()], body[marker.start() :]
        claim = claim_part.strip(" \t\r\n-•|")
        if not claim:
            continue
        marker = _SOURCES_RE.search(tail)
        src_text = tail[marker.end() :] if marker else ""
        nums: list[int] = []
        for token in _DIGITS_RE.findall(src_text):
            n = int(token)
            if n not in nums:
                nums.append(n)
        out.append((claim, nums))
    return out


def _fold(label: str) -> str:
    """The comparison key for an institution name.

    Case- and whitespace-folded, because the raw strings are what upstream APIs
    happened to write and the original compared them verbatim. Two records from
    one institution labelled ``"NASA"`` and ``"nasa "`` were therefore two
    institutions, which is the exact shape of a *false* corroboration — the one
    direction of error this whole module exists to prevent.

    What this still does not close: label drift within an institution, e.g.
    ``"PubMed"`` and ``"PubMed Central"``, both NIH. Closing that needs a
    registry of institutions rather than a string rule, and `cards.py` is where
    such a registry would live. Named here rather than left implied.
    """
    return _WHITESPACE_RE.sub(" ", label).strip().casefold()


def _identity(citation: dict[str, Any]) -> tuple[str, str]:
    """A citation's ``(comparison key, display name)``.

    The label first, since that is the institution the reader is being asked to
    trust. Its *site* is the fallback rather than the primary because in this
    package a citation URL is frequently the publisher's or a DOI resolver's,
    not the queried institution's — 18 of the source adapters build a
    ``doi.org`` link — so leading with the domain would fold unrelated
    publishers into one "source".

    An unlabelled citation used to yield nothing at all, and the consequence was
    not neutral: :func:`verify_claims` dropped it, so a claim backed by two real
    records whose ``institution`` field was empty was reported ``unsupported``,
    the strongest available denial. Empty institution strings are not
    hypothetical here — ``tests/test_sources.py`` pins that an absent
    institution stays empty rather than being invented.

    This precedence — ``source`` over ``institution`` — is a contract, pinned by
    every citation in this module's tests (``{"n": 1, "source": "NASA"}``: the
    label *is* the institution there). It matches ``jeles.institutional``'s
    citations for the same reason. It does **not** match a raw
    ``jeles.sources._result()`` record, whose ``source`` is a constant registry
    key rather than an institution — see the module docstring's "labelling
    hazard" and ``sources._result``'s docstring. Inverting this precedence to
    accommodate that other shape would break every citation this module already
    documents itself against; the fix belongs on the caller, re-labelling
    ``sources.py`` output before it reaches here.
    """
    label = str(citation.get("source") or citation.get("institution") or "").strip()
    key = _fold(label)
    if key:
        return key, label
    domain = registrable_domain(str(citation.get("url") or ""))
    return domain, domain


def _verdict(named: Sequence[str], supported: bool, min_institutions: int) -> str:
    """The claim's verdict, given the institutions named and whether anything
    cited backs it at all.

    ``single_source`` covers more than "exactly one institution": it is *backed,
    but by fewer than the bar's worth of institutions we can tell apart*, which
    also catches citations too anonymous to distinguish. Under-reporting is the
    survivable direction — it asks a human to look — whereas a false
    ``corroborated`` is the report asserting something it did not check.
    """
    if len(named) >= min_institutions:
        return "corroborated"
    return "single_source" if supported else "unsupported"


def _summary(claims: Sequence[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    verdicts = [c["verdict"] for c in claims]
    out: dict[str, Any] = {
        "total": len(verdicts),
        "corroborated": verdicts.count("corroborated"),
        "single_source": verdicts.count("single_source"),
        "unsupported": verdicts.count("unsupported"),
    }
    out.update(extra)
    return out


def verify_claims(
    answer: str,
    sources_block: str,
    citations: Sequence[dict[str, Any]],
    llm_respond: Callable[..., str],
    *,
    min_institutions: int = DEFAULT_MIN_INSTITUTIONS,
) -> dict[str, Any]:
    """Verify each atomic claim in ``answer`` against the numbered ``sources_block``.

    ``citations`` are the answer's citation records, each ``{"n": int, "source"
    or "institution": str, "url": str, …}`` — this package's standard citation
    shape. ``n`` maps a supporting source number back to an institution; see the
    module docstring on why that numbering must match the block.

    ``source``, if present, is read as the institution name — see
    :func:`_identity`. That holds for ``jeles.institutional``'s citations
    as-is. It does **not** hold for a ``jeles.sources._result()`` record passed
    through unchanged, whose ``source`` is a registry key, not an institution:
    a host built on ``jeles.sources.search`` must re-label each citation —
    ``{**hit, "source": hit["institution"] or hit["source"]}`` — before calling
    this function, or corroboration ends up counted over adapters instead of
    institutions. See the module docstring's "labelling hazard".

    ``llm_respond`` is ``callable(system, history, user) -> str``. It is called
    once. Anything it raises is caught and reported in the summary's ``error``
    key: a fact-check that could not run is a *missing* verdict, and turning
    that into an exception out of the caller's answer path would trade a degraded
    answer for no answer.

    Returns ``{"claims": [{claim, sources, institutions, verdict}], "summary":
    {total, corroborated, single_source, unsupported}}``.

    An empty answer, or one with no citations, short-circuits to an empty report
    rather than calling the model: with nothing to attribute to, every claim is
    ``unsupported`` by construction, and paying for a model call to be told so
    informs nobody.
    """
    if not answer or not citations:
        return {"claims": [], "summary": _summary([])}

    key_by_n: dict[int, str] = {}
    display_by_key: dict[str, str] = {}
    for citation in citations:
        n = citation.get("n")
        if not isinstance(n, int):
            continue
        key, display = _identity(citation)
        key_by_n[n] = key
        if key:
            display_by_key.setdefault(key, display)

    try:
        raw = llm_respond(_VERIFY_SYSTEM, [], f"ANSWER:\n{answer}\n\nSOURCES:\n{sources_block}")
    except Exception as e:
        return {"claims": [], "summary": _summary([], error=str(e))}

    claims: list[dict[str, Any]] = []
    for text, nums in _parse_claim_lines(raw):
        # A number the citation list does not have is dropped, not counted. A
        # model that invents "7" for a six-source block is the ordinary case,
        # and an invented number that counted would manufacture corroboration
        # out of nothing.
        valid_nums = [n for n in nums if n in key_by_n]
        keys = {key_by_n[n] for n in valid_nums}
        named = sorted(display_by_key[k] for k in keys if k)
        claims.append(
            {
                "claim": text,
                "sources": valid_nums,
                "institutions": named,
                "verdict": _verdict(named, bool(valid_nums), min_institutions),
            }
        )

    return {"claims": claims, "summary": _summary(claims)}
