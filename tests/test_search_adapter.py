"""search_adapter — the real web-search edge, tested with a stubbed urlopen.

No network: every test replaces urllib.request.urlopen with a canned response,
so we verify the JSON→contract mapping and the fail-soft guarantee offline.
"""

import io
import json
import urllib.error
import urllib.parse
from urllib.parse import urlparse

import pytest

from jeles.reactions import search_adapter as sa


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _stub_urlopen(monkeypatch, payload, *, capture=None):
    """Make urlopen return `payload` (dict→json bytes, or an Exception to raise)."""

    def fake(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["headers"] = dict(req.header_items())
            capture["data"] = req.data
        if isinstance(payload, Exception):
            raise payload
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr(sa.urllib.request, "urlopen", fake)


def test_searxng_maps_results(monkeypatch):
    monkeypatch.setenv("JELES_SEARXNG_URL", "http://127.0.0.1:8888")
    cap = {}
    _stub_urlopen(
        monkeypatch,
        {
            "results": [
                {
                    "title": "OPA bundles",
                    "url": "https://openpolicyagent.org/x",
                    "content": "signed",
                },
                {"title": "Cedar", "url": "https://cedarpolicy.com/y", "content": "deterministic"},
            ]
        },
        capture=cap,
    )
    hits = sa.make_searcher("searxng")("signed policy registry")
    assert [h["url"] for h in hits] == [
        "https://openpolicyagent.org/x",
        "https://cedarpolicy.com/y",
    ]
    assert hits[0]["snippet"] == "signed"  # content -> snippet
    assert "format=json" in cap["url"] and "127.0.0.1:8888" in cap["url"]


def test_brave_maps_nested_results_and_sends_key(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "secret-key")
    cap = {}
    _stub_urlopen(
        monkeypatch,
        {
            "web": {
                "results": [
                    {"title": "t", "url": "https://a.org/1", "description": "d"},
                ]
            }
        },
        capture=cap,
    )
    hits = sa.make_searcher("brave")("q")
    assert hits == [{"title": "t", "url": "https://a.org/1", "snippet": "d"}]
    # The key rides in a header, not the URL (urllib title-cases header names,
    # so match case-insensitively — HTTP headers are case-insensitive anyway).
    lc = {k.lower(): v for k, v in cap["headers"].items()}
    assert lc.get("x-subscription-token") == "secret-key"
    assert "secret-key" not in cap["url"]


def test_tavily_posts_body(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tv-key")
    cap = {}
    _stub_urlopen(
        monkeypatch,
        {
            "results": [
                {"title": "t", "url": "https://b.org/2", "content": "c"},
            ]
        },
        capture=cap,
    )
    hits = sa.make_searcher("tavily")("q")
    assert hits[0]["url"] == "https://b.org/2"
    assert json.loads(cap["data"])["query"] == "q"  # POSTed, not in querystring


def test_backend_that_fails_is_soft_empty(monkeypatch):
    monkeypatch.setenv("JELES_SEARXNG_URL", "http://127.0.0.1:8888")
    _stub_urlopen(monkeypatch, OSError("connection refused"))
    assert sa.make_searcher("searxng")("q") == []  # never raises


def test_missing_key_is_soft_empty(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    # No urlopen stub needed: the backend raises before any network call.
    assert sa.make_searcher("brave")("q") == []


def test_default_backend_prefers_searxng_when_url_set(monkeypatch):
    monkeypatch.delenv("JELES_SEARCH_BACKEND", raising=False)
    monkeypatch.setenv("JELES_SEARXNG_URL", "http://127.0.0.1:8888")
    assert sa._default_backend_name() == "searxng"
    monkeypatch.delenv("JELES_SEARXNG_URL", raising=False)
    assert sa._default_backend_name() == "ddg"  # keyless fallback


def test_unknown_backend_raises_at_construction(monkeypatch):
    with pytest.raises(ValueError):
        sa.make_searcher("altavista")


def test_end_to_end_react_with_stubbed_adapter(monkeypatch):
    """The adapter feeds react() exactly like the real thing — two independent
    domains from a stubbed SearXNG corroborate into a proposed nugget."""
    from jeles.reactions import conflict_scan as cs

    monkeypatch.setenv("JELES_SEARXNG_URL", "http://127.0.0.1:8888")
    _stub_urlopen(
        monkeypatch,
        {
            "results": [
                {
                    "title": "OPA signed bundles",
                    "url": "https://openpolicyagent.org/a",
                    "content": "a signed registry of reaction bundles",
                },
                {
                    "title": "Oso policy registry",
                    "url": "https://osohq.com/b",
                    "content": "signed reaction registry prior art",
                },
            ]
        },
    )
    proposals = cs.react(
        {"claim": "signed reaction registry"}, searcher=sa.make_searcher("searxng")
    )
    assert proposals[0]["driver"] == "put_nugget"
    assert proposals[0]["args"]["verified_by"] == cs.WITNESS


# ── Legibility: telling "found nothing" apart from "could not look" ──────────
#
# Every failure used to return [] with no logging, so an unset key, an
# unreachable host, a 403 and a genuinely empty result were one symptom with
# four causes. These pin the difference.


def test_describe_backend_flags_an_unconfigured_backend(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    info = sa.describe_backend("brave")
    assert info["configured"] is False
    assert info["requires"] == "BRAVE_API_KEY"
    assert "BRAVE_API_KEY" in info["reason"]


def test_describe_backend_no_longer_flags_ddg_as_shallow(monkeypatch):
    """ddg used to be a permanent trap: zero-config and healthy-looking, but
    wired to the Instant-Answer endpoint, which cannot corroborate anything.
    Since the HTML-SERP rewrite it is a real scrape, so describe_backend must
    say so rather than continuing to warn about a problem that was fixed."""
    info = sa.describe_backend("ddg")
    assert info["configured"] is True
    assert info["shallow"] is False
    assert info["reason"] == ""


def test_describe_backend_is_clean_when_properly_configured(monkeypatch):
    monkeypatch.setenv("JELES_SEARXNG_URL", "http://127.0.0.1:8888")
    info = sa.describe_backend("searxng")
    assert (info["configured"], info["shallow"], info["reason"]) == (True, False, "")


def test_describe_backend_makes_no_request(monkeypatch):
    """It answers "can this even work?" — asking must not cost a round trip."""

    def explode(*a, **k):
        raise AssertionError("describe_backend must not touch the network")

    monkeypatch.setattr(sa.urllib.request, "urlopen", explode)
    monkeypatch.setenv("JELES_SEARXNG_URL", "http://127.0.0.1:8888")
    assert sa.describe_backend("searxng")["configured"] is True


def test_describe_backend_names_an_unknown_backend(monkeypatch):
    info = sa.describe_backend("altavista")
    assert info["configured"] is False and "altavista" in info["reason"]


def test_search_with_status_separates_failure_from_emptiness(monkeypatch):
    monkeypatch.setenv("JELES_SEARXNG_URL", "http://127.0.0.1:8888")

    _stub_urlopen(monkeypatch, {"results": []})
    empty = sa.search_with_status("q", "searxng")
    assert (empty["ok"], empty["hits"], empty["error"]) == (True, [], "")

    _stub_urlopen(monkeypatch, OSError("connection refused"))
    broken = sa.search_with_status("q", "searxng")
    assert broken["ok"] is False
    assert broken["hits"] == []
    assert "connection refused" in broken["error"]


def test_search_with_status_explains_a_missing_key_rather_than_raising(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = sa.search_with_status("q", "tavily")
    assert out["ok"] is False
    assert "TAVILY_API_KEY" in out["error"]


def test_make_searcher_still_swallows_but_now_logs(monkeypatch, caplog):
    """Corroboration depends on a failed search yielding no witness, so [] stays.
    Silence does not."""
    monkeypatch.setenv("JELES_SEARXNG_URL", "http://127.0.0.1:8888")
    _stub_urlopen(monkeypatch, OSError("boom"))
    with caplog.at_level("WARNING", logger="jeles.search"):
        assert sa.make_searcher("searxng")("q") == []
    assert "boom" in caplog.text
    assert "q" in caplog.text, "the failing query should be identifiable"


def test_make_searcher_warns_once_about_an_unconfigured_backend(monkeypatch, caplog):
    """The configuration warning is per-searcher, not per-query — otherwise a
    misconfigured backend floods the log and the signal is lost in itself."""
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    search = sa.make_searcher("brave")
    with caplog.at_level("WARNING", logger="jeles.search"):
        search("one")
        search("two")

    config_warnings = [r for r in caplog.records if "search backend" in r.getMessage()]
    assert len(config_warnings) == 1
    assert "BRAVE_API_KEY" in config_warnings[0].getMessage()

    # Each individual failure is still reported, so a per-query problem is not
    # hidden by the once-only configuration notice.
    failures = [r for r in caplog.records if "failed for" in r.getMessage()]
    assert len(failures) == 2


# ── ddg: the DuckDuckGo HTML SERP + circuit breaker ─────────────────────────
#
# Ported from willow-2.0's core/web_search.py, rewritten onto `_egress`. Same
# offline contract as the rest of this file: `urllib.request.urlopen` is
# stubbed (via the autouse fixture in conftest.py that points `_egress.opener`
# at it) — no real HTML, no real network, ever.

_DDG_SERP_HTML = """
<div class="results">
  <div class="result">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fopenpolicyagent.org%2Fx&rut=1">
      OPA <b>signed</b> bundles</a>
    <a class="result__snippet"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fopenpolicyagent.org%2Fx">
      a <b>signed</b> registry of reaction bundles</a>
  </div>
  <div class="result">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fosohq.com%2Fb&rut=2">
      Oso policy registry</a>
    <a class="result__snippet"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fosohq.com%2Fb">
      signed reaction registry prior art</a>
  </div>
</div>
"""


@pytest.fixture(autouse=True)
def _reset_ddg_circuit_breakers():
    """The breaker registry is module-level state — isolate each test from
    whatever the previous one tripped."""
    sa.reset_circuit_breakers()
    yield
    sa.reset_circuit_breakers()


def _stub_urlopen_body(monkeypatch, body, *, capture=None):
    """Like `_stub_urlopen`, but for a raw HTML/text body (or an Exception to
    raise) instead of a JSON payload — the DDG backend posts and gets HTML back,
    not JSON."""

    def fake(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["headers"] = dict(req.header_items())
            capture["data"] = req.data
        if isinstance(body, Exception):
            raise body
        payload = body.encode() if isinstance(body, str) else body
        return _Resp(payload)

    monkeypatch.setattr(sa.urllib.request, "urlopen", fake)


def test_parse_ddg_html_unwraps_redirect_links_and_maps_snippets():
    """Pure parser test — no network, no breaker: the regex + unwrap contract
    ported from willow-2.0's `_parse_ddg_html`/`_unwrap_ddg`."""
    hits = sa._parse_ddg_html(_DDG_SERP_HTML, max_results=8)
    assert [h["url"] for h in hits] == [
        "https://openpolicyagent.org/x",
        "https://osohq.com/b",
    ]
    assert all(urlparse(h["url"]).netloc != "duckduckgo.com" for h in hits)
    assert hits[0]["title"] == "OPA signed bundles"
    assert hits[0]["snippet"] == "a signed registry of reaction bundles"


def test_looks_like_results_page_distinguishes_size_and_content():
    assert sa._looks_like_results_page("short") is False
    assert sa._looks_like_results_page("x" * 3000) is False  # no "result"
    assert sa._looks_like_results_page("result " + "x" * 3000) is True


def test_ddg_html_backend_returns_real_serp_results(monkeypatch):
    """The point of the rewrite: `ddg` now returns actual web results, not
    DuckDuckGo's Instant-Answer related topics."""
    cap = {}
    _stub_urlopen_body(monkeypatch, _DDG_SERP_HTML, capture=cap)
    hits = sa.make_searcher("ddg")("signed policy registry")
    assert [h["url"] for h in hits] == [
        "https://openpolicyagent.org/x",
        "https://osohq.com/b",
    ]
    assert set(hits[0]) == {"title", "url", "snippet"}  # the Searcher contract


def test_ddg_html_backend_posts_form_body_through_egress(monkeypatch):
    """Confirms the rewrite actually goes over `_egress.fetch`: form-encoded
    POST body, User-Agent header, and the real DDG HTML endpoint — not a GET
    querystring and not `requests`."""
    cap = {}
    _stub_urlopen_body(monkeypatch, _DDG_SERP_HTML, capture=cap)
    sa.make_searcher("ddg")("q")
    assert cap["url"] == sa._DDG_URL
    body = urllib.parse.parse_qs(cap["data"].decode())
    assert body["q"] == ["q"]
    lc = {k.lower(): v for k, v in cap["headers"].items()}
    assert lc.get("user-agent") == sa._UA


def test_ddg_html_parser_miss_is_fail_soft_and_logged(monkeypatch, caplog):
    """A substantial, results-shaped body that yields 0 parsed links is a
    likely DDG markup change, not a real empty result — flagged, not silent,
    but still fail-soft (conflict_scan must see `[]`, never an exception)."""
    big_no_match_body = "<html>" + "result " * 400 + "</html>"
    assert len(big_no_match_body) > sa._PARSER_MISS_MIN_BODY
    _stub_urlopen_body(monkeypatch, big_no_match_body)
    with caplog.at_level("WARNING", logger="jeles.search"):
        assert sa.make_searcher("ddg")("q") == []
    assert "parser miss" in caplog.text


def test_ddg_hard_block_status_raises_hardblockerror(monkeypatch):
    exc = urllib.error.HTTPError(sa._DDG_URL, 403, "Forbidden", None, None)
    _stub_urlopen_body(monkeypatch, exc)
    with pytest.raises(sa.HardBlockError):
        sa._ddg_fetch("q")


def test_ddg_retryable_status_raises_transientsearcherror(monkeypatch):
    exc = urllib.error.HTTPError(sa._DDG_URL, 503, "Service Unavailable", None, None)
    _stub_urlopen_body(monkeypatch, exc)
    with pytest.raises(sa.TransientSearchError):
        sa._ddg_fetch("q")


def test_ddg_html_backend_is_fail_soft_on_repeated_failure(monkeypatch):
    """Same guarantee as every other backend: conflict_scan must see `[]`,
    never an exception, however DDG fails."""
    monkeypatch.setenv("JELES_SEARCH_MAX_ATTEMPTS", "1")  # no retry, fast test
    _stub_urlopen_body(monkeypatch, OSError("connection refused"))
    assert sa.make_searcher("ddg")("q") == []


def test_ddg_html_backend_trips_its_breaker_after_repeated_failures(monkeypatch):
    """Enough consecutive failures open the breaker; the next call is refused
    locally — no network call at all — until the cooldown elapses."""
    monkeypatch.setenv("JELES_SEARCH_MAX_ATTEMPTS", "1")  # no retry, fast test
    monkeypatch.setenv("JELES_SEARCH_CB_THRESHOLD", "2")
    monkeypatch.setenv("JELES_SEARCH_CB_COOLDOWN", "300")  # won't elapse mid-test

    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        raise OSError("connection refused")

    monkeypatch.setattr(sa.urllib.request, "urlopen", fake)

    search = sa.make_searcher("ddg")
    search("one")
    search("two")
    assert calls["n"] == 2
    assert sa._get_breaker("ddg").state == "OPEN"

    # Breaker is open: the third call must not touch the network at all.
    assert search("three") == []
    assert calls["n"] == 2, "an open breaker must fast-fail, not dial DDG again"


def test_ddg_html_backend_search_with_status_reports_the_open_breaker(monkeypatch):
    monkeypatch.setenv("JELES_SEARCH_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("JELES_SEARCH_CB_THRESHOLD", "1")
    _stub_urlopen_body(monkeypatch, OSError("boom"))

    sa.search_with_status("q", "ddg")  # trips the breaker
    out = sa.search_with_status("q", "ddg")  # now refused locally
    assert out["ok"] is False
    assert out["hits"] == []
    assert "circuit" in out["error"]


def test_circuit_breaker_opens_after_threshold_and_half_opens_after_cooldown():
    """Direct unit test of `CircuitBreaker` with an injectable clock — no real
    sleeping, no dependence on wall-clock timing."""
    clock = {"t": 0.0}
    cb = sa.CircuitBreaker(fail_threshold=2, base_cooldown=10.0, clock=lambda: clock["t"])

    assert cb.allow() is True  # CLOSED
    cb.record_failure()
    assert cb.state == "CLOSED" and cb.allow() is True  # one failure: still closed
    cb.record_failure()
    assert cb.state == "OPEN"
    assert cb.allow() is False  # cooldown not elapsed

    clock["t"] = 10.0
    assert cb.allow() is True  # cooldown elapsed -> HALF_OPEN
    assert cb.state == "HALF_OPEN"


def test_circuit_breaker_half_open_failure_doubles_the_cooldown():
    clock = {"t": 0.0}
    cb = sa.CircuitBreaker(
        fail_threshold=1, base_cooldown=10.0, max_cooldown=100.0, clock=lambda: clock["t"]
    )
    cb.record_failure()
    assert cb.state == "OPEN"

    clock["t"] = 10.0
    assert cb.allow() is True and cb.state == "HALF_OPEN"
    cb.record_failure()  # probe failed
    assert cb.state == "OPEN"
    assert cb.allow() is False  # still within the new cooldown

    clock["t"] = 20.0  # 10s more: old cooldown, not new
    assert cb.allow() is False
    clock["t"] = 30.0  # 20s: the doubled cooldown
    assert cb.allow() is True


def test_circuit_breaker_success_resets_fully():
    clock = {"t": 0.0}
    cb = sa.CircuitBreaker(fail_threshold=2, base_cooldown=10.0, clock=lambda: clock["t"])
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.state == "CLOSED", "a success must clear the failure count, not just reopen"


def test_reset_circuit_breakers_clears_state():
    cb = sa._get_breaker("ddg")
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert sa._get_breaker("ddg").state == "OPEN"
    sa.reset_circuit_breakers()
    assert sa._get_breaker("ddg").state == "CLOSED"


def test_with_retry_retries_a_transient_failure_then_succeeds():
    calls = {"n": 0}
    sleeps = []

    def attempt():
        calls["n"] += 1
        if calls["n"] < 2:
            raise sa.TransientSearchError("503")
        return ["ok"]

    result = sa._with_retry(
        attempt,
        max_attempts=3,
        budget=100.0,
        base_backoff=0.01,
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )
    assert result == ["ok"]
    assert calls["n"] == 2
    assert len(sleeps) == 1, "one retry, so exactly one backoff sleep"


def test_with_retry_does_not_retry_a_hard_block():
    calls = {"n": 0}

    def attempt():
        calls["n"] += 1
        raise sa.HardBlockError("403")

    with pytest.raises(sa.HardBlockError):
        sa._with_retry(attempt, max_attempts=5, sleep=lambda s: None, clock=lambda: 0.0)
    assert calls["n"] == 1, "a hard block must not be retried"


def test_with_retry_gives_up_after_max_attempts():
    calls = {"n": 0}

    def attempt():
        calls["n"] += 1
        raise sa.TransientSearchError("still down")

    with pytest.raises(sa.TransientSearchError):
        sa._with_retry(attempt, max_attempts=3, sleep=lambda s: None, clock=lambda: 0.0)
    assert calls["n"] == 3
