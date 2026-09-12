"""search_adapter — the real web-search edge for conflict_scan (v1).

conflict_scan.react() never imports a search backend; it takes an injected
``searcher: (query) -> [{title, url, snippet}]``. This module is the default
implementation of that edge — the one impure, network-touching piece — kept
deliberately separate so the reaction's routing stays pure and offline-testable.

Design choices, all in the box's grain:

* **Stdlib only.** urllib.request, json, no third-party HTTP client — the
  dependency-less goal. (Importing this module opens no socket; the network
  happens only when the returned searcher is *called*.)
* **Backend-pluggable, fail-soft.** ``JELES_SEARCH_BACKEND`` selects
  ``searxng`` (sovereign, self-hosted JSON — the preferred default when a URL
  is set), ``brave`` or ``tavily`` (keyed APIs), or ``ddg`` (keyless,
  best-effort HTML). Any network/parse error yields ``[]``, which conflict_scan
  already treats as "no witness → contested gap" — a failed search never forges
  corroboration.
* **Fail-soft, not silent.** Returning ``[]`` for an unset API key, an
  unreachable host, a 403 and a genuinely empty result gave one symptom —
  "nothing found" — four causes, and no way to tell which. The swallowing stays
  (corroboration depends on a failed search yielding no witness), but failures
  now log at WARNING, an unconfigured or shallow backend says so on first use,
  and :func:`search_with_status` returns the reason as data for callers that
  need to act on it. :func:`describe_backend` answers "can this even work?"
  without making a request.
* **The perimeter is the operator's choice.** This default does *raw* egress
  (through ``HTTPS_PROXY`` if the environment sets one). In a gated deployment,
  don't use it — inject a searcher that routes through willow-mcp's three-key
  egress instead. The whole point of the injected-searcher seam is that swapping
  this out requires no change to the reaction. (See the 2026-07-24 red-team:
  raw egress from inside a reaction is exactly the "correct code, wrong
  perimeter" trap; this module names it rather than hiding it.)
"""

from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from jeles import _egress

log = logging.getLogger("jeles.search")

Searcher = Callable[[str], list[dict[str, Any]]]

_UA = "jeles-conflict-scan/1.0 (+local-first; https://github.com/rudi193-cmd/Jeles)"
_TIMEOUT = float(os.environ.get("JELES_SEARCH_TIMEOUT", "12"))
_MAX = int(os.environ.get("JELES_SEARCH_MAX", "8"))
# Cap the response body: a misbehaving/redirected endpoint could otherwise
# stream an unbounded body into memory before parse (this component is fail-soft
# and never trusted, so a big body should fail, not OOM).
_MAX_BYTES = int(os.environ.get("JELES_SEARCH_MAX_BYTES", str(4 * 1024 * 1024)))


#: http stays allowed here, unlike `jeles.sources`, which is https-only. The
#: sovereign default this module is built around is a SearXNG instance the
#: operator runs themselves — `JELES_SEARXNG_URL=http://127.0.0.1:8888` is the
#: documented zero-config setup — and refusing plain http would break exactly
#: the self-hosted case. The cost is real and worth naming: on this lane a
#: hostile redirect can still downgrade https -> http. Set the backend to a
#: keyed API over TLS if a deployment cannot afford that.
_ALLOWED_SCHEMES = _egress.HTTP_OR_HTTPS


def _get_json(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    allow_private: bool = False,
    data: bytes | None = None,
) -> Any:
    """Fetch and decode JSON through the shared egress guard.

    The guard was previously a one-shot `url.startswith(("https://", "http://"))`
    here, which urllib then walked straight past: it follows 3xx *inside*
    `urlopen`, so a redirect to `ftp://` was never inspected — reproduced
    against a listening socket, the connection arrived. `jeles._egress` re-checks
    the scheme on every hop and gives file:/ftp:/data: no transport at all.

    `allow_private` is a PER-CALL argument, not a property of this function.
    It used to be hardcoded True here, justified by the SearXNG default — but
    this is also the fetch path for Brave, Tavily and DuckDuckGo, three
    hardcoded public APIs with no claim to a private address. Measured: with it
    on, a 302 from `api.duckduckgo.com` to
    `http://169.254.169.254/latest/meta-data/` was followed. One operator-chosen
    backend was buying every other backend an exemption.
    """
    raw = _egress.fetch(
        url,
        allowed=_ALLOWED_SCHEMES,
        timeout=_TIMEOUT,
        max_bytes=_MAX_BYTES,
        data=data,
        allow_private=allow_private,
        headers={"User-Agent": _UA, **(headers or {})},
    )
    return json.loads(raw.decode("utf-8", "replace"))


def _hit(title: Any, url: Any, snippet: Any) -> dict[str, Any]:
    return {
        "title": str(title or "").strip(),
        "url": str(url or "").strip(),
        "snippet": str(snippet or "").strip(),
    }


# ── Backends: each is (query) -> [ {title,url,snippet}, ... ], raising on failure
#    (the make_searcher wrapper turns any raise into a fail-soft []). ──────────


def _searxng(query: str) -> list[dict[str, Any]]:
    """A self-hosted SearXNG instance's JSON API — the sovereign default.
    Set JELES_SEARXNG_URL (e.g. http://127.0.0.1:8888)."""
    base = os.environ.get("JELES_SEARXNG_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("JELES_SEARXNG_URL not set")
    qs = urllib.parse.urlencode({"q": query, "format": "json", "safesearch": "0"})
    # The one lane with a claim to a private address: the operator set
    # JELES_SEARXNG_URL themselves, and http://127.0.0.1:8888 is the
    # documented zero-config default.
    data = _get_json(f"{base}/search?{qs}", allow_private=True)
    return [
        _hit(r.get("title"), r.get("url"), r.get("content"))
        for r in (data.get("results") or [])[:_MAX]
    ]


def _brave(query: str) -> list[dict[str, Any]]:
    """Brave Search API. Set BRAVE_API_KEY."""
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        raise RuntimeError("BRAVE_API_KEY not set")
    qs = urllib.parse.urlencode({"q": query, "count": _MAX})
    data = _get_json(
        f"https://api.search.brave.com/res/v1/web/search?{qs}",
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )
    results = ((data.get("web") or {}).get("results")) or []
    return [_hit(r.get("title"), r.get("url"), r.get("description")) for r in results[:_MAX]]


def _tavily(query: str) -> list[dict[str, Any]]:
    """Tavily search API. Set TAVILY_API_KEY."""
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        raise RuntimeError("TAVILY_API_KEY not set")
    body = json.dumps({"api_key": key, "query": query, "max_results": _MAX}).encode()
    data = _get_json(
        "https://api.tavily.com/search", headers={"Content-Type": "application/json"}, data=body
    )
    return [
        _hit(r.get("title"), r.get("url"), r.get("content"))
        for r in (data.get("results") or [])[:_MAX]
    ]


# ── ddg: the DuckDuckGo HTML SERP, ported from willow-2.0's core/web_search.py
#    (SearchError/TransientSearchError/HardBlockError, the parser, and the
#    per-backend circuit breaker) and rewritten onto `_egress` — the upstream
#    module did its own raw `requests` egress, which is exactly the dependency
#    and the unguarded-redirect exposure this package's egress guard exists to
#    close. See `_ddg_fetch` for how the wiring differs.
#
#    This *replaces* the old `_ddg`, which hit the Instant-Answer JSON endpoint
#    (`api.duckduckgo.com`) and was permanently marked shallow in `_SHALLOW`:
#    that endpoint returns related topics, not a result page, so it could never
#    corroborate a claim regardless of how well it was wired up — it was a
#    placeholder, not a search engine, and the module docstring already
#    described `ddg` as "keyless, best-effort HTML" while the code underneath
#    it did not deliver that. Keeping both under different names would mean two
#    ways to spell "no config needed" with no way to tell them apart from
#    `JELES_SEARCH_BACKEND=ddg` alone; keeping only the Instant-Answer one and
#    adding the scraper as a third choice would leave the zero-config default
#    permanently shallow. Replacing is the one move that fixes what `ddg`
#    actually does for the callers who already select it by name. ────────────


class SearchError(Exception):
    """Base class for DDG-backend search failures."""


class TransientSearchError(SearchError):
    """Retryable failure — rate limit, 5xx, connection error, timeout."""


class HardBlockError(SearchError):
    """Non-retryable block (403/407) — retrying the same path won't help."""


_DDG_URL = "https://html.duckduckgo.com/html/"
_LINK_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SNIP_RE = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</(?:a|td|span|div)>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")

# HTTP status classification for retry vs. hard-block decisions.
_RETRYABLE_STATUS = frozenset({429, 503, 504})
_HARD_BLOCK_STATUS = frozenset({403, 407})

# Below this body size a 200-OK page with 0 parsed links is treated as a
# genuine empty/blocked response, not a structure change. A real DDG results
# page is tens of KB; a "no results"/interstitial page is small.
_PARSER_MISS_MIN_BODY = 2000


def _strip_tags(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def _unwrap_ddg(href: str) -> str:
    """DDG's HTML wraps outbound links in a redirector (`/l/?uddg=<encoded>`);
    unwrap it so the adapter returns the real destination, not duckduckgo.com."""
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if qs.get("uddg"):
                return urllib.parse.unquote(qs["uddg"][0])
        except Exception:
            pass
    return href


def _parse_ddg_html(text: str, max_results: int) -> list[dict[str, Any]]:
    """Parse a DuckDuckGo HTML SERP into hit dicts. Regex, not an HTML parser —
    stdlib has none, and pulling one in would break the zero-dependency goal
    for two fixed CSS-class anchors. `_looks_like_results_page` is the
    compensating control: if DDG's markup drifts enough that these patterns
    stop matching, a substantial body with 0 parsed links is distinguishable
    from a genuinely empty result set, and logged rather than silently eaten."""
    links = _LINK_RE.findall(text)
    snippets = _SNIP_RE.findall(text)
    hits: list[dict[str, Any]] = []
    for idx, (href, title_html) in enumerate(links[: max_results + 4]):
        url = _unwrap_ddg(href)
        if not url or "duckduckgo.com" in url:
            continue
        title = _strip_tags(title_html) or url
        snippet = _strip_tags(snippets[idx]) if idx < len(snippets) else ""
        hits.append(_hit(title[:200], url, snippet[:400]))
        if len(hits) >= max_results:
            break
    return hits


def _looks_like_results_page(html_text: str) -> bool:
    """Heuristic: did DDG return a substantial results-style page (vs. an empty
    or interstitial one)? Used to flag a parser miss as likely HTML drift
    rather than a legitimately empty result set."""
    body = html_text or ""
    if len(body) < _PARSER_MISS_MIN_BODY:
        return False
    return "result" in body.lower()


def _ddg_fetch(query: str, max_results: int = 8) -> list[dict[str, Any]]:
    """Fetch + parse the DuckDuckGo HTML SERP, raising typed errors on failure.

    Raises TransientSearchError (retryable) for timeouts, connection errors,
    and 429/503/504; HardBlockError for 403/407; SearchError for other
    failures. `_ddg_html` wraps this with retry + the circuit breaker; nothing
    here swallows.

    Ported from willow-2.0's `_ddg_fetch`, which posted via `requests` — a
    third-party dependency this package does not carry, and a call that
    bypassed `jeles._egress` entirely (no scheme re-check on redirect hops, no
    body cap, no private-destination guard). Routing through `_egress.fetch`
    closes both: the POST body and headers are unchanged, only the transport
    is. `_egress.fetch` raises `urllib.error.HTTPError`/`URLError` instead of
    `requests`' exception hierarchy, so the classification below is rewritten
    against those, not ported verbatim.
    """
    q = query.strip()
    if not q:
        return []
    body = urllib.parse.urlencode({"q": q, "b": "", "kl": "us-en"}).encode()
    try:
        raw = _egress.fetch(
            _DDG_URL,
            allowed=_ALLOWED_SCHEMES,
            timeout=_TIMEOUT,
            max_bytes=_MAX_BYTES,
            data=body,
            # DuckDuckGo is a hardcoded public API with no claim to a private
            # address — explicit, like `_get_json`'s per-call argument, not a
            # default this call could silently inherit from another backend.
            allow_private=False,
            headers={
                "User-Agent": _UA,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status in _HARD_BLOCK_STATUS:
            raise HardBlockError(f"hard block (HTTP {status})") from exc
        if status in _RETRYABLE_STATUS:
            raise TransientSearchError(f"retryable (HTTP {status})") from exc
        raise SearchError(f"HTTP {status}") from exc
    except TimeoutError as exc:
        raise TransientSearchError(f"timeout: {exc}") from exc
    except urllib.error.URLError as exc:
        raise TransientSearchError(f"connection error: {exc}") from exc
    except OSError as exc:
        # `URLError` is itself an `OSError` subclass and is what the real
        # opener wraps a socket failure in — this clause is the defensive
        # fallback for a raw `OSError` that reaches here some other way (a
        # lower-level handler change, a stub in a test) so it still counts as
        # transient rather than escaping the classification entirely, which
        # would silently stop the circuit breaker from ever seeing it.
        raise TransientSearchError(f"connection error: {exc}") from exc
    except ValueError as exc:
        # `_egress.check_url`'s scheme/private-destination refusal, or
        # `read_capped`'s body-size cap — a structural refusal, not something
        # a retry would fix.
        raise SearchError(str(exc)) from exc

    text = raw.decode("utf-8", "replace")
    hits = _parse_ddg_html(text, max_results)
    if not hits and _looks_like_results_page(text):
        log.warning(
            "ddg parser miss — HTTP 200, %d-byte results-like body, 0 links "
            "parsed; DDG HTML structure may have changed (_LINK_RE)",
            len(text),
        )
    return hits


def ddg_html_search(query: str, max_results: int = 8) -> list[dict[str, Any]]:
    """Fetch DuckDuckGo HTML results directly (no API key, no circuit breaker).

    Back-compat / direct-use surface: never raises — returns ``[]`` on any
    error, same contract as `ddg_html_search` in willow-2.0. The registered
    `ddg` backend (`_ddg_html`, below) calls `_ddg_fetch` through retry + the
    circuit breaker instead, so it can see and act on typed failures; this
    function is for a caller that wants DDG specifically without that
    machinery.
    """
    try:
        return _ddg_fetch(query, max_results=max_results)
    except SearchError as exc:
        log.warning("ddg search failed: %s", exc)
        return []
    except Exception as exc:  # pragma: no cover - defensive catch-all
        log.warning("ddg search failed: %s", exc)
        return []


# ── Retry + circuit breaker, ported from willow-2.0's core/web_search.py. ────
#
# The DDG HTML endpoint is the one backend in this module that is neither a
# sovereign self-hosted service (searxng) nor a keyed, contractual API (brave,
# tavily) — it is an unofficial scrape of a page DuckDuckGo can rate-limit,
# block, or reshape without notice. A single failed attempt is worth retrying
# (a 503 or a timeout is often transient); a *run* of failures is a signal to
# stop trying for a while rather than hammering an endpoint that has decided
# to block this egress path — both for DDG's sake and so a contested claim
# doesn't sit blocked on a slow, doomed retry loop. `CircuitBreaker` is that
# stop; `_with_retry` is the bounded retry underneath a single breaker-gated
# attempt.


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _retry_config() -> dict[str, float]:
    return {
        "max_attempts": _env_int("JELES_SEARCH_MAX_ATTEMPTS", 3),
        "budget": _env_float("JELES_SEARCH_RETRY_BUDGET", 15.0),
        "base_backoff": _env_float("JELES_SEARCH_BACKOFF_BASE", 1.0),
    }


def _with_retry(
    fn,
    *,
    max_attempts: int | None = None,
    budget: float | None = None,
    base_backoff: float | None = None,
    sleep=time.sleep,
    clock=time.monotonic,
):
    """Call `fn`, retrying on TransientSearchError with exponential backoff.

    Backoff is jittered (delay in [d, 2d] where d = base * 2**(attempt-1)) and
    the whole sequence is capped by a total time budget. HardBlockError and any
    other exception propagate immediately — only transient errors are retried.
    """
    cfg = _retry_config()
    max_attempts = int(cfg["max_attempts"] if max_attempts is None else max_attempts)
    budget = cfg["budget"] if budget is None else budget
    base = cfg["base_backoff"] if base_backoff is None else base_backoff
    start = clock()
    last_exc: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            return fn()
        except TransientSearchError as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            d = base * (2 ** (attempt - 1))
            delay = random.uniform(d, 2 * d)
            if (clock() - start) + delay > budget:
                log.info("ddg search retry budget exhausted after attempt %d: %s", attempt, exc)
                break
            log.info("ddg search retry %d/%d in %.1fs: %s", attempt, max_attempts, delay, exc)
            sleep(delay)
    raise last_exc if last_exc is not None else SearchError("retry exhausted")


class CircuitBreaker:
    """Per-backend circuit breaker: CLOSED -> OPEN -> HALF_OPEN.

    Trips OPEN after `fail_threshold` consecutive failures and fast-fails for a
    cooldown that doubles each time a half-open probe fails (capped at
    `max_cooldown`). A success resets it fully.
    """

    def __init__(
        self,
        fail_threshold: int = 5,
        base_cooldown: float = 30.0,
        max_cooldown: float = 300.0,
        clock=time.monotonic,
    ) -> None:
        self._threshold = fail_threshold
        self._base_cooldown = base_cooldown
        self._max_cooldown = max_cooldown
        self._clock = clock
        self.state = "CLOSED"
        self._failures = 0
        self._opened_at: float | None = None
        self._cooldown = base_cooldown

    def allow(self) -> bool:
        """Whether a request may proceed now."""
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if self._opened_at is not None and (self._clock() - self._opened_at) >= self._cooldown:
                self.state = "HALF_OPEN"
                return True
            return False
        return True  # HALF_OPEN — allow the single probe

    def record_success(self) -> None:
        self.state = "CLOSED"
        self._failures = 0
        self._opened_at = None
        self._cooldown = self._base_cooldown

    def record_failure(self) -> None:
        if self.state == "HALF_OPEN":
            # Probe failed — reopen with a longer cooldown.
            self._cooldown = min(self._cooldown * 2, self._max_cooldown)
            self.state = "OPEN"
            self._opened_at = self._clock()
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self.state = "OPEN"
            self._opened_at = self._clock()


_BREAKERS: dict[str, CircuitBreaker] = {}


def _get_breaker(name: str) -> CircuitBreaker:
    cb = _BREAKERS.get(name)
    if cb is None:
        cb = CircuitBreaker(
            fail_threshold=_env_int("JELES_SEARCH_CB_THRESHOLD", 5),
            base_cooldown=_env_float("JELES_SEARCH_CB_COOLDOWN", 30.0),
            max_cooldown=_env_float("JELES_SEARCH_CB_MAX_COOLDOWN", 300.0),
        )
        _BREAKERS[name] = cb
    return cb


def reset_circuit_breakers() -> None:
    """Clear all circuit-breaker state (test helper / operator reset)."""
    _BREAKERS.clear()


def _ddg_html(query: str) -> list[dict[str, Any]]:
    """The registered `ddg` backend: DuckDuckGo HTML SERP, guarded by retry and
    a circuit breaker, through `_egress`.

    A breaker OPEN raises `SearchError` immediately — no network call — so a
    run of failures degrades to the same fail-soft `[]` as any other error
    (via `make_searcher`/`search_with_status`) without adding latency or load
    on an endpoint that has already shown it is down or blocking this egress
    path. `record_success`/`record_failure` bracket one *whole* breaker-gated
    attempt (i.e. after `_with_retry` has already exhausted its own bounded
    retries), matching willow-2.0's per-provider chain: retry recovers a
    transient blip, the breaker responds to a sustained one.
    """
    breaker = _get_breaker("ddg")
    if not breaker.allow():
        raise SearchError(
            "ddg circuit breaker open — too many recent failures; skipping until cooldown elapses"
        )
    try:
        hits = _with_retry(lambda: _ddg_fetch(query, max_results=_MAX))
    except SearchError:
        breaker.record_failure()
        raise
    breaker.record_success()
    return hits


_BACKENDS: dict[str, Searcher] = {
    "searxng": _searxng,
    "brave": _brave,
    "tavily": _tavily,
    "ddg": _ddg_html,
}


def _default_backend_name() -> str:
    name = os.environ.get("JELES_SEARCH_BACKEND", "").strip().lower()
    if name:
        return name
    # Zero config: prefer a configured SearXNG, else fall back to keyless DDG.
    return "searxng" if os.environ.get("JELES_SEARXNG_URL") else "ddg"


# Which env var makes each backend usable, and whether it can answer properly
# once it is. `ddg` needs no configuration, which is why it is the zero-config
# fallback — it used to also be permanently `_SHALLOW` (the old Instant-Answer
# endpoint returned related topics, not a result page). Since the HTML-SERP
# rewrite it scrapes real results, so it is no longer marked shallow; the set
# stays as the mechanism for the next backend that *is* a placeholder, so
# describe_backend() can say so rather than letting a caller infer depth from
# the absence of an error.
_REQUIRES: dict[str, str | None] = {
    "searxng": "JELES_SEARXNG_URL",
    "brave": "BRAVE_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "ddg": None,
}
_SHALLOW: frozenset[str] = frozenset()


def describe_backend(backend: str | None = None) -> dict[str, Any]:
    """Report which backend is selected and whether it can actually answer.

    The blind spot this exists for: ``make_searcher`` returns ``[]`` for an
    unset API key, an unreachable host, a 403 and a genuinely empty result
    alike. Downstream that is one symptom — "nothing found" — with four causes,
    three of which are configuration. Ask this before believing a silence.

    Returns ``{backend, configured, shallow, requires, reason}``. ``reason`` is
    empty when the backend is configured and capable.
    """
    name = (backend or _default_backend_name()).lower()
    if name not in _BACKENDS:
        return {
            "backend": name,
            "configured": False,
            "shallow": False,
            "requires": None,
            "reason": f"unknown backend {name!r}; choose one of {sorted(_BACKENDS)}",
        }

    needs = _REQUIRES[name]
    configured = bool(os.environ.get(needs)) if needs else True
    shallow = name in _SHALLOW

    if not configured:
        reason = (
            f"{needs} is not set, so every search returns no results — "
            f"which is indistinguishable from finding nothing"
        )
    elif shallow:
        reason = (
            f"{name} is zero-config but too shallow to corroborate a "
            f"claim; set JELES_SEARXNG_URL (or a BRAVE_API_KEY / "
            f"TAVILY_API_KEY) for real depth"
        )
    else:
        reason = ""

    return {
        "backend": name,
        "configured": configured,
        "shallow": shallow,
        "requires": needs,
        "reason": reason,
    }


def search_with_status(query: str, backend: str | None = None) -> dict[str, Any]:
    """``search()`` that reports *why* it returned what it did.

    Same network call as the searcher, but the outcome is legible:
    ``{hits, ok, backend, shallow, error}``. ``ok`` false with an ``error``
    means the search failed; ``ok`` true with no hits means the web genuinely
    had nothing. Those are different facts and a caller should be able to tell
    them apart — the whole point of this module's second pass.
    """
    info = describe_backend(backend)
    name = info["backend"]
    fn = _BACKENDS.get(name)
    if fn is None:
        return {"hits": [], "ok": False, "backend": name, "shallow": False, "error": info["reason"]}
    try:
        hits = fn(query)
    except Exception as exc:
        # Include the configuration reason when there is one: "BRAVE_API_KEY is
        # not set" is a far more useful error than the KeyError it produces.
        detail = f"{type(exc).__name__}: {exc}"
        return {
            "hits": [],
            "ok": False,
            "backend": name,
            "shallow": info["shallow"],
            "error": f"{info['reason']} ({detail})" if info["reason"] else detail,
        }
    return {"hits": hits, "ok": True, "backend": name, "shallow": info["shallow"], "error": ""}


def make_searcher(backend: str | None = None) -> Searcher:
    """Return a fail-soft ``(query) -> [hits]`` ready to hand to
    ``conflict_scan.react(..., searcher=make_searcher())``.

    ``backend`` overrides ``JELES_SEARCH_BACKEND``. Any error inside the backend
    (unset key, network failure, unparseable response) is swallowed to ``[]`` —
    a failed search yields no witnesses, never a false one.

    The swallowing stays, because conflict_scan's corroboration rule depends on
    it: a failed search must not be able to forge a witness. What changes is
    that it is no longer *silent* — failures log at WARNING, and an unconfigured
    or shallow backend logs once at first use. Use :func:`search_with_status`
    when the caller needs the reason as data rather than as a log line.
    """
    name = (backend or _default_backend_name()).lower()
    fn = _BACKENDS.get(name)
    if fn is None:
        raise ValueError(f"unknown search backend {name!r}; choose one of {sorted(_BACKENDS)}")

    info = describe_backend(name)
    warned = False

    def search(query: str) -> list[dict[str, Any]]:
        nonlocal warned
        if info["reason"] and not warned:
            warned = True
            log.warning("jeles search backend %r: %s", name, info["reason"])
        try:
            return fn(query)
        except Exception as exc:
            # fail-soft: conflict_scan reads [] as "no witness". Say so anyway —
            # an empty result that is really a broken backend is the failure
            # mode this module was hardest to debug for.
            log.warning(
                "jeles search via %r failed for %r: %s: %s", name, query, type(exc).__name__, exc
            )
            return []

    return search
