"""conflict_scan — the prior-art / conflict reaction (v1).

The behavior this scripts is the one hand-run in the 2026-07-24 design session:
given a design *claim*, search the web not for what's *similar* (the mirror —
you always find a match and feel original) but for what *supersedes or refutes*
it, and hand back what you found. This is the reaction ``orchestrator.md`` was
prose about and never enforced; here it is a script that ends in a proposed
FRANK line.

Three disciplines, all deterministic, all testable network-free:

1. **Conflict-biased query framing.** ``frame_queries`` weights the queries
   toward supersession/rivalry/refutation, not similarity. The mirror query is
   kept (one baseline) but outnumbered.

2. **Two independent sources.** A finding is *corroborated* only when at least
   ``min_sources`` **distinct registrable domains** back it. Two hits from the
   same site do not corroborate — the independent-*source* rule (a cheap prior-
   art heuristic, deliberately weaker than, and named apart from, the
   constitution's Independent Witness, which requires failure-mode divergence).
   Corroborated → propose a nugget; single-source → propose a *gap* (contested;
   the corpus records that it looked and couldn't yet verify).

   The same bar is applied at the far end of the pipeline by :mod:`jeles.verify`,
   which asks it of an answer that already exists: not "did a search find two
   sites for this claim" but "does every claim in this answer have two
   institutions behind it". Different evidence, so a different independence
   test; the bar itself, and the domain identity underneath it, are shared from
   :mod:`jeles._independence` rather than written out twice.

3. **Propose, don't execute.** :func:`react` is pure routing: it calls an
   *injected* ``searcher`` and returns a list of proposed actions. It writes
   nothing — not the corpus, not FRANK. :func:`apply` executes proposals
   through injected drivers (defaulting to :mod:`jeles.corpus`). Reaction
   proposes; driver enforces — the model-proposes / gateway-enforces pattern,
   one layer down. Enforcing means :func:`apply` accepts only an explicit
   allowlist of arguments per driver and pins the verification rung itself:
   proposals are assembled from web-search results, so what one carries is a
   claim, and a claim must not be able to name its own place on the ladder.

The network lives *only* behind the injected ``searcher`` — there is no network
import at module load — so importing this module, and running :func:`react`
with a fake searcher, is fast and offline. That is the same purity seam
:mod:`jeles.corpus` holds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .._independence import MIN_INDEPENDENT_SOURCES
from .._independence import registrable_domain as _domain

# A searcher is any ``(query) -> [ {title, url, snippet}, ... ]``. The host wires
# one (e.g. an adapter over a web-search tool); the reaction never imports one.
Searcher = Callable[[str], list[dict[str, Any]]]

# The independent-SOURCE rule: a conflict is corroborated only by >= this many
# *distinct registrable domains*. Two pages on one site are one source, not two.
#
# The number, the domain identity behind it, and the reason it is not the
# constitution's Independent Witness all live in :mod:`jeles._independence` now,
# because `jeles.verify` asks the same question of different evidence and a
# second copy of these rules would have been a second set of the same bugs. This
# name stays as the reaction's own vocabulary for the bar it applies.
DEFAULT_MIN_SOURCES = MIN_INDEPENDENT_SOURCES

# The scan's machine witness. A corroborated finding is not a human-signed
# nugget; this tag says exactly what verified it — two independent sources —
# so a person can re-verify before it is trusted as settled.
WITNESS = "jeles:conflict-scan/2-independent-sources"


def frame_queries(claim: str, *, extra: list[str] | None = None) -> list[str]:
    """Turn a claim into a conflict-biased query set.

    One mirror query (baseline "does this exist"), then three that hunt the
    superseding / rival / refuting work. The bias is the point: a "find things
    like this" search validates; a "find what beats this" search carves the
    design down to the part nobody has already built.
    """
    claim = (claim or "").strip()
    if not claim:
        return []
    queries = [
        f"{claim} existing implementation library",  # mirror (baseline)
        f"{claim} alternative that supersedes",  # supersession
        f"{claim} vs prior art comparison",  # rivalry
        f"{claim} limitations criticism why not",  # refutation
    ]
    for q in extra or []:
        q = (q or "").strip()
        if q and q not in queries:
            queries.append(q)
    return queries


#: Domains that cannot be an independent witness, whatever they return.
#:
#: The search engine itself is the important one. DuckDuckGo's Instant-Answer
#: endpoint — the zero-config default backend — returns its `RelatedTopics` as
#: duckduckgo.com URLs, so an unfiltered domain count reads the engine as a
#: source about every claim, and pairs it with the one Wikipedia AbstractURL to
#: clear a two-source bar. Verified: a claim invented on the spot was
#: "corroborated by 2 independent sources (duckduckgo.com, wikipedia.org)".
#:
#: Shorteners are excluded because they are opaque — two of them can point at
#: one page, which is the exact opposite of independence.
_NON_WITNESS = frozenset(
    {
        "duckduckgo.com",
        "google.com",
        "bing.com",
        "yahoo.com",
        "baidu.com",
        "yandex.com",
        "search.brave.com",
        "ecosia.org",
        "startpage.com",
        "bit.ly",
        "t.co",
        "tinyurl.com",
        "goo.gl",
        "ow.ly",
        "buff.ly",
        "is.gd",
        "rebrand.ly",
        "cutt.ly",
        "shorturl.at",
        "lnkd.in",
        "dlvr.it",
    }
)


def _witnesses(hits: list[dict[str, Any]], claim: str) -> list[dict[str, Any]]:
    """The hits that may actually count toward corroboration.

    The old rule counted every returned domain. Nothing checked that a hit
    *refuted*, *superseded*, or even *mentioned* the claim — so any two results
    from any two sites promoted a machine-verified "prior work exists" nugget,
    and a search engine returning its own related-topic links satisfied it about
    every claim ever scanned.

    Three filters, in increasing order of how much they matter:

    * a parseable, non-address domain (see :func:`_domain`);
    * not a known non-witness (search engines, shorteners);
    * **shares at least one content word with the claim.**

    That last one is deliberately loose — one word, not a threshold — because
    the failure directions are not symmetric. Rejecting a genuine
    differently-worded witness costs a contested gap, which asks a human to
    look. Accepting an irrelevant one writes a nugget asserting prior art that
    does not exist. Under-reporting is recoverable; the corpus asserting
    something false is the thing this whole package exists not to do.

    It does not defend against mirrors and reposts (the same article on two
    sites is genuinely two domains), and it cannot: that needs content
    comparison, not URL rules. Named here rather than left implied.
    """
    from ..corpus import _tokens  # stdlib-only; keeps this module network-free

    claim_tokens = set(_tokens(claim))
    if not claim_tokens:
        return []
    out = []
    for h in hits:
        domain = h.get("domain") or ""
        if not domain or domain in _NON_WITNESS:
            continue
        text = f"{h.get('title') or ''} {h.get('snippet') or ''}"
        if claim_tokens & set(_tokens(text)):
            out.append(h)
    return out


def _gather(queries: list[str], searcher: Searcher, max_results: int) -> list[dict[str, Any]]:
    """Run every query through the injected searcher; normalize + dedupe by URL."""
    seen: set[str] = set()
    hits: list[dict[str, Any]] = []
    for q in queries:
        try:
            results = searcher(q) or []
        except Exception:
            # A single query failing must not sink the scan — it just yields
            # fewer witnesses, which the corroboration gate already handles.
            results = []
        for r in results[: max(0, max_results)]:
            url = str(r.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            hits.append(
                {
                    "title": str(r.get("title") or "").strip(),
                    "url": url,
                    "snippet": str(r.get("snippet") or "").strip(),
                    "domain": _domain(url),
                    "query": q,
                }
            )
    return hits


def react(
    event: dict[str, Any],
    *,
    searcher: Searcher,
    max_results: int = 6,
    min_sources: int = DEFAULT_MIN_SOURCES,
) -> list[dict[str, Any]]:
    """The reaction. ``event`` needs a ``claim``; ``kind``/``surface``/``tags``
    are optional context. Returns proposed actions — writes nothing.

    Proposals (most-actionable first, the conflict bias carried into ordering):

    * ``put_nugget`` — emitted only when the conflict is corroborated by
      ``>= min_sources`` independent domains. Records the *fact of corroborated
      prior art*, with the URLs as sources for a human to verify.
    * ``log_gap`` — emitted when the finding is contested (0—1 sources): the
      corpus remembers it looked and couldn't yet verify.
    * ``frank_append`` — always emitted last, so every firing leaves one legible
      line regardless of outcome (the reaction-engine legibility rule).
    """
    claim = str(event.get("claim") or "").strip()
    if not claim:
        return []

    queries = frame_queries(claim, extra=event.get("queries"))
    hits = _gather(queries, searcher, max_results)
    # Corroboration counts *witnesses*, not results. A hit that never mentions
    # the claim, or that comes from the search engine itself, is not evidence of
    # prior art no matter which domain served it.
    witnesses = _witnesses(hits, claim)
    domains = sorted({h["domain"] for h in witnesses})
    corroborated = len(domains) >= min_sources

    # Sources are exactly the witnessing URLs, so a human re-verifying "the
    # sources" sees precisely what cleared the bar — not query noise.
    sources = [h["url"] for h in witnesses]

    tags = ["conflict-scan", "prior-art"] + [str(t) for t in (event.get("tags") or [])]
    proposals: list[dict[str, Any]] = []

    if corroborated:
        top = domains[:4]
        answer = (
            f"Corroborated by {len(domains)} independent sources "
            f"({', '.join(top)}{'…' if len(domains) > len(top) else ''}). "
            f"Superseding / prior work exists — treat the design's overlap here "
            f"as bought, not novel. Human-verify the sources before sealing."
        )
        proposals.append(
            {
                "driver": "put_nugget",
                "reason": f"{len(domains)} independent domains corroborate prior art",
                "args": {
                    "question": f"Prior-art / conflict scan: {claim}",
                    "answer": answer,
                    "sources": sources,
                    "verified_by": WITNESS,
                    # Machine corroboration, not a human check — the driver stamps
                    # this so the nugget can't render as human-verified (corpus B6).
                    "verification_kind": "machine",
                    "tags": tags,
                },
            }
        )
    else:
        proposals.append(
            {
                "driver": "log_gap",
                "reason": (
                    f"contested — {len(domains)} independent source(s), "
                    f"below the {min_sources}-source bar"
                ),
                "args": {"question": f"Prior-art / conflict scan: {claim}"},
            }
        )

    proposals.append(
        {
            "driver": "frank_append",
            "reason": "legibility — every reaction leaves one line",
            "args": {
                "kind": "conflict_scan",
                "claim": claim,
                "event_kind": str(event.get("kind") or ""),
                "surface": str(event.get("surface") or ""),
                "corroborated": corroborated,
                "sources": sources,
                "domains": domains,
            },
        }
    )
    return proposals


# ── What a proposal is allowed to say to a driver ────────────────────────────
#
# `apply` used to splat `p["args"]` straight into the driver, so the driver's
# *signature* was the boundary. It is not one: the proposals `apply` executes are
# built by `react` out of web-search titles, URLs and snippets, and `apply` is a
# public function that anything can hand a list to. `corpus.put_nugget` accepts
# `verification_kind` and `nugget_id` — the two parameters c0f7941 deliberately
# kept off the MCP tool surface, because a caller that can set them can promote
# an unchecked claim to the top of the confidence ladder or land on an existing
# nugget's id.
#
# Reproduced against a temp store at 933d91a, before this allowlist existed:
#
#   hand-built proposal, verification_kind="human"
#       -> {'action': 'created', 'verification_kind': 'human'}
#          stored status: verified;  ask_corpus answered from it: True
#          verified_by AND written_by both carried straight from the proposal
#   same, plus nugget_id=<a human-verified nugget>
#       -> {'action': 'updated'};  that nugget's answer became the proposal's
#
# The second one is the reason the rung is pinned rather than merely checked:
# `put_nugget`'s own guard refuses a *lower* rung overwriting a higher one, so a
# proposal claiming "machine" is already stopped there — but "human" over
# "human" is not lower, and sailed through. The guard covers the machine case
# and nothing else; equal-rung overwrite was wide open.
_ALLOWED_ARGS: dict[str, frozenset[str]] = {
    "put_nugget": frozenset({"question", "answer", "sources", "verified_by", "tags"}),
    "log_gap": frozenset({"question"}),
}

# Driver parameters with no default. Missing one used to raise TypeError out of
# `apply`, which aborted the whole list — so one malformed proposal also lost the
# FRANK line that was supposed to make the firing legible.
_REQUIRED_ARGS: dict[str, frozenset[str]] = {
    "put_nugget": frozenset({"question", "answer", "sources", "verified_by"}),
    "log_gap": frozenset({"question"}),
}

# The rung this reaction is entitled to, chosen by the driver rather than read
# from the proposal. `react` does emit "machine", and that is the whole point of
# the reaction — but the driver choosing the rung and the proposal carrying it
# are different things, and only the first survives a proposal list `react` did
# not build. Pinning it means the escalation to "human" has nowhere to enter.
PROPOSAL_VERIFICATION_KIND = "machine"


def _vet(driver: str, args: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Check a proposal's args against its driver's allowlist.

    Returns ``(vetted_args, error)``; ``error`` is ``None`` when the proposal may
    run. Refusal is a receipt rather than an exception because `apply` processes
    a *list*: one bad proposal must not take the good ones with it.
    """
    if not isinstance(args, dict):
        return {}, {
            "error": "proposal_args_refused",
            "driver": driver,
            "detail": f"args must be a mapping, got {type(args).__name__}",
        }

    allowed = _ALLOWED_ARGS[driver]
    vetted = {k: v for k, v in args.items() if k in allowed}
    rejected = sorted(set(args) - allowed)

    if driver == "put_nugget":
        # `react` has always emitted verification_kind="machine", and the pin
        # agrees with it, so that exact value is not a rejection — react's output
        # applies unchanged. Any *other* rung is an escalation attempt and is
        # named as one rather than quietly rewritten: a proposal that asked for
        # "human", got "machine", and was never told is the same silent-drop
        # shape this package keeps finding bugs in.
        kind = args.get("verification_kind")
        if kind is not None and str(kind) != PROPOSAL_VERIFICATION_KIND:
            return {}, {
                "error": "proposal_args_refused",
                "driver": driver,
                "rejected": ["verification_kind"],
                "detail": (
                    f"a proposal may not set verification_kind={kind!r}; this "
                    f"reaction writes at the {PROPOSAL_VERIFICATION_KIND!r} rung "
                    "and the driver pins it. Proposals are built from web-search "
                    "results, so a rung carried in one is a claim, not evidence."
                ),
            }
        rejected = [k for k in rejected if k != "verification_kind"]
        vetted["verification_kind"] = PROPOSAL_VERIFICATION_KIND

    if rejected:
        detail = f"{driver} accepts only {sorted(allowed)} from a proposal; refused {rejected}"
        if "nugget_id" in rejected:
            # Named separately because it is not a typo, it is the overwrite
            # path: an id turns a new-nugget write into a write on top of an
            # existing record, keeping its place in every search result.
            detail += (
                ". nugget_id is not reachable from a proposal — a "
                "reaction may add a nugget, never replace one by id."
            )
        return {}, {
            "error": "proposal_args_refused",
            "driver": driver,
            "rejected": rejected,
            "allowed": sorted(allowed),
            "detail": detail,
        }

    missing = sorted(_REQUIRED_ARGS[driver] - set(vetted))
    if missing:
        return {}, {
            "error": "proposal_args_incomplete",
            "driver": driver,
            "missing": missing,
            "detail": f"{driver} requires {missing}",
        }
    return vetted, None


def apply(
    proposals: list[dict[str, Any]],
    *,
    put_nugget: Callable[..., Any] | None = None,
    log_gap: Callable[..., Any] | None = None,
    frank: Callable[[dict[str, Any]], Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute proposals through injected drivers. Defaults bind to
    :mod:`jeles.corpus` for the two corpus drivers; ``frank`` has no default
    (the host wires FRANK) and an un-wired ``frank_append`` is recorded as
    skipped, not silently dropped.

    Each corpus driver takes only the arguments in its :data:`_ALLOWED_ARGS`
    entry; anything else — ``nugget_id``, ``verified_at``, ``written_by``, a
    ``verification_kind`` other than the pinned one, a typo — produces an error
    receipt naming what was refused. It is not silently dropped, and it does not
    stop the rest of the list. See the note above :data:`_ALLOWED_ARGS` for the
    writes this was demonstrated to have allowed.
    """
    if put_nugget is None or log_gap is None:
        from .. import corpus

        put_nugget = put_nugget or corpus.put_nugget
        log_gap = log_gap or corpus.log_gap

    receipts: list[dict[str, Any]] = []
    for p in proposals:
        driver = p.get("driver")
        args = p.get("args") or {}
        if driver in _ALLOWED_ARGS:
            vetted, error = _vet(driver, args)
            if error is not None:
                receipts.append({"driver": driver, "result": error})
                continue
            fn = put_nugget if driver == "put_nugget" else log_gap
            receipts.append({"driver": driver, "result": fn(**vetted)})
        elif driver == "frank_append":
            # Not allowlisted: `frank` takes the whole entry as one dict rather
            # than as keyword arguments, so there is no signature to reach past,
            # and the FRANK log is append-only prose — no rung, no id, nothing
            # for an extra key to escalate into.
            result = frank(args) if frank else {"skipped": "no frank driver wired"}
            receipts.append({"driver": driver, "result": result})
        else:
            receipts.append({"driver": driver, "result": {"error": "unknown driver"}})
    return receipts
