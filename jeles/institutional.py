"""institutional — the third hop: named institutional and academic collections.

The persona's mandate is "local KB → open web → special collections". The
corpus is the first hop and `reactions.search_adapter` is the second; this is
the third, and it is a genuinely different tier rather than more web.

**It runs locally by default.** :mod:`jeles.sources` holds the source functions
— arXiv, PubMed, Crossref, OpenAlex, Library of Congress, Europeana,
CourtListener, the Smithsonian — and this module is the thin layer that fans a
query across them and shapes the results like every other hit in the package.
No secret, no deployment, no second service to be up. The count is 61
registered, 60 of them in the default fan-out (`wikipedia` is opt-in); read it
off :data:`jeles.sources.SOURCES` rather than from prose, which drifts.

A hosted `jeles-remote` deployment stays available as an **opt-in delegate**:
set ``JELES_REMOTE_URL`` and ``JELES_REMOTE_SECRET`` and the fan-out happens
there instead. That is worth having when a caller would rather not make sixty
outbound connections from its own process — a CI runner, a phone, a sandbox
with a narrow egress allowlist. It is a convenience, never a prerequisite.

The two lanes run **separately deployed copies of the same code**, so they can
drift. Nothing here can detect that: `describe_remote` promises to make no
request, and a registry listing is the one thing it therefore cannot get from
the remote. So it does not pretend to — everything this module reports about
*which* collections exist is local knowledge, labelled as such
(``sources_lane``). What the remote actually reached comes back per-search in
``sources_queried``, which is the only trustworthy answer.

Design, matching the rest of the package:

* **Stdlib only**, and no socket at import. The thread pool behind the local
  fan-out is built on first use.
* **Legible failure**, like `search_adapter`. `search_institutional` returns
  ``{hits, ok, ...}`` so "the collections had nothing" and "we never reached
  them" stay distinguishable. `describe_remote` answers "which lane is this
  going to take, and can it work?" without making a request.
* **The perimeter is the operator's choice.** Both lanes do raw egress (through
  ``HTTPS_PROXY`` if set), exactly as `search_adapter` does. In a gated
  deployment, route through willow-mcp's `JelesAdapter` instead.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import urlparse

from jeles import _egress, sources

log = logging.getLogger("jeles.institutional")

_TIMEOUT = float(os.environ.get("JELES_REMOTE_TIMEOUT", "30"))
# The remote lane returns a fan-out across dozens of sources and can legitimately
# be large; cap it anyway, since it is untrusted input like any other response.
_MAX_BYTES = int(os.environ.get("JELES_REMOTE_MAX_BYTES", str(8 * 1024 * 1024)))
_UA = "jeles/institutional (+https://github.com/rudi193-cmd/Jeles)"


def _remote_base() -> str:
    return os.environ.get("JELES_REMOTE_URL", "").strip().rstrip("/")


def _remote_secret() -> str:
    return os.environ.get("JELES_REMOTE_SECRET", "").strip()


def describe_remote() -> dict[str, Any]:
    """Report which lane a search will take, without asking the network.

    Returns ``{lane, base_url, configured, requires, reason, sources,
    sources_lane}``.

    ``lane`` is ``"local"`` unless a remote is configured. ``configured`` is
    about the *chosen* lane: the local lane is always configured — that is the
    point of moving the collections in-package — so an unset remote is not a
    problem to report, just a lane not taken. A remote URL set *without* a
    secret is a real misconfiguration, though, because the service answers 401
    and a 401 reads exactly like an empty shelf.

    ``sources`` is **always the local registry**, and ``sources_lane`` says so.
    On the remote lane that is a different claim from the one a reader wants:
    the remote is a separately deployed copy that can drift, and this function
    promises to make no request, so it cannot know what the remote actually
    has. Reporting the local list unlabelled — as this did — presents local
    knowledge as fact about a service it has never spoken to. Per-search,
    ``search_institutional`` returns the remote's own ``sources_queried``,
    which is the answer that is actually about the remote.

    The secret is never included, only whether one was found.
    """
    base, secret = _remote_base(), _remote_secret()
    local_sources = sorted(sources.SOURCES)

    if not base:
        return {
            "lane": "local",
            "base_url": "",
            "configured": True,
            "requires": None,
            "reason": "",
            "sources": local_sources,
            "sources_lane": "local",
        }

    # On the remote lane the listing is local knowledge about a remote service.
    # Say which, in the payload, rather than in a docstring the caller is not
    # reading at the moment it matters.
    remote_caveat = (
        "`sources` is this package's own registry, not the remote's — "
        "describe_remote makes no request, and the two are separately deployed "
        "copies that can drift. Trust `sources_queried` from a search instead."
    )

    if not secret:
        return {
            "lane": "remote",
            "base_url": base,
            "configured": False,
            "requires": "JELES_REMOTE_SECRET",
            "reason": (
                "JELES_REMOTE_URL is set but JELES_REMOTE_SECRET is not, "
                "so the remote refuses every search with 401 — which is "
                "indistinguishable from the collections having nothing. "
                "Unset JELES_REMOTE_URL to use the in-package sources."
            ),
            "sources": local_sources,
            "sources_lane": "local",
        }

    return {
        "lane": "remote",
        "base_url": base,
        "configured": True,
        "requires": "JELES_REMOTE_SECRET",
        "reason": remote_caveat,
        "sources": local_sources,
        "sources_lane": "local",
    }


def to_hit(raw: dict[str, Any], idx: int = 0) -> dict[str, Any]:
    """Shape one source result like `corpus.to_search_hit` shapes a nugget, so
    all three hops merge into one ranked list without translation.

    `confidence` is ``"institutional"`` — deliberately its own rung between a
    corpus nugget's ``"verified"``/``"corroborated"`` and the open web's
    ``"unverified"``. A Library of Congress record is not a human-checked
    nugget and is not a random page; flattening it into either would throw away
    the only thing this hop is for.
    """
    url = str(raw.get("url") or "")
    try:
        host = urlparse(url).netloc or "institutional"
    except ValueError:
        host = "institutional"
    institution = str(raw.get("institution") or raw.get("source") or "").strip()
    return {
        "title": raw.get("title") or "",
        "url": url,
        "snippet": raw.get("snippet") or "",
        "source": institution or "Institutional source",
        "date": raw.get("date") or "",
        "source_id": "institutional",
        "hostname": host,
        "confidence": "institutional",
        "verification_kind": "institutional",
        "nugget_id": "",
        "verified_by": "",
        "verified_at": "",
        "extra_sources": [],
        "tags": [t for t in [raw.get("source")] if t],
        # Kept in the merge contract with `corpus.to_search_hit` — see the
        # comment above `_KIND_RANK` in corpus.py. An institutional record
        # never carries evidence of this kind, so it is always empty here.
        "evidence": {},
        "n": idx,
    }


def _flatten(grouped: dict[str, list]) -> list[dict[str, Any]]:
    """jeles-remote and `sources.search` both return results grouped by source
    id. Flatten, keeping the grouping as each hit's tag so a caller can still
    see which collection answered."""
    flat: list[dict[str, Any]] = []
    for source_id, items in (grouped or {}).items():
        for raw in items or []:
            raw = dict(raw)
            raw.setdefault("source", source_id)
            flat.append(raw)
    return flat


#: http stays allowed, as in `reactions.search_adapter` and unlike
#: `jeles.sources`, which is https-only. `JELES_REMOTE_URL` points wherever the
#: operator deployed jeles-remote, and a private-network or localhost
#: deployment over plain http is a legitimate one. The cost is named rather
#: than hidden: this request carries `X-Jeles-Secret`, so on a plain-http URL
#: — or after a hostile https -> http redirect, which this set cannot refuse —
#: the shared secret is on the wire in clear. Point `JELES_REMOTE_URL` at https
#: for anything off the host.
_ALLOWED_SCHEMES = _egress.HTTP_OR_HTTPS


def _post_remote(base: str, payload: dict, secret: str) -> Any:
    """POST to the remote delegate through the shared egress guard.

    The check here was a one-shot `url.startswith(...)` on the URL built from
    `JELES_REMOTE_URL`, which urllib walked straight past — it follows 3xx
    inside `urlopen`, so a redirect was never inspected, and a 302 to `ftp://`
    landed a TCP connection on the target (reproduced against a listening
    socket). `jeles._egress` re-checks every hop and installs no transport for
    file:/ftp:/data:. It matters more here than anywhere else in the package:
    this is the one request that carries a credential.
    """
    # `allow_private=True` for the same reason this lane allows plain http:
    # JELES_REMOTE_URL is an address the operator chose, and a deployment on a
    # private network is the sovereign case this package exists for. The
    # sources lane gets the opposite default, because nothing it queries has a
    # legitimate private address.
    raw = _egress.fetch(
        f"{base}/search",
        allowed=_ALLOWED_SCHEMES,
        timeout=_TIMEOUT,
        allow_private=True,
        max_bytes=_MAX_BYTES,
        data=json.dumps(payload).encode(),
        headers={
            "User-Agent": _UA,
            "Content-Type": "application/json",
            # A raw shared secret, not a Bearer token — jeles-remote
            # hmac-compares it (its main.py `_verify_secret`).
            "X-Jeles-Secret": secret,
        },
    )
    return json.loads(raw.decode("utf-8", "replace"))


# The keys `sources.search` grew when it learned to account for every source it
# dispatched. A payload without any of them came from an older build — which on
# the remote lane means a separately deployed service that has not been
# redeployed, a thing that will happen and that nothing else here can detect.
_ACCOUNTING_KEYS = ("skipped", "unknown", "timed_out")


def _verdict(data: dict[str, Any]) -> tuple[bool, str]:
    """Decide `ok` — "were we able to look?", not "did we find anything?".

    A source that was dispatched ends up in exactly one of `results`, `skipped`
    (abstained before egress, e.g. no API key), `failed` (tried, could not
    reach) or `timed_out`. Only the first two mean anything was learned, and
    only `results` means the shelf was actually read. So `ok` is: did at least
    one source complete a look?

    The old test was ``len(failed) >= len(sources_queried)``, and it could not
    fire in the default configuration. Six key-required sources sit in the
    default fan-out and abstained with a bare ``return []``, entering neither
    bucket, so the count never reached the total. Measured with all egress
    blocked: 60 queried, 55 failed, 5 vanished — ``ok: true, total: 0, error:
    ""``. A sandbox with no network reported the collections as empty.
    """
    queried = list(data.get("sources_queried") or [])
    results = data.get("results") or {}
    failed = dict(data.get("failed") or {})
    skipped = dict(data.get("skipped") or {})
    timed_out = list(data.get("timed_out") or [])

    if not queried:
        return False, (
            "no source was dispatched — every requested id was "
            "unknown, or the filter selected nothing. This is a "
            "configuration answer, not a search result."
        )

    def _sample(items: Any) -> str:
        pairs = items.items() if isinstance(items, dict) else ((i, "") for i in items)
        return "; ".join(f"{k}: {v}" if v else str(k) for k, v in list(pairs)[:3])

    if not any(k in data for k in _ACCOUNTING_KEYS):
        # Legacy payload: abstentions are invisible, so "looked and found
        # nothing" cannot be told from "never got out of the process". Resolve
        # the ambiguity toward *not* claiming an empty shelf — that is the error
        # this whole module exists to avoid — and name the skew, because the fix
        # is to redeploy the remote rather than to retry the query.
        if results or not failed:
            return True, ""
        return False, (
            f"{len(failed)} of {len(queried)} sources failed and none returned "
            "anything. This response predates per-source accounting (no "
            "`skipped`/`timed_out`), so an abstention cannot be told from a "
            f"successful empty look — treating it as an outage. {_sample(failed)}"
        )

    looked = [
        sid for sid in queried if sid not in failed and sid not in skipped and sid not in timed_out
    ]
    if looked:
        return True, ""

    parts = []
    if failed:
        parts.append(f"{len(failed)} could not be reached ({_sample(failed)})")
    if skipped:
        parts.append(f"{len(skipped)} abstained ({_sample(skipped)})")
    if timed_out:
        parts.append(f"{len(timed_out)} timed out ({_sample(timed_out)})")
    return False, (
        f"not one of {len(queried)} sources completed a look — "
        + ", ".join(parts)
        + ". This is an outage, a missing key, or a blocked egress, not an "
        "empty result."
    )


def list_sources() -> list[dict[str, Any]]:
    """The registered collections: ``[{id, name, key_required, opt_in}, ...]``.

    **Local knowledge**, and no request. On the remote lane that is a weaker
    claim than it looks: the remote runs a separately deployed copy of the same
    registry and can drift from this one. `describe_remote()["sources_lane"]`
    carries the same caveat in machine-readable form.

    `key_required` sources are in the default fan-out and abstain when their key
    is absent, so this is how a caller finds out what it is *not* reaching.
    """
    return [
        {
            "id": sid,
            "name": cfg.get("name", sid),
            "key_required": bool(cfg.get("key_required", False)),
            # Naming the variable is the actionable half. "this source needs a key"
            # sends a caller reading source code; "set EUROPEANA_KEY" does not.
            "key_env": cfg.get("key_env") or "",
            "opt_in": bool(cfg.get("opt_in", False)),
        }
        for sid, cfg in sorted(sources.SOURCES.items())
    ]


def search_institutional(
    query: str,
    *,
    sources_filter: list[str] | None = None,
    limit_per_source: int = 3,
) -> dict[str, Any]:
    """Search the institutional collections.

    Runs the in-package fan-out unless ``JELES_REMOTE_URL`` is set, in which
    case it delegates to that deployment. Returns ``{hits, ok, lane,
    sources_queried, failed, skipped, timed_out, unknown, total, error}``.

    Read `ok` before reading `hits`: ``ok`` true with no hits means the
    collections had nothing, ``ok`` false means no source completed a look —
    an outage, a blocked egress, or every key-required source abstaining.
    Each dispatched source appears in exactly one of `results`, `skipped`,
    `failed` and `timed_out`, which is what makes that distinction decidable;
    `unknown` holds requested ids that are not in the registry and so were
    never dispatched at all.

    ``sources_filter`` narrows the fan-out to specific registered ids; omit it
    for every non-opt-in source.

    Never raises. A failed hop yields no hits and an explanation, never a
    forged result.
    """
    info = describe_remote()
    lane = info["lane"]

    if lane == "remote" and not info["configured"]:
        return {
            "hits": [],
            "ok": False,
            "lane": lane,
            "sources_queried": [],
            "failed": [],
            "total": 0,
            "error": info["reason"],
        }

    try:
        if lane == "remote":
            payload: dict[str, Any] = {"query": query, "limit_per_source": limit_per_source}
            if sources_filter:
                payload["sources"] = sources_filter
            data = _post_remote(info["base_url"], payload, _remote_secret())
        else:
            data = sources.search(query, sources=sources_filter, limit_per_source=limit_per_source)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("institutional search (%s lane) failed for %r: %s", lane, query, detail)
        return {
            "hits": [],
            "ok": False,
            "lane": lane,
            "sources_queried": [],
            "failed": [],
            "total": 0,
            "error": detail,
        }

    flat = _flatten(data.get("results") or {})
    hits = [to_hit(raw, i) for i, raw in enumerate(flat)]
    ok, error = _verdict(data)

    return {
        "hits": hits,
        "ok": ok,
        "lane": lane,
        "sources_queried": list(data.get("sources_queried") or []),
        # Every dispatched source lands in exactly one of these, so a caller can
        # tell an outage from a missing key from a slow endpoint — and a source
        # can no longer disappear between the request and the report.
        "failed": sorted(data.get("failed") or {}),
        "skipped": dict(data.get("skipped") or {}),
        "timed_out": sorted(data.get("timed_out") or []),
        "unknown": sorted(data.get("unknown") or []),
        "total": data.get("total", len(hits)),
        "error": error,
    }
