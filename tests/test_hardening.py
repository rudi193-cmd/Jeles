"""Box-audit follow-up fixes (2026-07-24): lock in the jeles hardening."""

import io
import json

import pytest

from jeles import corpus, willow_mcp_client
from jeles.reactions import conflict_scan as cs
from jeles.reactions import search_adapter as sa

# ── corpus ────────────────────────────────────────────────────────────────


def test_machine_nugget_does_not_render_as_human_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("WILLOW_STORE_ROOT", str(tmp_path))
    corpus._conns.clear()
    corpus.put_nugget("q1?", "a", ["https://x.org/1"], "designer")  # human (default)
    corpus.put_nugget("q2?", "a", ["https://x.org/2"], cs.WITNESS, verification_kind="machine")
    hits = {n["question"]: corpus.to_search_hit(n) for n in corpus.list_nuggets()}
    assert hits["q1?"]["confidence"] == "verified"
    assert hits["q1?"]["verification_kind"] == "human"
    assert hits["q2?"]["confidence"] == "corroborated"  # not "verified"
    assert hits["q2?"]["verification_kind"] == "machine"


def test_records_table_carries_willow_mcp_columns(tmp_path, monkeypatch):
    # A3: a jeles-created collection must not crash a willow-mcp writer that
    # inserts deviation/action.
    monkeypatch.setenv("WILLOW_STORE_ROOT", str(tmp_path))
    corpus._conns.clear()
    conn = corpus._conn("shared_soil")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(records)")}
    assert {"deviation", "action"} <= cols
    # A willow-mcp-shaped insert succeeds against the jeles-created table.
    conn.execute(
        "INSERT INTO records (id,data,created_at,updated_at,deviation,action,deleted)"
        " VALUES ('x','{}','t','t',0.0,'work_quiet',0)"
    )


def test_collection_name_is_validated(monkeypatch):
    with pytest.raises(ValueError):
        corpus._validate_collection("../../../etc/cron.d/x")
    with pytest.raises(ValueError):
        corpus._validate_collection("")
    corpus._validate_collection("ask_jeles_corpus")  # legal — no raise


def test_short_codes_do_not_collide_distinct_gaps(tmp_path, monkeypatch):
    monkeypatch.setenv("WILLOW_STORE_ROOT", str(tmp_path))
    corpus._conns.clear()
    a = corpus.log_gap("Does drug A interact with X?")
    b = corpus.log_gap("Does drug B interact with X?")
    assert a["id"] != b["id"]  # were merging before
    assert len(corpus.list_gaps()) == 2
    # A genuine rephrasing (same tokens) still merges.
    c = corpus.log_gap("With X, does drug A interact?")
    assert c["id"] == a["id"]


# ── conflict_scan ─────────────────────────────────────────────────────────


def test_domain_keeps_two_label_public_suffixes_distinct():
    assert cs._domain("https://alice.co.uk/x") == "alice.co.uk"
    assert cs._domain("https://bob.co.uk/y") == "bob.co.uk"
    assert cs._domain("https://foo.github.io/p") == "github.io"  # still collapses


def test_sources_exclude_unparseable_urls_and_tag_machine():
    def searcher(q):
        if "supersedes" in q:
            return [
                {
                    "title": "A signed registry",
                    "url": "https://real-a.org/x",
                    "snippet": "an existing signed registry",
                }
            ]
        if "comparison" in q:
            return [
                {
                    "title": "Signed registry designs",
                    "url": "https://real-b.org/y",
                    "snippet": "comparing signed registry designs",
                },
                {"title": "signed registry", "url": "not-a-url"},
            ]  # no domain
        return []

    proposals = cs.react({"claim": "signed registry"}, searcher=searcher)
    assert proposals[0]["driver"] == "put_nugget"
    nug = proposals[0]["args"]
    assert set(nug["sources"]) == {"https://real-a.org/x", "https://real-b.org/y"}
    assert "not-a-url" not in nug["sources"]
    assert nug["verification_kind"] == "machine"


# ── search_adapter ────────────────────────────────────────────────────────


def test_oversized_response_fails_soft(monkeypatch):
    monkeypatch.setenv("JELES_SEARXNG_URL", "http://127.0.0.1:8888")
    monkeypatch.setattr(sa, "_MAX_BYTES", 100)
    big = json.dumps({"results": [{"title": "t", "url": "https://a/" + "x" * 500}]}).encode()

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    monkeypatch.setattr(sa.urllib.request, "urlopen", lambda req, timeout=None: _R(big))
    assert sa.make_searcher("searxng")("q") == []  # too big -> soft empty


# ── willow_mcp_client ─────────────────────────────────────────────────────


def test_subprocess_env_drops_unrelated_secrets(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "secret")
    monkeypatch.setenv("TAVILY_API_KEY", "secret2")
    monkeypatch.setenv("WILLOW_HOME", "/w")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = willow_mcp_client._subprocess_env()
    assert "BRAVE_API_KEY" not in env and "TAVILY_API_KEY" not in env
    assert env.get("WILLOW_HOME") == "/w"
    assert "PATH" in env
