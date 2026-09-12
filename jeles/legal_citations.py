"""legal_citations — is this citation real, not just plausible-looking?

A different question from ``jeles.sources.search_courtlistener``. That one is a
*search*: given a query, ask CourtListener what case law matches it, and hand
back candidate results for a human or a downstream reaction to weigh. This
module is a *verifier*: given prose that already contains citations — the kind
an LLM writes, confidently, sometimes to a case that does not exist — ask
CourtListener's Citation Lookup API to resolve each one against its database
and report, per citation, whether it is real. Search finds candidates;
verification checks a claim already made. Conflating the two would make a
hallucinated citation that merely *looks* like real reporter notation
(``123 F.4th 456``) indistinguishable from one CourtListener actually has on
file — which is the exact failure mode a citation verifier exists to catch.

**The endpoint.** ``POST /api/rest/v4/citation-lookup/`` takes the whole blob
of prose as one form field (``text=``) rather than a pre-extracted citation
list — CourtListener does the citation *finding* server-side (via its
``eyecite`` parser) as well as the lookup, so this module never runs its own
regex over the text and inherits eyecite's parsing rather than a hand-rolled
approximation of it. The response is a JSON array with one object per citation
*eyecite found*, not one per span of text that might be a citation — prose
with zero recognizable citations returns ``[]``, not an error.

**Per-citation ``status`` — what each number means, and why the mapping cares.**
CourtListener document these four:

* ``200`` — found. A real cluster backs it; ``clusters`` is non-empty.
* ``404`` — the citation is well-formed (a real reporter, plausible volume/page)
  but nothing in CourtListener's database matches it. This is the header case
  a verifier exists for: text that reads as a perfectly good citation and
  simply is not one. Treating this as a match because "status came back" would
  turn the exact hallucination this module is meant to catch into a false
  positive.
* ``400`` — the reporter abbreviation itself is not recognized. Malformed or
  invented citation form, not a specific-case lookup miss.
* ``300`` — ambiguous: more than one real citation could be meant.
  ``normalized_citations`` lists the resolutions CourtListener considered;
  this module surfaces that list rather than picking one, since picking would
  be a guess this module has no basis for.

Only ``200`` counts as ``matched: True`` here — see :func:`_record`. Every
other value, including ones not in this list (a fifth status added upstream),
maps to ``matched: False`` rather than raising, because a verifier that raises
on an unrecognized status code makes CourtListener's future evolution able to
crash a caller reading old citations offline.

There is a fifth, unrelated ``429`` that appears **inside** the array, on
individual citation objects, when a request exceeds 250 citations —
CourtListener parses everything but only matches the first 250. That is
distinct from the *top-level* HTTP 429 described below: one is a per-citation
field inside a 200 response, the other is the whole request being refused.
They use the same number for two different things; this module tells them
apart because they need different handling — the per-citation one is just
another `matched: False` row, the top-level one is a request that should be
retried later.

**Token posture — required, not merely preferred.** CourtListener's anonymous
rate limit is 5/minute, which a single call to :mod:`jeles.sources`' own
``search_courtlistener`` (also unauthenticated) could already exhaust on a
shared deployment. Rather than let this module silently degrade into
tripping that limit, it makes **zero network calls** when no token resolves —
:func:`verify_citations` short-circuits before building a request, and returns
a self-reported ``configured: False`` result. Mirrors
``jeles.reactions.search_adapter.describe_backend``'s stance: an unconfigured
backend should say so as data, not spend a round trip discovering it, and
never be indistinguishable from "checked and found nothing".

**The 64,000-character limit is enforced here, not just documented.** Posting
over the cap is refused client-side with a flagged, non-ok result rather than
silently truncated. Truncation was the other option and was rejected: cutting
prose at an arbitrary byte offset can sever a citation mid-reporter (turning
a real one into unparseable noise) or drop the second half of the text
entirely, and either way the caller would get back "no citations found" for
text that was never actually checked — a silent gap dressed as a clean
negative result. Refusing tells the caller the text needs to be split; a
result set with `citations: []` cannot tell them that.

**Egress.** Routed through :mod:`jeles._egress`, https-only — the same lane
``jeles.sources`` uses for its 61 institutional fan-out sources, not the
looser one :mod:`jeles.reactions.search_adapter` uses for an operator-chosen
SearXNG instance. CourtListener is a fixed public API with no claim to a
private address, so ``allow_private=False``. This is the package's first POST:
every existing egress site is a GET, and the shape stdlib wants for a POST
body is already-encoded ``bytes`` handed to ``Request(data=...)`` — done here
with ``urllib.parse.urlencode`` rather than JSON, because the documented
content type is ``application/x-www-form-urlencoded``, not JSON.

**Fail-soft, always.** No network error, HTTP error, or malformed response
from this module ever raises into a caller's path — every failure, including
the top-level 429 (rate limited, not "no citations"), comes back as
``{"ok": False, ...}`` with a ``reason`` a caller can log or show. A caller
that wants to *know* whether the check actually ran reads ``ok`` and
``configured``, never assumes a truthy return meant "verified".
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
from typing import Any

from jeles import _egress

log = logging.getLogger("jeles.legal_citations")

_ENDPOINT = "https://www.courtlistener.com/api/rest/v4/citation-lookup/"

#: CourtListener's own hard limit on the `text` field. Enforced client-side
#: (see module docstring) rather than left for the API to reject, so a
#: too-long call never reaches the network at all — one less way to spend a
#: token's rate budget on a request that was always going to fail.
MAX_TEXT_CHARS = 64_000

_TIMEOUT = float(os.environ.get("JELES_LEGAL_CITATIONS_TIMEOUT", "20"))
# A citation-lookup response is one small JSON object per matched citation;
# even a text at the 64k-char cap with 250 matched citations (the API's own
# per-request cap) stays well under a megabyte. This is generous headroom
# against a misbehaving/compromised endpoint, not a sizing of the normal case.
_MAX_BYTES = int(os.environ.get("JELES_LEGAL_CITATIONS_MAX_BYTES", str(2 * 1024 * 1024)))

#: https only. CourtListener is one fixed public host, the same posture
#: `jeles.sources` takes for its whole institutional fan-out — never the
#: looser HTTP_OR_HTTPS lane reserved for an operator-chosen private endpoint.
_ALLOWED_SCHEMES = _egress.HTTPS_ONLY


def _empty(
    *, ok: bool, configured: bool, reason: str, citations: list | None = None
) -> dict[str, Any]:
    """The one result shape, whichever branch built it.

    Every return from :func:`verify_citations` — not-configured, guard-refused,
    network failure, rate-limited, or a genuine success — carries the same key
    set (``ok, configured, reason, citations``, success adds ``count`` and
    ``matched_count``), so a caller can read ``result["citations"]`` without
    first checking which branch produced the dict.
    """
    return {
        "ok": ok,
        "configured": configured,
        "reason": reason,
        "citations": citations if citations is not None else [],
    }


def _cluster_field(cluster: dict[str, Any], *names: str) -> Any:
    """First present value among CourtListener's known spellings for a field.

    The Citation Lookup API's exact cluster key names are not pinned by the
    contract this module was built from — only that "case metadata: case
    name, court, date" is there. `jeles.sources.search_courtlistener`, which
    hits CourtListener's *search* endpoint (a related but different response
    shape), reads `caseName`/`case_name`, `court_id` and `dateFiled`; the v4
    REST convention elsewhere in that API is snake_case (`case_name`,
    `date_filed`). Both are tried, in that order, so a real response is read
    correctly under either convention rather than this module guessing wrong
    and silently reporting an empty field for a hit that had the data.
    """
    for name in names:
        value = cluster.get(name)
        if value:
            return value
    return ""


def _record(item: dict[str, Any]) -> dict[str, Any]:
    """One CourtListener citation-lookup object -> this module's clean shape.

    ``matched`` is ``status == 200`` and nothing softer — see the module
    docstring's status table for why 404 (valid citation, not in the
    database) must not count as a match despite "the API answered".
    """
    status = item.get("status")
    clusters = item.get("clusters") or []
    cluster = clusters[0] if clusters and isinstance(clusters[0], dict) else {}
    abs_url = _cluster_field(cluster, "absolute_url")
    normalized = item.get("normalized_citations")
    return {
        "citation": str(item.get("citation") or ""),
        "normalized_citations": list(normalized) if isinstance(normalized, list) else [],
        "status": status,
        "matched": status == 200,
        "case": str(_cluster_field(cluster, "case_name", "caseName", "case_name_short")),
        "court": str(_cluster_field(cluster, "court", "court_id", "court_citation_string")),
        "date": str(_cluster_field(cluster, "date_filed", "dateFiled")),
        # Same construction as `sources.search_courtlistener`: the API hands
        # back a path, not a full URL, and courtlistener.com is the only host
        # that path is ever relative to.
        "url": f"https://www.courtlistener.com{abs_url}" if abs_url else "",
    }


def verify_citations(text: str, *, token: str | None = None) -> dict[str, Any]:
    """Ask CourtListener which citations in ``text`` are real.

    ``token`` falls back to ``COURTLISTENER_API_TOKEN``, read here — per call,
    not at import — so a token set after this module is imported (or rotated
    mid-process) is picked up on the next call rather than baked in at load.

    **No token -> zero network calls.** Returns
    ``{"ok": False, "configured": False, "reason": "...", "citations": []}``
    immediately. See the module docstring's "Token posture" section for why
    this is a hard requirement rather than a fall-through to anonymous access.

    **Over the 64,000-character API limit -> refused, not truncated.** Same
    shape, ``configured: True`` (a token *was* available; the input was the
    problem), ``reason`` names the length and the limit.

    **Any other failure — network error, non-2xx HTTP, unparseable body —
    never raises.** It comes back the same shape with a ``reason`` describing
    what happened. A top-level HTTP 429 (the whole request rate-limited, not
    the per-citation "over 250" status inside a 200) gets special handling:
    CourtListener's documented body on a 429 is ``{"wait_until": "<ISO-8601>"}``,
    and that timestamp is folded into ``reason`` when present, so a caller
    that wants to schedule a retry does not have to re-derive it.

    **Success** returns
    ``{"ok": True, "configured": True, "reason": "", "citations": [...],
    "count": N, "matched_count": M}`` — one record per :func:`_record` for
    every object CourtListener's array contained, in the order returned.
    """
    resolved_token = token or os.environ.get("COURTLISTENER_API_TOKEN", "")
    if not resolved_token:
        return _empty(
            ok=False,
            configured=False,
            reason=(
                "no CourtListener API token — set COURTLISTENER_API_TOKEN "
                "or pass token=. Anonymous access is 5 requests/minute, "
                "easily exhausted, so this module makes no network call "
                "at all without one rather than degrade into tripping it."
            ),
        )

    if len(text) > MAX_TEXT_CHARS:
        return _empty(
            ok=False,
            configured=True,
            reason=(
                f"text is {len(text)} characters, over CourtListener's "
                f"{MAX_TEXT_CHARS}-character limit for the citation-lookup "
                f"endpoint — refusing rather than silently truncating, "
                f"which could sever a citation mid-reporter or drop the "
                f"back half of the text and report a false-clean result. "
                f"Split the text and call again per chunk."
            ),
        )

    body = urllib.parse.urlencode({"text": text}).encode("utf-8")
    headers = {
        "Authorization": f"Token {resolved_token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        raw = _egress.fetch(
            _ENDPOINT,
            allowed=_ALLOWED_SCHEMES,
            timeout=_TIMEOUT,
            max_bytes=_MAX_BYTES,
            data=body,
            headers=headers,
            allow_private=False,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            wait_until = ""
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
                if isinstance(payload, dict):
                    wait_until = str(payload.get("wait_until") or "")
            except Exception:
                # The 429 itself is the fact that matters; a body that fails
                # to parse just means no wait_until to report, not a reason
                # to raise out of a fail-soft function.
                pass
            reason = "CourtListener rate-limited this request (HTTP 429)"
            if wait_until:
                reason += f"; retry after {wait_until}"
            log.warning("jeles legal_citations: %s", reason)
            return _empty(ok=False, configured=True, reason=reason)
        reason = f"CourtListener returned HTTP {exc.code}: {exc.reason}"
        log.warning("jeles legal_citations: %s", reason)
        return _empty(ok=False, configured=True, reason=reason)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        log.warning("jeles legal_citations: request failed: %s", reason)
        return _empty(ok=False, configured=True, reason=reason)

    try:
        items = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:
        reason = f"could not parse CourtListener's response: {type(exc).__name__}: {exc}"
        log.warning("jeles legal_citations: %s", reason)
        return _empty(ok=False, configured=True, reason=reason)

    if not isinstance(items, list):
        reason = "CourtListener's response was not the documented JSON array"
        log.warning("jeles legal_citations: %s", reason)
        return _empty(ok=False, configured=True, reason=reason)

    citations = [_record(item) for item in items if isinstance(item, dict)]
    matched_count = sum(1 for c in citations if c["matched"])
    return {
        "ok": True,
        "configured": True,
        "reason": "",
        "citations": citations,
        "count": len(citations),
        "matched_count": matched_count,
    }
