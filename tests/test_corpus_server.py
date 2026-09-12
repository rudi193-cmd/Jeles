"""corpus_server — the standalone MCP server over the corpus.

This module had **no tests at all** until the SDK 2.0 port, which is exactly why
its breakage was invisible: `mcp.server.fastmcp` was removed in SDK 2.0, so
`import jeles.corpus_server` raised ModuleNotFoundError under any 2.x SDK, and
nothing in CI ever imported it.

These tests are deliberately cheap. They import the module (which is the check
that caught nothing before), assert the tool surface is what the README claims,
and drive each tool against a stubbed corpus to pin the two behaviours that are
easy to get wrong and impossible to see from a signature:

  * `corpus_ask` forwards a gap on a miss, and does *not* on a hit.
  * `corpus_search` never forwards, whatever it finds.
  * `corpus_web_search` reports a failed search as a failure, never as an
    empty answer, and never labels a web page as verified.

Skipped wholesale when the SDK is absent — base `jeles` has no runtime
dependencies, and the `no-extras` CI leg installs exactly that shape.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp", reason="jeles[mcp] extra not installed")

import jeles
from jeles import corpus_server

EXPECTED_TOOLS = {
    # The settled layer.
    "corpus_ask",
    "corpus_search",
    "corpus_get",
    "corpus_list",
    "corpus_put",
    "corpus_gaps",
    "corpus_resolve_gap",
    # The second hop.
    "corpus_web_search",
    "corpus_search_status",
    # The third hop.
    "corpus_institutional_search",
    "corpus_sources",
    # Checking a claim, and whose shelf it came off.
    "corpus_verify_claim",
    "corpus_host_card",
    # The fleet edges.
    "corpus_fleet_status",
}


def _listed_tools():
    return asyncio.run(corpus_server.mcp.list_tools())


def test_exactly_the_documented_tools_are_registered():
    assert {t.name for t in _listed_tools()} == EXPECTED_TOOLS


def test_every_tool_carries_a_description():
    """The docstrings are the tool descriptions an MCP client shows a model."""
    missing = [t.name for t in _listed_tools() if not (t.description or "").strip()]
    assert not missing, f"tools with no description: {missing}"


def test_every_tool_takes_app_id():
    """Naming-convention parity with willow-mcp: `app_id` on every tool, so a
    permission gate can be added later without changing any signature."""
    # SDK 2.0 renamed Tool.inputSchema -> Tool.input_schema.
    without = [
        t.name for t in _listed_tools() if "app_id" not in (t.input_schema.get("properties") or {})
    ]
    assert not without, f"tools missing app_id: {without}"


def test_server_advertises_the_package_version():
    """Guards the tag-derived version reaching the wire, not just the metadata."""
    assert corpus_server.mcp.version == jeles.__version__


def test_ask_forwards_a_gap_on_a_miss(monkeypatch):
    forwarded = []
    monkeypatch.setattr(corpus_server.corpus, "ask_corpus", lambda q: {"found": False})
    monkeypatch.setattr(
        corpus_server.willow_mcp_client, "forward_gap", lambda q: forwarded.append(q)
    )

    result = corpus_server.corpus_ask("app", "what is the accent colour?")

    assert result == {"found": False}
    assert forwarded == ["what is the accent colour?"]


def test_ask_does_not_forward_on_a_hit(monkeypatch):
    forwarded = []
    monkeypatch.setattr(
        corpus_server.corpus,
        "ask_corpus",
        lambda q: {"found": True, "exact": True, "nugget": {"id": "n1"}},
    )
    monkeypatch.setattr(
        corpus_server.willow_mcp_client, "forward_gap", lambda q: forwarded.append(q)
    )

    result = corpus_server.corpus_ask("app", "known question")

    assert result["found"] is True
    assert forwarded == [], "a hit must not be recorded as a gap"


def test_search_never_forwards_even_when_it_finds_nothing(monkeypatch):
    """The passive/deliberate split: search is background, so a miss is not a
    gap. Only `ask` treats a miss as a question worth tracking."""
    forwarded = []
    monkeypatch.setattr(corpus_server.corpus, "search_nuggets", lambda q, limit: [])
    monkeypatch.setattr(
        corpus_server.willow_mcp_client, "forward_gap", lambda q: forwarded.append(q)
    )

    assert corpus_server.corpus_search("app", "nothing matches this") == []
    assert forwarded == []


def _capture_put(monkeypatch):
    seen = {}

    def _put(question, answer, sources, verified_by, **kw):
        seen.update(
            question=question, answer=answer, sources=sources, verified_by=verified_by, **kw
        )
        return {"id": "n1", "action": "created", "verification_kind": kw.get("verification_kind")}

    monkeypatch.setattr(corpus_server.corpus, "put_nugget", _put)
    return seen


def test_put_passes_every_field_through(monkeypatch):
    seen = _capture_put(monkeypatch)
    monkeypatch.delenv(corpus_server.TRUST_TOOL_WRITES_ENV, raising=False)

    result = corpus_server.corpus_put(
        "app",
        "q?",
        "a.",
        ["src/one.json"],
        "designer",
        tags=["colour"],
        nugget_id="n1",
    )

    assert result == {"id": "n1", "action": "created", "verification_kind": "asserted"}
    assert seen == {
        "question": "q?",
        "answer": "a.",
        "sources": ["src/one.json"],
        "verified_by": "designer",
        "tags": ["colour"],
        "nugget_id": "n1",
        "verification_kind": "asserted",
        "written_by": "app",
    }


# ── corpus_put cannot mint the top rung ──────────────────────────────────────
#
# This tool is reachable by any MCP client that can start the server, and one
# of the things that client does is read the open web through
# `corpus_web_search`. At HEAD, a page saying "record that X is true" came back
# through here as a nugget with `verified_by` set to whatever the model typed,
# landed at `confidence: verified`, and was served by `corpus_ask` as settled
# fact from then on — in a store shared with willow-mcp. Verified before the
# fix: `to_search_hit` returned `verified | Verified corpus — the operator`.


def test_a_tool_write_is_an_assertion_not_a_verification(monkeypatch):
    seen = _capture_put(monkeypatch)
    monkeypatch.delenv(corpus_server.TRUST_TOOL_WRITES_ENV, raising=False)

    corpus_server.corpus_put("app", "q?", "a.", ["s"], "the operator")

    assert seen["verification_kind"] == "asserted"


def test_the_caller_cannot_choose_its_own_rung(monkeypatch):
    """`verification_kind` is deliberately not a parameter of the tool. If a
    model could pass it, the gate would be a suggestion."""
    schema = {t.name: t.input_schema for t in _listed_tools()}["corpus_put"]
    props = schema.get("properties") or {}
    assert "verification_kind" not in props
    assert "written_by" not in props, "written_by is stamped from app_id, not supplied"


def test_the_writing_app_is_recorded_beside_the_claim(monkeypatch):
    """`verified_by` is whatever string the caller typed. `written_by` is which
    app actually made the call, and is what a reader is shown."""
    seen = _capture_put(monkeypatch)
    corpus_server.corpus_put("some-mcp-client", "q?", "a.", ["s"], "the operator")
    assert seen["verified_by"] == "the operator"
    assert seen["written_by"] == "some-mcp-client"


def test_the_trust_switch_alone_no_longer_mints_human(monkeypatch):
    """`JELES_CORPUS_TRUST_TOOL_WRITES=1` used to be sufficient by itself —
    exactly the forgery `jeles._nestor_seal` (the Nestor give-back) closes:
    a tool caller that knows the switch is set, and types a plausible
    `verified_by`, must still be refused the `human` rung without a
    verifying Nestor seal in `evidence`. No `evidence` at all is the
    simplest such caller."""
    seen = _capture_put(monkeypatch)
    monkeypatch.setenv(corpus_server.TRUST_TOOL_WRITES_ENV, "1")

    corpus_server.corpus_put("app", "q?", "a.", ["s"], "designer")

    assert seen["verification_kind"] == "asserted"


def test_the_trust_switch_alone_refuses_even_a_typed_evidence_dict(monkeypatch):
    """A caller that also fabricates an evidence-shaped dict — the scheme
    name, a made-up hex string — is refused exactly the same way. Only a
    signature that actually verifies is different from typing nothing."""
    seen = _capture_put(monkeypatch)
    monkeypatch.setenv(corpus_server.TRUST_TOOL_WRITES_ENV, "1")

    corpus_server.corpus_put(
        "app",
        "q?",
        "a.",
        ["s"],
        "designer",
        evidence={"scheme": "nestor-seal-v1", "seal_sig": "0" * 64},
    )

    assert seen["verification_kind"] == "asserted"


def test_the_trust_switch_off_refuses_even_with_valid_evidence(monkeypatch):
    """Both gates are required, not either: with the trust switch off, a
    valid-looking (here: unchecked, since _nestor_seal.verify_human_write is
    stubbed to say yes) seal still does not reach `human` — corpus_put must
    not even ask the question unless the operator opened the door first."""
    seen = _capture_put(monkeypatch)
    monkeypatch.delenv(corpus_server.TRUST_TOOL_WRITES_ENV, raising=False)
    monkeypatch.setattr(
        corpus_server._nestor_seal,
        "verify_human_write",
        lambda *a, **k: (True, "ok"),
    )

    corpus_server.corpus_put(
        "app",
        "q?",
        "a.",
        ["s"],
        "designer",
        evidence={"scheme": "nestor-seal-v1", "seal_sig": "irrelevant-here"},
    )

    assert seen["verification_kind"] == "asserted"


def test_a_verifying_seal_promotes_the_write_to_human(monkeypatch):
    """With the trust switch on AND a seal that actually verifies —
    `_nestor_seal.verify_human_write` stubbed here to isolate this tool from
    the real cryptography, which `test_nestor_seal_signing.py` covers end to
    end — the write reaches `human`, and the evidence rides along on the
    record."""
    seen = _capture_put(monkeypatch)
    monkeypatch.setenv(corpus_server.TRUST_TOOL_WRITES_ENV, "1")
    monkeypatch.setattr(
        corpus_server._nestor_seal,
        "verify_human_write",
        lambda *a, **k: (True, "ok"),
    )
    evidence = {"scheme": "nestor-seal-v1", "seal_sig": "a-real-signature"}

    result = corpus_server.corpus_put("app", "q?", "a.", ["s"], "designer", evidence=evidence)

    assert seen["verification_kind"] == "human"
    assert seen["evidence"] == evidence
    assert result["verification_kind"] == "human"


def test_a_failing_seal_is_carried_on_the_asserted_write_for_a_reviewer(monkeypatch):
    """A refused seal still lands — as `asserted`, not lost — and the
    evidence that failed to verify is kept on the record rather than
    discarded, so a person triaging gaps/assertions can see what was tried.
    `corpus.py` never interprets `evidence` either way (see its comment
    above `_KIND_RANK`)."""
    seen = _capture_put(monkeypatch)
    monkeypatch.setenv(corpus_server.TRUST_TOOL_WRITES_ENV, "1")
    monkeypatch.setattr(
        corpus_server._nestor_seal,
        "verify_human_write",
        lambda *a, **k: (False, "signature does not verify under verified_by's key"),
    )
    evidence = {"scheme": "nestor-seal-v1", "seal_sig": "forged-or-transplanted"}

    corpus_server.corpus_put("app", "q?", "a.", ["s"], "designer", evidence=evidence)

    assert seen["verification_kind"] == "asserted"
    assert seen["evidence"] == evidence


def test_the_trust_switch_is_not_read_at_import(monkeypatch):
    """A module-level `os.environ[...]` read is how a typo in an env var turns
    into a server that will not start at all."""
    import inspect

    src = inspect.getsource(corpus_server)
    module_level = [
        line
        for line in src.splitlines()
        if "os.environ" in line and not line.startswith((" ", "\t"))
    ]
    assert not module_level, f"env read at import time: {module_level}"


@pytest.mark.parametrize(
    "tool,corpus_fn,args",
    [
        ("corpus_get", "get_nugget", ("app", "n1")),
        ("corpus_list", "list_nuggets", ("app",)),
        ("corpus_gaps", "list_gaps", ("app",)),
    ],
)
def test_read_tools_delegate_to_corpus(monkeypatch, tool, corpus_fn, args):
    sentinel = {"delegated": True}
    monkeypatch.setattr(corpus_server.corpus, corpus_fn, lambda *a, **k: sentinel)
    assert getattr(corpus_server, tool)(*args) == sentinel


# ── The second hop: the open web ────────────────────────────────────────────

WEB_TOOLS = {"corpus_web_search", "corpus_search_status"}


def test_the_web_hop_is_registered():
    """The gap this closes: search_adapter existed with exactly one consumer
    (conflict_scan.react), and neither was reachable from this server — so a
    client got a corpus and no internet."""
    assert {t.name for t in _listed_tools()} >= WEB_TOOLS


def test_web_search_reports_a_failure_rather_than_an_empty_answer(monkeypatch):
    """`ok: false` with no hits must not be mistakable for "the web had
    nothing" — answering "I don't know" to a search that never ran is a lie."""
    monkeypatch.setattr(
        corpus_server.search_adapter,
        "search_with_status",
        lambda q: {
            "hits": [],
            "ok": False,
            "backend": "brave",
            "shallow": False,
            "error": "BRAVE_API_KEY is not set",
        },
    )
    out = corpus_server.corpus_web_search("app", "anything")
    assert out["ok"] is False
    assert out["hits"] == []
    assert "BRAVE_API_KEY" in out["error"]


def test_web_search_distinguishes_a_genuinely_empty_result(monkeypatch):
    monkeypatch.setattr(
        corpus_server.search_adapter,
        "search_with_status",
        lambda q: {"hits": [], "ok": True, "backend": "searxng", "shallow": False, "error": ""},
    )
    out = corpus_server.corpus_web_search("app", "nothing matches")
    assert (out["ok"], out["hits"], out["error"]) == (True, [], "")


def test_web_hits_are_never_labelled_verified(monkeypatch):
    """Corpus hits and web hits share a shape so they can merge into one ranked
    list. They must not share a confidence: a human-verified nugget and a page
    off the internet cannot be allowed to read the same."""
    monkeypatch.setattr(
        corpus_server.search_adapter,
        "search_with_status",
        lambda q: {
            "hits": [
                {"title": "A", "url": "https://example.org/a", "snippet": "s"},
                {"title": "B", "url": "https://sub.example.com/b", "snippet": "t"},
            ],
            "ok": True,
            "backend": "searxng",
            "shallow": False,
            "error": "",
        },
    )
    hits = corpus_server.corpus_web_search("app", "q")["hits"]

    assert [h["confidence"] for h in hits] == ["unverified", "unverified"]
    assert [h["source_id"] for h in hits] == ["web", "web"]
    assert [h["hostname"] for h in hits] == ["example.org", "sub.example.com"]
    assert [h["n"] for h in hits] == [0, 1]


def test_web_hits_carry_the_same_keys_as_corpus_hits(monkeypatch):
    """The merge contract for the layering work: same keys, so a host can rank
    corpus and web results together without translating either."""
    monkeypatch.setattr(
        corpus_server.search_adapter,
        "search_with_status",
        lambda q: {
            "hits": [{"title": "A", "url": "https://example.org/a", "snippet": "s"}],
            "ok": True,
            "backend": "searxng",
            "shallow": False,
            "error": "",
        },
    )
    web = corpus_server.corpus_web_search("app", "q")["hits"][0]
    nugget = corpus_server.corpus.to_search_hit(
        {"question": "q?", "answer": "a", "sources": ["s"], "verified_by": "human"}
    )
    assert set(web) == set(nugget)


def test_web_search_respects_limit(monkeypatch):
    monkeypatch.setattr(
        corpus_server.search_adapter,
        "search_with_status",
        lambda q: {
            "hits": [
                {"title": str(i), "url": f"https://e.org/{i}", "snippet": ""} for i in range(10)
            ],
            "ok": True,
            "backend": "searxng",
            "shallow": False,
            "error": "",
        },
    )
    assert len(corpus_server.corpus_web_search("app", "q", limit=3)["hits"]) == 3


def test_web_hit_survives_a_junk_url(monkeypatch):
    """Backends are untrusted input; a malformed url must not take the tool
    down, and the hit must still be shaped."""
    monkeypatch.setattr(
        corpus_server.search_adapter,
        "search_with_status",
        lambda q: {
            "hits": [{"title": "x", "url": "not a url", "snippet": ""}],
            "ok": True,
            "backend": "ddg",
            "shallow": True,
            "error": "",
        },
    )
    hit = corpus_server.corpus_web_search("app", "q")["hits"][0]
    assert hit["confidence"] == "unverified"
    assert hit["hostname"] in ("web", "")


def test_search_status_passes_the_backend_diagnosis_through(monkeypatch):
    monkeypatch.delenv("JELES_SEARXNG_URL", raising=False)
    monkeypatch.delenv("JELES_SEARCH_BACKEND", raising=False)
    status = corpus_server.corpus_search_status("app")
    assert status["backend"] == "ddg"
    assert (status["configured"], status["shallow"]) == (True, False), (
        "the zero-config default is a real HTML-SERP scrape, not a placeholder"
    )


def test_search_status_makes_no_request(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("corpus_search_status must not touch the network")

    monkeypatch.setattr(corpus_server.search_adapter.urllib.request, "urlopen", explode)
    assert corpus_server.corpus_search_status("app")["backend"]


# ── The third hop: special collections ──────────────────────────────────────


def test_the_institutional_hop_is_registered():
    names = {t.name for t in _listed_tools()}
    assert {"corpus_institutional_search", "corpus_sources"} <= names


def test_institutional_search_runs_locally_with_no_configuration(monkeypatch):
    """No secret, no service — the reason the collections were moved in."""
    monkeypatch.delenv("JELES_REMOTE_URL", raising=False)
    monkeypatch.setattr(
        corpus_server.institutional.sources,
        "search",
        lambda q, sources=None, limit_per_source=3, **k: {
            "sources_queried": ["arxiv"],
            "total": 1,
            "results": {
                "arxiv": [{"title": "t", "url": "https://arxiv.org/abs/1", "institution": "arXiv"}]
            },
        },
    )
    out = corpus_server.corpus_institutional_search("app", "q")
    assert (out["ok"], out["lane"]) == (True, "local")
    assert out["hits"][0]["confidence"] == "institutional"


def test_institutional_search_reports_failure_rather_than_an_empty_shelf(monkeypatch):
    monkeypatch.setattr(
        corpus_server.institutional,
        "search_institutional",
        lambda q, **k: {
            "hits": [],
            "ok": False,
            "lane": "remote",
            "sources_queried": [],
            "total": 0,
            "error": "JELES_REMOTE_SECRET is not set",
        },
    )
    out = corpus_server.corpus_institutional_search("app", "q")
    assert out["ok"] is False
    assert "JELES_REMOTE_SECRET" in out["error"]


def test_institutional_search_narrows_and_pages(monkeypatch):
    seen = {}

    def _search(q, *, sources_filter=None, limit_per_source=3):
        seen.update(query=q, sources_filter=sources_filter, limit_per_source=limit_per_source)
        return {
            "hits": [{"n": i} for i in range(20)],
            "ok": True,
            "lane": "local",
            "sources_queried": ["arxiv"],
            "total": 20,
            "error": "",
        }

    monkeypatch.setattr(corpus_server.institutional, "search_institutional", _search)
    out = corpus_server.corpus_institutional_search(
        "app", "q", limit=5, sources=["arxiv"], limit_per_source=2
    )

    assert seen == {"query": "q", "sources_filter": ["arxiv"], "limit_per_source": 2}
    assert len(out["hits"]) == 5
    assert out["total"] == 20, "total reports the fan-out, not the truncated page"


def test_corpus_sources_lists_the_collections_without_searching(monkeypatch):
    out = corpus_server.corpus_sources("app")
    assert out["total"] >= 50
    assert out["default_count"] <= out["total"], "opt-in sources sit out by default"
    assert set(out["sources"][0]) == {"id", "name", "key_required", "key_env", "opt_in"}


def test_search_status_covers_both_outward_hops(monkeypatch):
    """One place to ask "can I look anywhere?" — and the web keys stay at the
    top level so a client written against 0.3.x keeps working."""
    monkeypatch.delenv("JELES_SEARXNG_URL", raising=False)
    monkeypatch.delenv("JELES_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("JELES_REMOTE_URL", raising=False)

    status = corpus_server.corpus_search_status("app")

    assert status["backend"] == "ddg"  # unchanged top-level web keys
    assert status["shallow"] is False
    assert status["institutional"]["lane"] == "local"
    assert status["institutional"]["configured"] is True


def test_the_confidence_ladder_has_four_distinct_rungs():
    """verified > corroborated > institutional > unverified. If any two of
    these ever collapse, the librarian is citing something it did not check."""
    from jeles import corpus, institutional

    human = corpus.to_search_hit(
        {"question": "q", "answer": "a", "sources": ["s"], "verified_by": "human"}
    )
    machine = corpus.to_search_hit(
        {
            "question": "q",
            "answer": "a",
            "sources": ["s"],
            "verified_by": "scan",
            "verification_kind": "machine",
        }
    )
    inst_hit = institutional.to_hit(
        {"title": "t", "url": "https://arxiv.org/a", "institution": "arXiv"}
    )
    web_hit = corpus_server._web_hit({"title": "t", "url": "https://e.org/a"}, 0)

    rungs = [
        human["confidence"],
        machine["confidence"],
        inst_hit["confidence"],
        web_hit["confidence"],
    ]
    assert rungs == ["verified", "corroborated", "institutional", "unverified"]
    assert len(set(rungs)) == 4


# ── Closing a gap ───────────────────────────────────────────────────────────


def test_resolve_gap_delegates_and_stamps_the_caller(monkeypatch):
    """`resolved_by` defaults to the calling app_id, so the record always says
    who closed the gap even when nobody passed a name."""
    seen = {}

    def _capture(gap_id, resolved_by="", nugget_id=""):
        seen.update(gap_id=gap_id, resolved_by=resolved_by, nugget_id=nugget_id)
        return {"id": gap_id, "status": "resolved", "resolved_at": "now"}

    monkeypatch.setattr(corpus_server.corpus, "resolve_gap", _capture)

    out = corpus_server.corpus_resolve_gap("ask-jeles", "g1", nugget_id="n1")
    assert out["status"] == "resolved"
    assert seen == {"gap_id": "g1", "resolved_by": "ask-jeles", "nugget_id": "n1"}


def test_an_explicit_resolver_outranks_the_app_id(monkeypatch):
    """A person deciding is worth recording as the person, not as the tool
    they happened to be driving."""
    seen = {}
    monkeypatch.setattr(
        corpus_server.corpus,
        "resolve_gap",
        lambda gap_id, resolved_by="", nugget_id="": seen.update(by=resolved_by) or {},
    )
    corpus_server.corpus_resolve_gap("ask-jeles", "g1", resolved_by="designer")
    assert seen["by"] == "designer"


def test_gaps_hides_resolved_by_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        corpus_server.corpus,
        "list_gaps",
        lambda limit=50, include_resolved=False: (
            seen.update(limit=limit, include_resolved=include_resolved) or []
        ),
    )
    corpus_server.corpus_gaps("app")
    assert seen["include_resolved"] is False
    corpus_server.corpus_gaps("app", include_resolved=True)
    assert seen["include_resolved"] is True


# ── The fleet edges ─────────────────────────────────────────────────────────


def test_fleet_status_reports_both_edges(monkeypatch):
    monkeypatch.setattr(
        corpus_server.willow_mcp_client,
        "forward_status",
        lambda: {"enabled": True, "forwarded": 3, "failed": 0},
    )
    monkeypatch.setattr(
        corpus_server._nestor_seal,
        "describe",
        lambda: {"installed": True, "ready": True, "reason": "ok"},
    )
    out = corpus_server.corpus_fleet_status("app")
    assert out["willow_mcp"]["forwarded"] == 3
    assert out["nestor"]["ready"] is True


def test_fleet_status_makes_no_request(monkeypatch):
    """The whole point is to be safe to call before deciding to use an edge."""

    def _boom(*a, **k):
        raise AssertionError("fleet status must not touch the network")

    monkeypatch.setattr(corpus_server.willow_mcp_client, "forward_gap", _boom)
    monkeypatch.setattr(corpus_server.willow_mcp_client, "call_tool", _boom)
    monkeypatch.setattr(corpus_server._nestor_seal, "verify_human_write", _boom)
    corpus_server.corpus_fleet_status("app")


def test_a_silently_failing_forward_is_visible(monkeypatch):
    """Gap forwarding never raises into `corpus_ask`, so a gate denial is
    invisible from every other surface. This is the window onto it."""
    monkeypatch.setattr(
        corpus_server.willow_mcp_client,
        "forward_status",
        lambda: {
            "enabled": True,
            "session_ready": True,
            "forwarded": 0,
            "failed": 12,
            "last_error": "no manifest for 'ask-jeles'",
        },
    )
    monkeypatch.setattr(corpus_server._nestor_seal, "describe", lambda: {})
    willow = corpus_server.corpus_fleet_status("app")["willow_mcp"]
    assert willow["failed"] == 12 and willow["forwarded"] == 0
    assert "no manifest" in willow["last_error"]


# ── A negative limit is not "all but the last N" ────────────────────────────


def test_web_search_refuses_a_negative_limit(monkeypatch):
    """A bare [:limit] reads -1 as "all but the last", so the guard that
    `corpus.py` applies to every one of its own slices belongs here too."""
    monkeypatch.setattr(
        corpus_server.search_adapter,
        "search_with_status",
        lambda q: {
            "hits": [
                {"title": str(i), "url": f"https://e.org/{i}", "snippet": ""} for i in range(10)
            ],
            "ok": True,
            "backend": "searxng",
            "shallow": False,
            "error": "",
        },
    )
    assert corpus_server.corpus_web_search("app", "q", limit=-1)["hits"] == []


def test_institutional_search_refuses_a_negative_limit(monkeypatch):
    seen = {}

    def _search(query, sources_filter=None, limit_per_source=3):
        seen["limit_per_source"] = limit_per_source
        return {"hits": [{"title": str(i)} for i in range(10)], "ok": True}

    monkeypatch.setattr(corpus_server.institutional, "search_institutional", _search)

    out = corpus_server.corpus_institutional_search("app", "q", limit=-1, limit_per_source=-5)
    assert out["hits"] == []
    # Guarded before it reaches sources.py, where 65 source functions each
    # slice their own results with a bare [:limit].
    assert seen["limit_per_source"] == 0


# ── Every required argument says what it is ─────────────────────────────────


def test_app_id_is_described_on_every_tool():
    """`app_id` carried no schema description for the life of this server, and
    its meaning lived only in the module docstring — which a calling model
    never sees. Measured against eight local models on 2026-08-28: all eight
    filled it wrong, omitting it, inventing a value ("design", "your_app_id"),
    or putting the subject of the question there ("Tokyo Night"). Adding one
    sentence fixed it for four of them. This is the regression guard."""
    undescribed = []
    for t in _listed_tools():
        prop = (t.input_schema.get("properties") or {}).get("app_id") or {}
        if not (prop.get("description") or "").strip():
            undescribed.append(t.name)
    assert not undescribed, f"tools whose app_id says nothing: {undescribed}"


# ── Checking a claim, and whose shelf it came off ───────────────────────────


def test_verify_claim_delegates_and_guards_its_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        corpus_server.source_trail,
        "verify_claim",
        lambda claim, sources=None, limit=2: (
            seen.update(claim=claim, sources=sources, limit=limit) or {"matched": False}
        ),
    )
    corpus_server.corpus_verify_claim("app", "the sky is blue", limit=-3)
    assert seen["claim"] == "the sky is blue"
    assert seen["limit"] == 0, "a negative limit must not reach sources.py"


def test_verify_claim_reports_an_unbacked_claim_as_an_answer(monkeypatch):
    """`matched: false` is a finding, not a failure — the caller asked whether
    anything backs the claim and the answer is no."""
    monkeypatch.setattr(
        corpus_server.source_trail,
        "verify_claim",
        lambda claim, sources=None, limit=2: {
            "claim": claim,
            "matched": False,
            "title": "",
            "url": "",
            "source": "",
            "institution": "",
            "tier": "",
            "confidence": 0.0,
        },
    )
    out = corpus_server.corpus_verify_claim("app", "the moon is cheese")
    assert out["matched"] is False
    assert out["confidence"] == 0.0


def test_host_card_reports_a_known_host():
    out = corpus_server.corpus_host_card("app", "api.crossref.org")
    assert out["found"] is True
    assert out["card"]["host"] == "api.crossref.org"
    assert "roles" in out["card"]


def test_host_card_tolerates_how_hostnames_actually_arrive():
    """Case and a trailing dot are how a host comes off a parsed URL."""
    a = corpus_server.corpus_host_card("app", "API.Crossref.ORG.")
    assert a["found"] is True and a["card"]["host"] == "api.crossref.org"


def test_an_uncatalogued_host_is_absent_not_condemned(monkeypatch):
    """No card means this package makes no statement about the host — which is
    not the same as saying it should not be trusted."""
    out = corpus_server.corpus_host_card("app", "example.invalid")
    assert out == {"found": False, "host": "example.invalid"}


def test_host_card_reaches_no_verdict():
    """A card records custody and jurisdiction. Deciding trust is a policy's
    job, and a person's — nothing here may look like a ruling."""
    card = corpus_server.corpus_host_card("app", "api.crossref.org")["card"]
    for banned in ("trusted", "trustworthy", "safe", "verdict", "allow"):
        assert banned not in card, f"a card must not carry a {banned!r} field"


def test_the_module_docstring_names_every_registered_tool():
    """The one list nothing was guarding.

    README records this drift happening once already — "They were added
    without this list being updated, so it said six for as long as there were
    ten" — and it happened again in the same file the day that sentence was
    quoted: `corpus_verify_claim` and `corpus_host_card` were registered,
    tested, added to the README, and left out of the module docstring.

    `test_exactly_the_documented_tools_are_registered` did not catch it,
    because EXPECTED_TOOLS lives in this file: it forces a human to notice a
    *new* tool, not to describe it where a reader of the module would look.
    """
    doc = corpus_server.__doc__ or ""
    missing = [t.name for t in _listed_tools() if t.name not in doc]
    assert not missing, f"registered but absent from the module docstring: {missing}"


def test_the_readme_names_every_registered_tool():
    """The third list. README's tool paragraph is the first thing a host reads
    and the last thing anyone updates — it undercounted for four releases
    before this, by its own admission."""
    from pathlib import Path

    readme = Path(__file__).resolve().parent.parent / "README.md"
    text = readme.read_text(encoding="utf-8")
    missing = [t.name for t in _listed_tools() if t.name not in text]
    assert not missing, f"registered but absent from README.md: {missing}"
