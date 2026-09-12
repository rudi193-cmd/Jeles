"""Standalone MCP server over Jeles' verified-nugget corpus.

Mirrors willow-mcp's shape — `MCPServer`, stdio by default, every tool takes
an `app_id` — but scoped to one small corpus so it can run independently of
any particular fleet and be MCP-agnostic: any stdio MCP client (Claude
Code, Claude Desktop, Cursor, willow-mcp itself, a bare script) can point
at it with `python -m jeles.corpus_server`.

Unlike willow-mcp, this server does not implement manifest-based ACL. That is
a statement about *isolation* — the corpus is scoped to a single app's own
data, so there is nothing for a per-app permission gate to keep apart — and
it was mistaken for a statement about *writes*. The two are different: no
caller here needs protecting from another caller's data, but every reader
needs protecting from a claim that the corpus never checked. `corpus_put` is
gated on that axis instead, by rung rather than by identity (see its
docstring). `app_id` is accepted on every tool for naming-convention parity,
and is now also recorded as `written_by` on anything written through one.
This server does not depend on willow-mcp.

Tools:
  corpus_ask     — best-match *verified* nugget for a question, or
                   {found: false} (logs a gap on miss)
  corpus_search  — ranked nugget search (no gap logging)
  corpus_get     — fetch a single nugget by id
  corpus_list    — list nuggets, most recently updated first
  corpus_put     — record a nugget as an unchecked assertion
  corpus_gaps    — list logged "I don't know yet" questions
  corpus_resolve_gap — mark a gap answered, taking it out of that queue

The outward hops, for what the verified layer could not answer:

  corpus_web_search           — the open web; results are always `unverified`
  corpus_institutional_search — every registered institutional/academic
                                collection, run in-process; results are
                                `institutional`
  corpus_sources              — which collections exist, and which need a key
  corpus_search_status        — can either outward hop work? (asks nothing of
                                the network)

Checking a claim, and knowing whose shelf it came off:

  corpus_verify_claim         — is one claim findable in an institution's
                                catalogue? Read `overlap` before believing
                                `matched`; `source_rank` is the publisher's
                                rank, not the match's quality
  corpus_host_card            — what a hostname is: publisher, custody,
                                jurisdiction, and which roles it may play

And the two fleet edges, which fail silently by design and so need a window
of their own:

  corpus_fleet_status         — did forwarded gaps reach willow-mcp, and is
                                the `human` rung reachable via Nestor at all?

Together those are the persona's mandate — local KB → open web → special
collections — with the confidence ladder intact across all three:

  verified > corroborated > institutional > unverified

A rung is earned, not declared. `verified` comes only from a person writing
in-process, `corroborated` only from independent sources agreeing; a nugget
written through `corpus_put` sits at `unverified` with everything else nobody
checked. That is what stops the bottom of this ladder from reaching the top of
it by way of a tool call.
"""

from __future__ import annotations

import os

try:
    from mcp.server.mcpserver import MCPServer
except ImportError as exc:  # pragma: no cover - exercised by install shape, not tests
    # The MCP SDK is an optional extra: base `jeles` has zero runtime
    # dependencies so a host can depend on it without inheriting a version
    # constraint. Two different failures land here, and they need different
    # answers, so say which one it is rather than leaking a bare traceback.
    if exc.name == "mcp":
        raise ImportError(
            "jeles.corpus_server needs the MCP SDK, which base `jeles` does not "
            'install. Add the extra:  pip install "jeles[mcp]"\n'
            "(the corpus, the persona, and the reactions all work without it)"
        ) from exc
    raise ImportError(
        "jeles.corpus_server requires MCP SDK 2.x — `mcp.server.mcpserver` does "
        "not exist in SDK 1.x, where the equivalent was `mcp.server.fastmcp`. "
        "Upgrade:\n"
        '    pip install --upgrade "jeles[mcp]"   # pins mcp>=2.0,<3\n'
        "SDK 2.x is also what willow-mcp requires, so the two now co-install."
    ) from exc

from typing import Annotated
from urllib.parse import urlparse

from pydantic import Field

import jeles
from jeles import _nestor_seal, cards, corpus, institutional, source_trail, willow_mcp_client
from jeles.reactions import search_adapter

#: `app_id` is the first parameter of every tool here, and until now it carried
#: no schema description at all — its meaning lived in this module's docstring,
#: which a calling model never sees. Measured 2026-08-28 against eight local
#: models: every one of them filled it wrong, either omitting it, inventing a
#: value ("design", "your_app_id"), or putting the *subject of the question*
#: there ("Tokyo Night"). One sentence of description fixed it for four of
#: them. The lesson generalises past this parameter: a required argument whose
#: meaning is only in prose the model cannot read is a required argument the
#: model will guess.
AppId = Annotated[
    str,
    Field(
        description=(
            "The calling application's own name, e.g. 'ask-jeles'. Identifies who is "
            "calling. NOT the subject of the question or search."
        )
    ),
]

mcp = MCPServer(
    "jeles-corpus",
    version=jeles.__version__,
    instructions=(
        "Jeles' verified-nugget corpus. Ask a question to get a cited, "
        "human-verified answer if one exists, search the corpus directly, "
        "or contribute a new verified nugget. Misses are logged as gaps "
        "for someone to fill in later."
    ),
)


@mcp.tool()
def corpus_ask(app_id: AppId, question: str) -> dict:
    """Answer from the verified corpus if a nugget matches; returns
    {found: false} and logs a gap otherwise. The gap also gets a
    best-effort, non-blocking forward to willow-mcp's fleet-wide gap
    backlog, so it isn't just a local secret.

    Only human-verified and machine-corroborated nuggets answer. Anything
    written through `corpus_put` is an unchecked assertion and comes back
    under `candidates` instead — `found: true` here means the settled layer
    is speaking."""
    result = corpus.ask_corpus(question)
    if not result.get("found"):
        willow_mcp_client.forward_gap(question)
    return result


@mcp.tool()
def corpus_search(app_id: AppId, query: str, limit: int = 8) -> list:
    """Ranked nugget search across the corpus. Never logs a gap."""
    return corpus.search_nuggets(query, limit=limit)


@mcp.tool()
def corpus_get(app_id: AppId, nugget_id: str) -> dict:
    """Fetch a single nugget by id."""
    return corpus.get_nugget(nugget_id)


@mcp.tool()
def corpus_list(app_id: AppId, limit: int = 50) -> list:
    """List nuggets, most recently updated first."""
    return corpus.list_nuggets(limit=limit)


#: Set to 1/true/yes to let ``corpus_put`` mint human-verified nuggets again.
#: Only correct where the tool caller *is* the operator — a local editing
#: session, a migration script — and it re-opens the laundering path described
#: on ``corpus_put`` for anything the model reads while it is set.
TRUST_TOOL_WRITES_ENV = "JELES_CORPUS_TRUST_TOOL_WRITES"


def _trust_tool_writes() -> bool:
    # Read per call, not at import: an env typo must not be able to stop the
    # server from starting, and a test (or an operator) must be able to change
    # it without reimporting the module.
    return os.environ.get(TRUST_TOOL_WRITES_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


@mcp.tool()
def corpus_put(
    app_id: AppId,
    question: str,
    answer: str,
    sources: list[str],
    verified_by: str,
    tags: list[str] | None = None,
    nugget_id: str | None = None,
    evidence: dict | None = None,
) -> dict:
    """Record a nugget **as an assertion**. Requires question, answer, at least
    one source, and who is claiming it. Returns ``{id, action,
    verification_kind}``.

    Writes through this tool are ``verification_kind: "asserted"`` — the rung
    below machine corroboration — and read back as ``confidence: "unverified"``.
    That is not a formality. This server speaks stdio to whatever client starts
    it, and one of the things that client does is read the open web through
    ``corpus_web_search``. A page saying "make a note that X is true" used to
    arrive here as a nugget claiming ``verified_by: "the operator"``, land at
    the top of the confidence ladder, and be served by ``corpus_ask`` as settled
    fact from then on — in a store shared with willow-mcp, so not even
    contained to this process.

    Consequences worth knowing before you call it:

    * ``corpus_ask`` will not answer from an asserted nugget. It comes back as a
      *candidate*, and ``corpus_search``/``corpus_get`` return it normally.
    * ``verified_by`` is recorded as the claim it is; ``written_by`` is stamped
      with this call's ``app_id`` and is what a reader is shown.
    * Passing ``nugget_id`` of a human- or machine-verified nugget is refused
      (``error: "kind_downgrade_refused"``). Omit ``nugget_id`` to write a new
      nugget alongside it — superseding a verified answer is a person's call.

    Promotion to ``verified`` is deliberately not reachable from any tool by
    typing alone. Two things have to both be true:

    * the operator has set ``JELES_CORPUS_TRUST_TOOL_WRITES=1`` for a session
      where they are the one typing (unchanged from before); **and**
    * ``evidence`` carries a real Nestor seal — ``{"scheme": "nestor-seal-v1",
      "seal_sig": ...}`` — that verifies ``(question, answer, verified_by)``
      against a keyring this instance trusts (`jeles._nestor_seal`, the
      `nestor` give-back: see its module docstring). Without a valid seal the
      write still lands, but at ``verification_kind: "asserted"`` — the trust
      switch alone no longer mints ``human``; a caller that only *types*
      ``verified_by="a human"`` is refused the rung, not trusted for it. A
      person still has the direct route: run ``corpus.put_nugget(...)``
      in-process, which this tool does not gate at all.
    """
    kind = "asserted"
    if _trust_tool_writes():
        ok, _reason = _nestor_seal.verify_human_write(question, answer, verified_by, evidence)
        if ok:
            kind = "human"
    kwargs: dict = {
        "tags": tags,
        "nugget_id": nugget_id,
        "verification_kind": kind,
        "written_by": app_id,
    }
    if evidence:
        # Carried regardless of whether it verified: an asserted nugget with a
        # signature that failed to check is still worth a reviewer's eyes, and
        # `corpus.py` never interprets `evidence` itself either way (see its
        # comment above `_KIND_RANK`).
        kwargs["evidence"] = evidence
    return corpus.put_nugget(question, answer, sources, verified_by, **kwargs)


@mcp.tool()
def corpus_gaps(app_id: AppId, limit: int = 50, include_resolved: bool = False) -> list:
    """List logged 'I don't know yet' questions, most-asked first — the
    corpus's growth queue.

    Resolved gaps are left out unless ``include_resolved`` asks for them. A
    resolved gap's ``asked_count`` is frozen (once answered, `corpus_ask` hits
    the nugget and stops logging), so keeping them in a list sorted by that
    count would park long-answered questions above every newer open one.
    Pass ``include_resolved=True`` to read the history rather than the queue."""
    return corpus.list_gaps(limit=limit, include_resolved=include_resolved)


@mcp.tool()
def corpus_resolve_gap(
    app_id: AppId,
    gap_id: str,
    nugget_id: str = "",
    resolved_by: str = "",
) -> dict:
    """Mark a gap answered, taking it out of the growth queue. Returns
    ``{id, status, resolved_at}`` or ``{"error": "not_found"}``.

    Until this existed the queue was write-only: `corpus_ask` logged a miss,
    `corpus_gaps` listed it, and nothing could ever say it had been filled.

    The gap is marked, not deleted — what was asked and how often is the
    corpus's record of its own blind spots, and it stays readable through
    ``corpus_gaps(include_resolved=True)``. If the same question misses again
    later the gap reopens by itself, keeping the earlier resolution alongside a
    `reopened_at`, so a recurring hole does not read as a new one.

    **This closes a queue; it verifies nothing.** Resolving a gap makes no
    claim that whatever answers it is true, and moves nothing up the confidence
    ladder — that is `corpus_put`'s business, and for the `human` rung a
    signature's (see `corpus_fleet_status`). ``resolved_by`` defaults to the
    calling ``app_id`` so the record always says who closed it; pass a person's
    name when a person decided it.
    """
    return corpus.resolve_gap(gap_id, resolved_by=resolved_by or app_id, nugget_id=nugget_id)


# ── The second hop: the open web ────────────────────────────────────────────
#
# The persona's mandate is "local KB → open web → special collections", and
# until now this server implemented only the first. `search_adapter` existed
# but had exactly one consumer — conflict_scan.react — which was not exposed as
# a tool, so a client running this server got a corpus and no internet at all.
#
# These do not change what `corpus_ask` does. The corpus sits *in front of*
# live search rather than replacing it (design principle 1), so the second hop
# is a separate, explicit call: the caller decides to leave the settled layer,
# and can see that it did.


def _web_hit(hit: dict, idx: int) -> dict:
    """Shape a raw searcher result like `corpus.to_search_hit` shapes a nugget,
    so corpus and web results merge into one ranked list without translation.

    The fields that must never collapse are `source_id` and `confidence`: a
    human-verified nugget and a page someone found on the internet can sit in
    the same list, but they cannot be allowed to *read* the same. Everything
    here is `unverified` — the librarian's no-unsourced-output rule expressed
    as data rather than as a warning in prose.
    """
    url = str(hit.get("url") or "")
    try:
        host = urlparse(url).netloc or "web"
    except ValueError:
        host = "web"
    return {
        "title": hit.get("title") or "",
        "url": url,
        "snippet": hit.get("snippet") or "",
        "source": f"Open web — {host}",
        "date": "",
        "source_id": "web",
        "hostname": host,
        "confidence": "unverified",
        "verification_kind": "none",
        "nugget_id": "",
        "verified_by": "",
        "verified_at": "",
        "extra_sources": [],
        "tags": [],
        # Kept in the merge contract with `corpus.to_search_hit` — see the
        # comment above `_KIND_RANK` in corpus.py. A page from the open web
        # never carries evidence of this kind, so it is always empty here.
        "evidence": {},
        "n": idx,
    }


@mcp.tool()
def corpus_web_search(app_id: AppId, query: str, limit: int = 8) -> dict:
    """Search the open web — the corpus's second hop, for questions the
    verified layer could not answer.

    Returns ``{hits, ok, backend, shallow, error}``. Read `ok` before reading
    `hits`: an empty list with ``ok: true`` means the web had nothing, and an
    empty list with ``ok: false`` means the search never happened (unset key,
    unreachable host, wrong backend). Those are different facts and answering
    "I don't know" on the second is a lie.

    ``shallow: true`` means the selected backend is a placeholder that cannot
    corroborate a claim even when it "works" — treat thin results as a
    configuration problem, not as evidence of absence. (The zero-config
    ``ddg`` default is a real DuckDuckGo HTML-SERP scrape, not shallow; it can
    still return ``ok: false`` if its circuit breaker is open after repeated
    failures.) Call ``corpus_search_status`` for the details.

    Every hit is ``confidence: "unverified"``, and stays unverified if you
    record it: ``corpus_put`` writes assertions, not verified nuggets, so a
    page found here cannot promote itself into the settled layer by being
    written down. Promotion is a person's act.
    """
    out = search_adapter.search_with_status(query)
    # `max(0, limit)` for the same reason `corpus.py` guards every one of its
    # own slices: a bare [:limit] reads a negative limit as "all but the last
    # N", so `limit=-1` quietly returns almost everything instead of nothing.
    hits = [_web_hit(h, i) for i, h in enumerate(out["hits"][: max(0, limit)])]
    return {
        "hits": hits,
        "ok": out["ok"],
        "backend": out["backend"],
        "shallow": out["shallow"],
        "error": out["error"],
    }


@mcp.tool()
def corpus_search_status(app_id: AppId) -> dict:
    """Report whether the outward hops can work at all, without searching.

    Top-level keys describe the open web —
    ``{backend, configured, shallow, requires, reason}`` — and
    ``institutional`` carries the same question for the third hop, including
    which lane it will take (`local` in-process, or a configured `remote`).

    Worth calling before concluding that anything "found nothing": the
    zero-config web default is `ddg`, a DuckDuckGo HTML-SERP scrape that needs
    no configuration and is `configured` for that reason — but it scrapes an
    unofficial page DuckDuckGo can block without notice, so it is guarded by a
    circuit breaker. A silent `[]` after repeated failures and an open breaker
    look the same from `corpus_search`; call this first to tell them apart.
    """
    status = dict(search_adapter.describe_backend())
    # Additive: the web keys stay at the top level so anything reading this
    # tool before the third hop existed keeps working unchanged.
    status["institutional"] = institutional.describe_remote()
    return status


# ── The third hop: special collections ──────────────────────────────────────


@mcp.tool()
def corpus_institutional_search(
    app_id: AppId,
    query: str,
    limit: int = 12,
    sources: list[str] | None = None,
    limit_per_source: int = 3,
) -> dict:
    """Search named institutional and academic collections — the persona's
    third hop, and the one that produces citable answers.

    One query fans out across every registered source (arXiv, PubMed, Crossref,
    OpenAlex, Library of Congress, Europeana, CourtListener, the Smithsonian,
    ...), **in this process** — no service to deploy and no secret to hold.
    ``sources`` narrows the fan-out to specific registered ids; omit it for
    every non-opt-in source, and call ``corpus_sources`` for the current list
    rather than trusting a count written down here, which drifts.

    Returns ``{hits, ok, lane, sources_queried, failed, skipped, timed_out,
    unknown, total, error}``. Read `ok` before reading `hits`: ``ok: false``
    means no source completed a look — an outage, a blocked egress, or every
    key-required source abstaining — as distinct from the shelves being empty.
    Each dispatched source appears in exactly one of the accounting lists, so
    "nobody had it" and "nobody was asked" stay different answers. ``lane`` is
    ``local`` unless a remote deployment is configured.

    Every hit is ``confidence: "institutional"`` — its own rung between a
    corpus nugget's ``verified``/``corroborated`` and the open web's
    ``unverified``. A Library of Congress record is neither a human-checked
    nugget nor a random page, and collapsing it into either would discard the
    only thing this hop is for. `source` names the publishing body.
    """
    out = institutional.search_institutional(
        query,
        sources_filter=sources,
        # Guarded before it leaves this process: `limit_per_source` is passed
        # down into `sources.py`, where each of the 65 source functions slices
        # its own results with a bare [:limit]. A negative value would reach
        # every one of them.
        limit_per_source=max(0, limit_per_source),
    )
    return {**out, "hits": out["hits"][: max(0, limit)]}


# ── The fleet edges: willow-mcp and Nestor ──────────────────────────────────
#
# `corpus_search_status` answers "can this edge work?" for the two *outward*
# hops. This package has two more edges and neither could be asked the same
# question: `willow_mcp_client.forward_status()` was written, tested, and
# reachable from nothing, and `_nestor_seal` had no status function at all.
#
# Both fail silently by design, which is what makes the omission expensive.
# Gap forwarding is best-effort and never raises, so a fleet that has been
# refusing every forward for a week looks exactly like a working one. A missing
# `[nestor]` extra is only discoverable by attempting a write and reading the
# rung it landed at. Neither is a bug in the hiding — both are correct
# behaviour that simply had no window.


@mcp.tool()
def corpus_fleet_status(app_id: AppId) -> dict:
    """Report whether this instance's two *fleet* connections work, without
    using either — the companion to ``corpus_search_status``, which answers the
    same question for the open web and the institutional collections.

    Returns ``{willow_mcp, nestor}``.

    ``willow_mcp`` is where forwarded gaps actually went:
    ``{enabled, app_id, session_ready, session_error, forwarded, failed,
    last_error}``. Forwarding is best-effort and never raises into
    ``corpus_ask``, so ``failed`` climbing while ``forwarded`` stays flat is the
    only way to see a gate denial — an app_id without ``gap_write``, or no
    manifest at all. ``session_error`` says why no session exists;
    ``last_error`` says why the last call failed on a session that does. A
    local ``corpus.log_gap`` has already succeeded regardless: the local store
    is the source of truth and the fleet backlog is additive (design principle
    7), so ``failed`` is a fleet problem, never a lost gap.

    ``nestor`` is whether the ``human`` rung is reachable here at all:
    ``{scheme, installed, signing_enabled, ready, reason}``. ``ready: false``
    means ``corpus_put`` cannot mint ``human`` no matter what a caller sends —
    the write still lands, at ``asserted``. ``reason`` is the same string
    ``corpus_put`` would refuse with, so the two never disagree. No key, path,
    or key material appears in any field.

    Neither half asks anything of the network, and neither verifies anything.
    """
    return {
        "willow_mcp": willow_mcp_client.forward_status(),
        "nestor": _nestor_seal.describe(),
    }


# ── Checking a claim, and knowing whose shelf it came off ───────────────────
#
# `source_trail.verify_claim` and `cards` were both already here — tested,
# public, and reachable from no tool, like `forward_status` before them. They
# answer the two questions an agent actually has when handed a sentence and a
# link: is this backed by anything, and what is the thing it is backed by.


@mcp.tool()
def corpus_verify_claim(
    app_id: AppId,
    claim: Annotated[
        str,
        Field(
            description=(
                "One factual claim, as a single sentence. Not a question, and not a "
                "whole document - pass one claim per call."
            )
        ),
    ],
    sources: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional: restrict the check to these registered source ids (see "
                "corpus_sources). Omit to let the claim route itself."
            )
        ),
    ] = None,
    limit: Annotated[int, Field(description="Results per source. Default 2.")] = 2,
) -> dict:
    """Check whether one claim is backed by a real institutional source, and
    say which. Use this before repeating a fact you did not verify yourself.

    Returns ``{claim, matched, title, url, date, source, institution, tier,
    source_rank, overlap}``. ``matched: false`` with empty fields means the
    fan-out returned nothing — which is an answer, not a failure.

    **Read `overlap` before you believe `matched`.** ``matched: true`` means
    only that a search came back with a document. Nothing compares that
    document to the claim, so a claim assembled from common academic
    vocabulary matches *something* in a high-ranked journal every time.
    Measured 2026-08-28: "Gemma 4 ships with native function calling" was
    reported ``matched: true`` against an Elsevier paper — a claim about a
    model that does not exist. ``overlap`` is how much of the claim the
    document actually says, and it is what separated that case (0.38) from a
    real one (0.57). It is reported, not enforced: no threshold here has been
    earned, so a caller that wants a bar must pick one and say so.

    ``source_rank`` is the *publisher's* rank from `sources.py`'s own table —
    0.9 means "Elsevier is highly ranked", never "this claim is 90%
    supported". It was called ``confidence`` until the day that distinction
    was measured.

    **And one witness is not agreement.** The single highest-ranked hit wins;
    nothing counts how many independent institutions concur. `matched: true`
    must never be reported as "verified" — the corpus's `verified` rung is a
    person's act and `corroborated` needs independent domains
    (`jeles.verify`, `jeles.reactions.conflict_scan`). At best this says a
    claim is *findable*, which is the weakest of the three answers and the
    one worth asking first.
    """
    return source_trail.verify_claim(claim, sources=sources, limit=max(0, limit))


@mcp.tool()
def corpus_host_card(
    app_id: AppId,
    host: Annotated[
        str,
        Field(
            description=(
                "A hostname, e.g. 'api.crossref.org'. Just the host - not a full URL, "
                "though a trailing dot and any capitalisation are tolerated."
            )
        ),
    ],
) -> dict:
    """Say what a hostname is: who publishes it, who holds custody, whose
    jurisdiction it sits in, and what roles it may play. Use it when you have
    a URL and need to know what kind of source you are looking at.

    Returns ``{found: true, card: {...}}``, or ``{found: false, host}`` for a
    host with no card — which means *this package has no statement about it*,
    not that the host is untrustworthy. The catalog covers the collections
    Jeles itself reaches, not the open web.

    ``roles`` is the useful field and it is narrower than the catalog: a host
    may be a ``query`` endpoint, a ``citation`` target, a ``namespace``, or
    several. Only some are citable, so "Jeles talks to it" and "you may cite
    it" are different facts and this is where they part company.

    A card records custody and jurisdiction; it reaches no verdict about
    whether a host should be trusted. That decision belongs to a policy, and
    to a person.
    """
    found = cards.card(host)
    if found is None:
        return {"found": False, "host": (host or "").strip()}
    return {"found": True, "card": found}


@mcp.tool()
def corpus_sources(app_id: AppId) -> dict:
    """List the registered institutional collections, without searching.

    ``{sources: [{id, name, key_required, opt_in}], total, default_count}``.
    A `key_required` source is skipped silently when its key is absent, so this
    is how a caller learns what it is *not* reaching; `opt_in` sources sit out
    of the default fan-out and must be named explicitly in
    ``corpus_institutional_search(sources=[...])``.
    """
    listed = institutional.list_sources()
    return {
        "sources": listed,
        "total": len(listed),
        "default_count": sum(1 for s in listed if not s["opt_in"]),
    }


def main() -> None:
    # Explicit transport: SDK 2.0 moved host/port off the constructor onto
    # run(transport=, host=, port=), making "where this instance listens" a
    # property of the run rather than of the server. stdio is the default and
    # the only mode this server offers — it is a local organ, not a service.
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
