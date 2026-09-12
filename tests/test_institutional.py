"""institutional — the third hop, local-first, tested offline.

`jeles.sources` does the fan-out and is tested in test_sources.py. What this
file pins is the layer above it:

  * which lane a search takes, and that the local one needs no configuration
  * that a failure never reads as an empty shelf
  * the hit shape, and that `institutional` stays its own rung on the
    confidence ladder

Nothing here makes a request: the local lane is driven with a stubbed
`sources.search`, and the remote lane with a stubbed `urlopen`.
"""

from __future__ import annotations

import io
import json
import urllib.request

import pytest

from jeles import institutional as inst

SECRET = "s3cret"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JELES_REMOTE_URL", raising=False)
    monkeypatch.delenv("JELES_REMOTE_SECRET", raising=False)


def _stub_local(monkeypatch, results=None, **kw):
    # The full accounting contract by default — every dispatched source lands in
    # exactly one of results/skipped/failed/timed_out. `_legacy_payload` below
    # builds the older shape on purpose, for the version-skew cases.
    payload = {
        "query": "q",
        "sources_queried": ["arxiv", "loc"],
        "total": sum(len(v) for v in (results or {}).values()),
        "results": results or {},
        "failed": {},
        "skipped": {},
        "timed_out": [],
        "unknown": [],
    }
    payload.update(kw)
    calls = []

    def fake(query, sources=None, limit_per_source=3, **_):
        calls.append({"query": query, "sources": sources, "limit_per_source": limit_per_source})
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(inst.sources, "search", fake)
    return calls


def _stub_remote(monkeypatch, payload, capture=None):
    def fake(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["headers"] = dict(req.header_items())
            capture["body"] = json.loads(req.data.decode())
        if isinstance(payload, Exception):
            raise payload
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)


# ── Which lane, and can it work ─────────────────────────────────────────────


def test_the_local_lane_needs_no_configuration():
    """The whole point of moving the collections in-package: out of the box,
    with nothing set, the third hop works."""
    info = inst.describe_remote()
    assert info["lane"] == "local"
    assert info["configured"] is True
    assert info["reason"] == ""
    assert len(info["sources"]) >= 50


def test_a_remote_url_with_a_secret_selects_the_remote_lane(monkeypatch):
    monkeypatch.setenv("JELES_REMOTE_URL", "https://remote.example/")
    monkeypatch.setenv("JELES_REMOTE_SECRET", SECRET)
    info = inst.describe_remote()
    assert (info["lane"], info["configured"]) == ("remote", True)
    assert info["base_url"] == "https://remote.example", "trailing slash trimmed"


def test_a_remote_url_without_a_secret_is_a_misconfiguration(monkeypatch):
    """Not "fall back quietly to local" — the operator asked for the remote,
    and a 401 from it would read exactly like an empty shelf."""
    monkeypatch.setenv("JELES_REMOTE_URL", "https://remote.example")
    info = inst.describe_remote()
    assert (info["lane"], info["configured"]) == ("remote", False)
    assert "JELES_REMOTE_SECRET" in info["reason"]
    assert "Unset JELES_REMOTE_URL" in info["reason"], "say how to get back to local"


def test_describe_remote_never_leaks_the_secret(monkeypatch):
    monkeypatch.setenv("JELES_REMOTE_URL", "https://remote.example")
    monkeypatch.setenv("JELES_REMOTE_SECRET", SECRET)
    assert SECRET not in json.dumps(inst.describe_remote())


def test_describe_remote_makes_no_request(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("describe_remote must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    assert inst.describe_remote()["lane"] == "local"


def test_list_sources_is_local_knowledge(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("listing sources must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    listed = inst.list_sources()
    assert len(listed) >= 50
    assert set(listed[0]) == {"id", "name", "key_required", "key_env", "opt_in"}
    keyed = [s for s in listed if s["key_required"]]
    assert keyed and all(s["key_env"] for s in keyed), (
        "a source that needs a key must name the variable, not just say it needs one"
    )
    assert any(s["key_required"] for s in listed), "some sources need keys"


# ── The local lane ──────────────────────────────────────────────────────────


def test_local_search_runs_in_process(monkeypatch):
    calls = _stub_local(
        monkeypatch,
        results={
            "arxiv": [
                {
                    "title": "Signed policy bundles",
                    "url": "https://arxiv.org/abs/1",
                    "source": "arxiv",
                    "institution": "arXiv / Cornell University",
                    "snippet": "abstract",
                    "date": "2026-01-01",
                    "id": "1",
                }
            ]
        },
    )

    out = inst.search_institutional("policy", sources_filter=["arxiv"], limit_per_source=2)

    assert out["lane"] == "local"
    assert out["ok"] is True
    assert calls == [{"query": "policy", "sources": ["arxiv"], "limit_per_source": 2}]
    assert out["hits"][0]["source"] == "arXiv / Cornell University"


def test_local_failure_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(
        inst.sources, "search", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    out = inst.search_institutional("q")
    assert (out["ok"], out["hits"], out["lane"]) == (False, [], "local")
    assert "boom" in out["error"]


def test_an_empty_shelf_is_not_a_failure(monkeypatch):
    _stub_local(monkeypatch, results={})
    out = inst.search_institutional("q")
    assert (out["ok"], out["hits"], out["error"]) == (True, [], "")


def test_grouped_results_are_flattened_with_the_group_kept_as_a_tag(monkeypatch):
    _stub_local(
        monkeypatch,
        results={
            "arxiv": [{"title": "A", "url": "https://arxiv.org/abs/1", "institution": "arXiv"}],
            "loc": [
                {
                    "title": "B",
                    "url": "https://loc.gov/item/2",
                    "institution": "Library of Congress",
                }
            ],
        },
    )
    out = inst.search_institutional("q")

    assert len(out["hits"]) == 2
    assert out["sources_queried"] == ["arxiv", "loc"]
    by_host = {h["hostname"]: h for h in out["hits"]}
    assert by_host["arxiv.org"]["tags"] == ["arxiv"]
    assert by_host["loc.gov"]["source"] == "Library of Congress"
    assert [h["n"] for h in out["hits"]] == [0, 1]


# ── The remote lane, when opted into ────────────────────────────────────────


def test_remote_request_matches_the_service_contract(monkeypatch):
    monkeypatch.setenv("JELES_REMOTE_URL", "https://remote.example")
    monkeypatch.setenv("JELES_REMOTE_SECRET", SECRET)
    monkeypatch.setattr(
        inst.sources, "search", lambda *a, **k: pytest.fail("must not fan out locally")
    )
    cap = {}
    _stub_remote(monkeypatch, {"sources_queried": [], "total": 0, "results": {}}, capture=cap)

    inst.search_institutional("q", sources_filter=["arxiv"], limit_per_source=2)

    assert cap["url"] == "https://remote.example/search"
    assert cap["body"] == {"query": "q", "limit_per_source": 2, "sources": ["arxiv"]}
    headers = {k.lower(): v for k, v in cap["headers"].items()}
    # A raw shared secret, not a Bearer token — the service hmac-compares it.
    assert headers["x-jeles-secret"] == SECRET
    assert "authorization" not in headers


def test_remote_omits_sources_when_not_narrowed(monkeypatch):
    monkeypatch.setenv("JELES_REMOTE_URL", "https://remote.example")
    monkeypatch.setenv("JELES_REMOTE_SECRET", SECRET)
    cap = {}
    _stub_remote(monkeypatch, {"results": {}}, capture=cap)
    inst.search_institutional("q")
    assert "sources" not in cap["body"], "omitting it is what asks for everything"


def test_a_misconfigured_remote_never_calls_out(monkeypatch):
    monkeypatch.setenv("JELES_REMOTE_URL", "https://remote.example")

    def explode(*a, **k):
        raise AssertionError("must not call a remote with no secret")

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    out = inst.search_institutional("q")
    assert out["ok"] is False
    assert "JELES_REMOTE_SECRET" in out["error"]


def test_remote_transport_failure_is_reported(monkeypatch):
    monkeypatch.setenv("JELES_REMOTE_URL", "https://remote.example")
    monkeypatch.setenv("JELES_REMOTE_SECRET", SECRET)
    _stub_remote(monkeypatch, OSError("connection refused"))
    out = inst.search_institutional("q")
    assert (out["ok"], out["lane"]) == (False, "remote")
    assert "connection refused" in out["error"]


def test_remote_oversized_response_is_refused(monkeypatch):
    monkeypatch.setenv("JELES_REMOTE_URL", "https://remote.example")
    monkeypatch.setenv("JELES_REMOTE_SECRET", SECRET)
    monkeypatch.setattr(inst, "_MAX_BYTES", 10)
    _stub_remote(monkeypatch, {"results": {"x": [{"title": "a long title"}]}})
    out = inst.search_institutional("q")
    assert out["ok"] is False and "refusing" in out["error"]


def test_non_http_remote_url_is_refused(monkeypatch):
    monkeypatch.setenv("JELES_REMOTE_URL", "file:///etc")
    monkeypatch.setenv("JELES_REMOTE_SECRET", SECRET)
    out = inst.search_institutional("q")
    assert out["ok"] is False
    assert "refusing" in out["error"].lower()


# ── The hit shape ───────────────────────────────────────────────────────────


def test_institutional_hits_get_their_own_confidence_rung():
    """Not `verified` (nobody checked it) and not `unverified` (a named body
    published it). Collapsing it either way discards the point of the hop."""
    hit = inst.to_hit(
        {
            "title": "t",
            "url": "https://arxiv.org/abs/1",
            "institution": "arXiv / Cornell University",
        }
    )
    assert hit["confidence"] == "institutional"
    assert hit["source_id"] == "institutional"
    assert hit["verification_kind"] == "institutional"
    assert hit["nugget_id"] == "" and hit["verified_by"] == ""


def test_institutional_hits_carry_the_same_keys_as_corpus_hits():
    """The merge contract across all three hops."""
    from jeles import corpus

    hit = inst.to_hit({"title": "t", "url": "https://arxiv.org/abs/1", "institution": "arXiv"})
    nugget = corpus.to_search_hit(
        {"question": "q?", "answer": "a", "sources": ["s"], "verified_by": "human"}
    )
    assert set(hit) == set(nugget)


def test_a_hit_missing_institution_falls_back_to_the_source_id():
    """OpenAlex often has no affiliation. Showing the source id is honest;
    inventing an institution would not be."""
    hit = inst.to_hit({"title": "t", "url": "https://gbif.org/x", "source": "gbif"})
    assert hit["source"] == "gbif"
    assert hit["tags"] == ["gbif"]


def test_a_junk_url_does_not_break_shaping():
    hit = inst.to_hit({"title": "t", "url": "not a url"})
    assert hit["confidence"] == "institutional"


# ── Unreachable is not empty ────────────────────────────────────────────────


def test_all_sources_failing_is_reported_as_a_failure(monkeypatch):
    """Found live: a sandbox blocking egress returned ok=true, total=0 — the
    same lie the web hop used to tell, one level down. If everything we asked
    failed, we did not look."""
    _stub_local(
        monkeypatch,
        results={},
        sources_queried=["arxiv", "crossref"],
        failed={"arxiv": "URLError: tunnel 403", "crossref": "URLError: tunnel 403"},
    )
    out = inst.search_institutional("q")

    assert out["ok"] is False
    assert out["failed"] == ["arxiv", "crossref"]
    assert "not one of 2 sources completed a look" in out["error"]
    assert "not an empty result" in out["error"]


# ── The bug that made the check above unreachable ────────────────────────────
#
# The test above passed, and the code it tested never fired in production. It
# asked `len(failed) >= len(sources_queried)`, and six key-required sources sit
# in the DEFAULT fan-out and abstained with a bare `return []` — entering
# neither `results` nor `failed`, so the count could not reach the total.
# Measured with all egress blocked: 60 queried, 55 failed, 5 vanished,
# `ok: true, total: 0, error: ""`. The fixture above only ever exercised the
# case where nothing abstains.
#
# `ok` now means "did at least one source complete a look?", which is decidable
# only because every dispatched source lands in exactly one bucket.


def test_the_default_configuration_outage_is_caught(monkeypatch):
    """The real shape of a blocked-egress run: most sources fail, and the
    key-required ones abstain without ever reaching the network."""
    _stub_local(
        monkeypatch,
        results={},
        sources_queried=[f"s{i}" for i in range(60)],
        failed={f"s{i}": "URLError: tunnel 403" for i in range(55)},
        skipped={f"s{i}": "no EUROPEANA_KEY" for i in range(55, 60)},
    )
    out = inst.search_institutional("q")

    assert out["ok"] is False, (
        "55 failed + 5 abstained of 60 is an outage; the old ratio test read "
        "55 >= 60 as False and called it an empty shelf"
    )
    assert "55 could not be reached" in out["error"]
    assert "5 abstained" in out["error"]
    assert out["skipped"], "an abstention is reported, not swallowed"


def test_an_abstention_alone_is_not_a_successful_look(monkeypatch):
    """Every source needing a key nobody has set is a configuration problem
    that reads exactly like an empty library."""
    _stub_local(
        monkeypatch,
        results={},
        sources_queried=["europeana", "dpla"],
        skipped={"europeana": "no EUROPEANA_KEY", "dpla": "no DPLA_KEY"},
    )
    out = inst.search_institutional("q")
    assert out["ok"] is False
    assert "2 abstained" in out["error"]
    assert "EUROPEANA_KEY" in out["error"], "name the key, so it is fixable"


def test_a_timed_out_source_is_not_a_source_that_looked(monkeypatch):
    _stub_local(monkeypatch, results={}, sources_queried=["a", "b"], timed_out=["a", "b"])
    out = inst.search_institutional("q")
    assert out["ok"] is False and "2 timed out" in out["error"]
    assert out["timed_out"] == ["a", "b"]


def test_one_source_that_looked_is_enough(monkeypatch):
    """`ok` is "were we able to look", not "did we find anything". One source
    that reached an empty shelf makes the empty answer trustworthy."""
    _stub_local(
        monkeypatch,
        results={},
        sources_queried=["a", "b", "c"],
        failed={"b": "URLError"},
        skipped={"c": "no KEY"},
    )
    out = inst.search_institutional("q")
    assert (out["ok"], out["error"]) == (True, "")


def test_a_typo_in_every_source_id_is_not_an_empty_library(monkeypatch):
    """An unrecognised id was logged and dropped while still being counted as
    queried, so a single typo disarmed the ratio the outage check depended on.
    Nothing dispatched is a configuration answer, not a search result."""
    _stub_local(monkeypatch, results={}, sources_queried=[], unknown=["arxvi", "crosref"])
    out = inst.search_institutional("q", sources_filter=["arxvi", "crosref"])
    assert out["ok"] is False
    assert "no source was dispatched" in out["error"]
    assert out["unknown"] == ["arxvi", "crosref"]


# ── The two lanes are separately deployed and will drift ─────────────────────


def _legacy_payload(**kw):
    """A `sources.search` response from before per-source accounting existed —
    what a `jeles-remote` deployment returns until it is redeployed."""
    payload = {"query": "q", "sources_queried": [], "total": 0, "results": {}}
    payload.update(kw)
    assert not {"skipped", "unknown", "timed_out"} & set(payload)
    return payload


def test_an_older_remote_is_read_conservatively(monkeypatch):
    """No `skipped` key means abstentions are invisible, so "looked and found
    nothing" cannot be told from "never got out of the process". Resolve toward
    not claiming an empty shelf — that is the error this module exists to avoid
    — and name the skew, because the fix is a redeploy, not a retry."""
    monkeypatch.setenv("JELES_REMOTE_URL", "https://remote.example")
    monkeypatch.setenv("JELES_REMOTE_SECRET", SECRET)
    _stub_remote(
        monkeypatch,
        _legacy_payload(sources_queried=["a", "b"], failed={"a": "URLError", "b": "URLError"}),
    )

    out = inst.search_institutional("q")
    assert out["ok"] is False
    assert "predates per-source accounting" in out["error"]


def test_an_older_remote_that_answers_is_still_believed(monkeypatch):
    """Conservative, not paranoid: hits are hits whatever shape they arrive in."""
    monkeypatch.setenv("JELES_REMOTE_URL", "https://remote.example")
    monkeypatch.setenv("JELES_REMOTE_SECRET", SECRET)
    _stub_remote(
        monkeypatch,
        _legacy_payload(
            sources_queried=["a", "b"],
            failed={"b": "URLError"},
            results={"a": [{"title": "t", "url": "https://a.org/1"}]},
            total=1,
        ),
    )

    out = inst.search_institutional("q")
    assert (out["ok"], out["error"]) == (True, "")
    assert len(out["hits"]) == 1


def test_the_source_listing_does_not_claim_to_know_the_remote(monkeypatch):
    """`describe_remote` promises to make no request, so a registry listing is
    the one thing it cannot get from the remote. It used to return the local
    list unlabelled — local knowledge presented as fact about a service it has
    never spoken to, and the two are separately deployed copies that drift."""
    monkeypatch.setenv("JELES_REMOTE_URL", "https://remote.example")
    monkeypatch.setenv("JELES_REMOTE_SECRET", SECRET)
    info = inst.describe_remote()

    assert info["lane"] == "remote"
    assert info["sources_lane"] == "local"
    assert "can drift" in info["reason"]
    assert "sources_queried" in info["reason"], "point at the answer that is real"


def test_the_local_lane_has_nothing_to_disclaim(monkeypatch):
    info = inst.describe_remote()
    assert (info["lane"], info["sources_lane"], info["reason"]) == ("local", "local", "")


# ── The count in the prose ───────────────────────────────────────────────────


def test_the_documented_source_count_matches_the_registry():
    """Four files said "~65 sources". The registry now really does hold 65,
    after four functions were recovered from the archived jeles-remote fork —
    three of them opt-in, so the default fan-out is 61. (Four opt-in among the
    recovered set now: `patentsview` joined them when search.patentsview.org
    turned out to have been DNS-dead since 2026.) A number retyped into four
    docstrings drifts from the thing it describes the moment anyone edits the
    registry. This is the only place the number is checked, so if it changes,
    this test is what tells you which prose to update — and the module
    docstring is checked here too, because it was the one that drifted: it
    still said 60 after the default set had moved twice."""
    from pathlib import Path

    registered = len(inst.sources.SOURCES)
    default = sum(1 for c in inst.sources.SOURCES.values() if not c.get("opt_in"))
    assert (registered, default) == (65, 61)

    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    assert f"{registered} registered source functions" in readme
    assert f"{default} of them in the default fan-out" in readme
    # Not `"~65" not in readme`: that pinned one drifted spelling, and the
    # README carried a *second* approximate count ("the ~62 institutional and
    # academic collections") that the literal check walked straight past. Any
    # tilde'd count in this range is the same mistake wearing a different
    # number, so refuse the shape rather than the instance.
    import re as _re

    approx = _re.findall(r"~\d{2}\b", readme)
    assert not approx, f"approximate source counts drift; say the number: {approx}"

    doc = inst.sources.__doc__ or ""
    assert f"{registered} registered" in doc
    assert f"{default} of them in the default fan-out" in doc


def test_every_key_required_source_reports_its_abstention(monkeypatch):
    """Not a detail: these abstain when their key is unset, which is what made
    the outage check unreachable. Any source added with key_required must
    either stay out of the default fan-out or report its abstention.

    This used to assert `not any(opt_in)` — a *proxy* for that rule, and one
    that was only ever true by accident of no keyed source being opt-in yet.
    `omdb`, recovered from the archived jeles-remote fork, is both: it needs a
    key, and it is opt-in because it was plain http upstream and its TLS is
    unverified. It satisfies the rule by the first branch. So the proxy is
    replaced by the property, checked by running it — which is strictly
    stronger, since it would also catch a keyed source that abstains with a
    bare `return []` and never reaches `skipped` at all.

    `semantic_scholar` is deliberately absent. Its registry entry claimed
    `key_required: True`, but it queries anonymously and
    SEMANTIC_SCHOLAR_API_KEY only lifts rate limits — so `list_sources()` was
    telling callers they needed a key they do not, and this test asserted the
    same wrong fact. Five sources abstain, not six.
    """
    keyed = {sid for sid, cfg in inst.sources.SOURCES.items() if cfg.get("key_required")}
    assert keyed == {"rijksmuseum", "dpla", "smithsonian", "europeana", "bhl", "omdb"}
    assert "semantic_scholar" not in keyed
    # Every source that abstains must name the variable it is waiting on,
    # otherwise `skipped` says a key is missing without saying which.
    assert all(inst.sources.SOURCES[sid].get("key_env") for sid in keyed)

    # The property, exercised. With its key unset, naming the source explicitly
    # must put it in `skipped` with the variable named — not return an empty
    # result set that reads like "this source had nothing".
    for sid in sorted(keyed):
        monkeypatch.delenv(inst.sources.SOURCES[sid]["key_env"], raising=False)
        out = inst.sources.search("anything", sources=[sid], limit_per_source=1)
        assert out["skipped"].get(sid), f"{sid} abstained without saying so"
        assert inst.sources.SOURCES[sid]["key_env"] in out["skipped"][sid]
        assert not out["results"].get(sid)


def test_a_partial_outage_still_succeeds(monkeypatch):
    """One dead source must not turn a real answer into a failure."""
    _stub_local(
        monkeypatch,
        results={
            "loc": [
                {"title": "t", "url": "https://loc.gov/1", "institution": "Library of Congress"}
            ]
        },
        sources_queried=["arxiv", "loc"],
        failed={"arxiv": "URLError: tunnel 403"},
    )
    out = inst.search_institutional("q")

    assert out["ok"] is True
    assert out["failed"] == ["arxiv"], "still reported, so the gap is visible"
    assert len(out["hits"]) == 1
    assert out["error"] == ""


def test_a_genuinely_empty_result_is_not_a_failure(monkeypatch):
    _stub_local(monkeypatch, results={}, sources_queried=["arxiv"], failed={})
    out = inst.search_institutional("q")
    assert (out["ok"], out["failed"], out["error"]) == (True, [], "")
