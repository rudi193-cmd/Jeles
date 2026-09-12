"""legal_citations — tested with a stubbed urlopen, exactly like
test_search_adapter.py: no network, every test replaces
urllib.request.urlopen (the conftest fixture routes the shared `_egress`
opener back through it) with a canned response or a canned exception.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from jeles import legal_citations as lc


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _stub_urlopen(monkeypatch, payload, *, capture=None):
    """Make urlopen return `payload` (dict -> json bytes) or raise it if it's
    an Exception. Mirrors test_search_adapter.py's helper."""

    def fake(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["headers"] = {k.lower(): v for k, v in req.header_items()}
            capture["data"] = req.data
        if isinstance(payload, Exception):
            raise payload
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr(lc._egress.urllib.request, "urlopen", fake)


def _explode(monkeypatch):
    """Wire urlopen to fail the test if it is ever called."""

    def boom(req, timeout=None):
        raise AssertionError("verify_citations must not touch the network here")

    monkeypatch.setattr(lc._egress.urllib.request, "urlopen", boom)


# ── Token posture: required, else a hard no-op ──────────────────────────────


def test_no_token_makes_zero_network_calls(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    _explode(monkeypatch)
    result = lc.verify_citations("See Brown v. Board, 347 U.S. 483 (1954).")
    assert result == {"ok": False, "configured": False, "reason": result["reason"], "citations": []}
    assert result["configured"] is False
    assert "COURTLISTENER_API_TOKEN" in result["reason"]


def test_explicit_token_param_is_used_even_without_env(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    cap = {}
    _stub_urlopen(monkeypatch, [], capture=cap)
    result = lc.verify_citations("text", token="explicit-token")
    assert result["ok"] is True
    assert cap["headers"]["authorization"] == "Token explicit-token"


def test_env_token_is_used_when_no_param_given(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "env-token")
    cap = {}
    _stub_urlopen(monkeypatch, [], capture=cap)
    lc.verify_citations("text")
    assert cap["headers"]["authorization"] == "Token env-token"


# ── The POST itself ──────────────────────────────────────────────────────────


def test_posts_form_encoded_text_with_auth_header(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok-123")
    cap = {}
    _stub_urlopen(monkeypatch, [], capture=cap)
    lc.verify_citations("347 U.S. 483 is Brown v. Board", token=None)
    assert cap["headers"]["authorization"] == "Token tok-123"
    assert cap["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert cap["data"] == b"text=347+U.S.+483+is+Brown+v.+Board"
    assert cap["url"] == lc._ENDPOINT


# ── Per-citation status mapping ──────────────────────────────────────────────


def test_200_hit_maps_to_matched_true_with_case_and_url(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    _stub_urlopen(
        monkeypatch,
        [
            {
                "citation": "347 U.S. 483",
                "normalized_citations": ["347 U.S. 483"],
                "start_index": 4,
                "end_index": 16,
                "status": 200,
                "clusters": [
                    {
                        "case_name": "Brown v. Board of Education",
                        "court": "scotus",
                        "date_filed": "1954-05-17",
                        "absolute_url": "/opinion/12345/brown-v-board-of-education/",
                    }
                ],
            },
        ],
    )
    result = lc.verify_citations("some text with 347 U.S. 483 in it")
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["matched_count"] == 1
    rec = result["citations"][0]
    assert rec["matched"] is True
    assert rec["citation"] == "347 U.S. 483"
    assert rec["case"] == "Brown v. Board of Education"
    assert rec["court"] == "scotus"
    assert rec["date"] == "1954-05-17"
    assert rec["url"] == ("https://www.courtlistener.com/opinion/12345/brown-v-board-of-education/")
    assert rec["normalized_citations"] == ["347 U.S. 483"]


@pytest.mark.parametrize("status", [404, 400, 300])
def test_non_200_statuses_map_to_matched_false(monkeypatch, status):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    _stub_urlopen(
        monkeypatch,
        [
            {
                "citation": "999 F.4th 999",
                "normalized_citations": [],
                "start_index": 0,
                "end_index": 12,
                "status": status,
            },
        ],
    )
    result = lc.verify_citations("999 F.4th 999")
    assert result["ok"] is True
    rec = result["citations"][0]
    assert rec["matched"] is False
    assert rec["status"] == status
    assert rec["case"] == ""
    assert rec["url"] == ""
    assert result["matched_count"] == 0


def test_ambiguous_300_surfaces_normalized_citations(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    _stub_urlopen(
        monkeypatch,
        [
            {
                "citation": "1 U.S. 1",
                "normalized_citations": ["1 U.S. 1", "1 Dall. 1"],
                "status": 300,
            },
        ],
    )
    result = lc.verify_citations("1 U.S. 1")
    rec = result["citations"][0]
    assert rec["matched"] is False
    assert rec["normalized_citations"] == ["1 U.S. 1", "1 Dall. 1"]


def test_mixed_batch_counts_matches_correctly(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    _stub_urlopen(
        monkeypatch,
        [
            {
                "citation": "a",
                "status": 200,
                "clusters": [{"case_name": "A", "absolute_url": "/opinion/1/a/"}],
            },
            {"citation": "b", "status": 404},
            {
                "citation": "c",
                "status": 200,
                "clusters": [{"case_name": "C", "absolute_url": "/opinion/3/c/"}],
            },
        ],
    )
    result = lc.verify_citations("a b c")
    assert result["count"] == 3
    assert result["matched_count"] == 2


# ── Top-level HTTP 429: rate limited, fail-soft ─────────────────────────────


def test_top_level_429_is_fail_soft_and_surfaces_wait_until(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    body = json.dumps({"wait_until": "2026-08-10T12:34:56Z"}).encode()
    err = urllib.error.HTTPError(lc._ENDPOINT, 429, "Too Many Requests", {}, io.BytesIO(body))
    _stub_urlopen(monkeypatch, err)
    result = lc.verify_citations("some text")
    assert result["ok"] is False
    assert result["configured"] is True
    assert result["citations"] == []
    assert "429" in result["reason"]
    assert "2026-08-10T12:34:56Z" in result["reason"]


def test_top_level_429_without_parseable_body_still_fails_soft(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    err = urllib.error.HTTPError(
        lc._ENDPOINT, 429, "Too Many Requests", {}, io.BytesIO(b"not json")
    )
    _stub_urlopen(monkeypatch, err)
    result = lc.verify_citations("some text")
    assert result["ok"] is False
    assert "429" in result["reason"]


def test_other_http_error_is_fail_soft(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    err = urllib.error.HTTPError(lc._ENDPOINT, 401, "Unauthorized", {}, io.BytesIO(b""))
    _stub_urlopen(monkeypatch, err)
    result = lc.verify_citations("some text")
    assert result == {"ok": False, "configured": True, "reason": result["reason"], "citations": []}
    assert "401" in result["reason"]


def test_network_error_never_raises(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    _stub_urlopen(monkeypatch, OSError("connection refused"))
    result = lc.verify_citations("some text")
    assert result["ok"] is False
    assert "connection refused" in result["reason"]


def test_unparseable_json_body_is_fail_soft(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")

    def fake(req, timeout=None):
        return _Resp(b"not json at all")

    monkeypatch.setattr(lc._egress.urllib.request, "urlopen", fake)
    result = lc.verify_citations("some text")
    assert result["ok"] is False
    assert result["citations"] == []


def test_non_array_response_is_fail_soft(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    _stub_urlopen(monkeypatch, {"unexpected": "shape"})
    result = lc.verify_citations("some text")
    assert result["ok"] is False
    assert "array" in result["reason"]


# ── The 64,000-character guard ───────────────────────────────────────────────


def test_text_over_the_char_limit_is_refused_without_a_network_call(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    _explode(monkeypatch)
    too_long = "x" * (lc.MAX_TEXT_CHARS + 1)
    result = lc.verify_citations(too_long)
    assert result["ok"] is False
    assert result["configured"] is True
    assert result["citations"] == []
    assert str(lc.MAX_TEXT_CHARS) in result["reason"]
    assert str(len(too_long)) in result["reason"]


def test_text_exactly_at_the_char_limit_is_allowed_through(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "tok")
    cap = {}
    _stub_urlopen(monkeypatch, [], capture=cap)
    at_limit = "x" * lc.MAX_TEXT_CHARS
    result = lc.verify_citations(at_limit)
    assert result["ok"] is True
    assert "url" in cap
