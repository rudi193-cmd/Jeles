"""sources — the in-package institutional collections, tested offline.

Sixty-odd source functions is too many to pin one-by-one without the suite
becoming the thing you maintain. So the bar here is deliberate:

  * **The fan-out machinery in full** — registry integrity, default vs opt-in
    selection, per-source failure isolation, the wall-clock cap, and the
    grouped response shape. That is the part every source depends on, and the
    part where a bug silently loses results rather than raising.
  * **A representative handful of parsers** — one JSON, one XML, one keyed —
    to prove the `_result` citation contract holds across response formats.

Every test stubs the network. Nothing here makes a request.
"""

from __future__ import annotations

import copy
import io
import json
import threading
import time

import pytest

# The genuine builder, from conftest, which captured it before any fixture could
# replace it. `sources._opener` is not it: that delegates to `_egress.opener`,
# which the autouse seam patches.
from conftest import real_opener as _REAL_OPENER

from jeles import sources


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if a test reaches the network by accident."""

    def explode(*a, **k):
        raise AssertionError("tests must not make real requests")

    monkeypatch.setattr(sources.urllib.request, "urlopen", explode)


@pytest.fixture(autouse=True)
def _opener_delegates_to_urlopen(monkeypatch):
    """`_urlopen` goes through a shared OpenerDirector so the scheme guard can
    run on redirect hops too. Every stub in this file replaces
    `urllib.request.urlopen`, which the opener does not consult — without this
    the stubs would be silently bypassed and the tests would hit the network.
    """

    class _Delegating:
        @staticmethod
        def open(req, timeout=None):
            return sources.urllib.request.urlopen(req, timeout=timeout)

    monkeypatch.setattr(sources, "_opener", lambda: _Delegating)


def _stub_json(monkeypatch, payload, capture=None):
    def fake(req, timeout=None):
        if capture is not None:
            capture.append(req.full_url if hasattr(req, "full_url") else str(req))
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        return _Resp(body.encode() if isinstance(body, str) else body)

    monkeypatch.setattr(sources.urllib.request, "urlopen", fake)


# ── The registry ────────────────────────────────────────────────────────────


def test_the_registry_is_populated_and_well_formed():
    reg = sources._load_registry()
    assert len(reg) >= 50, "the point of this module is breadth"
    for sid, cfg in reg.items():
        assert cfg["name"], f"{sid} has no display name"
        assert isinstance(cfg["key_required"], bool)
        assert isinstance(cfg["opt_in"], bool)
        assert isinstance(cfg["key_env"], str), f"{sid} key_env must be a string"


def test_every_registered_source_resolves_to_a_real_function():
    """`_resolve_fn` does getattr on this module, so a typo in a registry
    entry is invisible until someone searches and silently gets nothing."""
    missing = [
        sid
        for sid, cfg in sources._load_registry().items()
        if not callable(sources._resolve_fn(cfg["fn_name"]))
    ]
    assert not missing, f"registry entries with no function: {missing}"


def test_wikipedia_is_not_in_the_default_set():
    """Stated policy: every default result can appear in an academic
    bibliography."""
    reg = sources._load_registry()
    if "wikipedia" in reg:
        assert reg["wikipedia"]["opt_in"] is True


def test_default_fan_out_excludes_opt_in_sources(monkeypatch):
    seen = []
    monkeypatch.setattr(sources, "_resolve_fn", lambda fn: lambda q, n: seen.append(fn) or [])
    sources.search("q")
    reg = sources._load_registry()
    opt_in_fns = {c["fn_name"] for c in reg.values() if c["opt_in"]}
    assert not (opt_in_fns & set(seen)), "opt-in sources must be asked for by name"


def test_narrowing_queries_only_the_named_sources(monkeypatch):
    called = []

    def _resolve(fn_name):
        def _fn(q, n):
            called.append(fn_name)
            return []

        return _fn

    monkeypatch.setattr(sources, "_resolve_fn", _resolve)
    out = sources.search("q", sources=["arxiv"])
    assert len(called) == 1
    assert out["sources_queried"] == ["arxiv"]


def test_an_unknown_source_id_is_reported_not_fatal(monkeypatch):
    """It must not raise — and it must not be counted as queried either.
    `sources_queried` used to echo the request, so `search(sources=["typo"])`
    reported one source queried and zero failed, and any consumer computing a
    failure ratio from that saw a healthy search that never happened."""
    monkeypatch.setattr(sources, "_resolve_fn", lambda fn: lambda q, n: [])
    out = sources.search("q", sources=["arxiv", "not-a-real-source"])

    assert out["total"] == 0  # and no exception
    assert out["unknown"] == ["not-a-real-source"]
    assert out["sources_queried"] == ["arxiv"], "nothing was dispatched for a typo"


def test_a_registered_source_with_no_function_is_unknown_not_silent(monkeypatch):
    """`_resolve_fn` is a getattr, so a registry fn_name typo resolves to None.
    That used to `continue` while still counting the source as queried."""
    monkeypatch.setattr(
        sources, "_resolve_fn", lambda fn: None if fn == "search_arxiv" else (lambda q, n: [])
    )
    out = sources.search("q", sources=["arxiv", "loc"])
    assert out["unknown"] == ["arxiv"]
    assert out["sources_queried"] == ["loc"]


# ── Failure isolation: one bad source must not sink the fan-out ─────────────


def test_one_failing_source_does_not_lose_the_others(monkeypatch):
    def _resolve(fn_name):
        if fn_name == "search_arxiv":

            def _boom(q, n):
                raise RuntimeError("arxiv is down")

            return _boom
        return lambda q, n: [
            sources._result(
                title="t", url="https://loc.gov/1", source="loc", institution="Library of Congress"
            )
        ]

    monkeypatch.setattr(sources, "_resolve_fn", _resolve)
    out = sources.search("q", sources=["arxiv", "loc"])

    assert out["total"] == 1
    assert "arxiv" not in out["results"], "a failed source contributes nothing"
    assert "loc" in out["results"]


def test_a_reached_but_empty_source_is_recorded_as_empty_not_absent(monkeypatch):
    """Was `results == {}`. An empty dict cannot say whether a source was
    reached and had nothing or was never heard from, and that ambiguity is the
    whole bug this file's last section is about. `results[sid] == []` is the
    positive statement: asked, answered, nothing there."""
    monkeypatch.setattr(sources, "_resolve_fn", lambda fn: lambda q, n: [])
    out = sources.search("q", sources=["arxiv", "loc"])
    assert out["results"] == {"arxiv": [], "loc": []}
    assert out["total"] == 0
    assert sorted(out["sources_queried"]) == ["arxiv", "loc"]


def test_the_response_is_grouped_by_source_id(monkeypatch):
    monkeypatch.setattr(
        sources,
        "_resolve_fn",
        lambda fn: (
            lambda q, n: [
                sources._result(title="t", url="https://e.org/1", source="s", institution="Inst")
            ]
        ),
    )
    out = sources.search("q", sources=["arxiv", "loc"])
    assert set(out["results"]) == {"arxiv", "loc"}
    assert out["total"] == 2
    assert out["query"] == "q"


def test_limit_per_source_is_passed_through(monkeypatch):
    seen = []
    monkeypatch.setattr(sources, "_resolve_fn", lambda fn: lambda q, n: seen.append(n) or [])
    sources.search("q", sources=["arxiv", "loc"], limit_per_source=7)
    assert seen == [7, 7]


def test_the_wall_clock_cap_drops_stragglers_rather_than_hanging(monkeypatch):
    """A slow source must cost a result, not the whole call."""
    import time

    def _resolve(fn_name):
        if fn_name == "search_arxiv":

            def _slow(q, n):
                time.sleep(5)
                return [sources._result(title="late", url="u", source="arxiv", institution="arXiv")]

            return _slow
        return lambda q, n: [
            sources._result(
                title="quick",
                url="https://loc.gov/1",
                source="loc",
                institution="Library of Congress",
            )
        ]

    monkeypatch.setattr(sources, "_resolve_fn", _resolve)
    started = time.monotonic()
    out = sources.search("q", sources=["arxiv", "loc"], wall_clock_limit=0.5)
    elapsed = time.monotonic() - started

    assert elapsed < 3, "the cap must bound the call, not just log about it"
    assert "loc" in out["results"]


def test_no_thread_pool_is_created_at_import():
    """A thread pool at import would be a side effect in every process that
    merely imports jeles. There is no module-level pool to inspect any more —
    `_executor` builds one per `search` call — so this checks the property
    directly: importing must start no worker threads."""
    import subprocess
    import sys

    probe = (
        "import threading, jeles.sources\n"
        "extra = [t.name for t in threading.enumerate()\n"
        "         if t is not threading.current_thread()]\n"
        "assert not extra, f'import started threads: {extra}'\n"
    )
    assert subprocess.run([sys.executable, "-c", probe]).returncode == 0


def test_the_corpus_does_not_drag_in_the_collections():
    """`jeles.sources` imports urllib.request, so it pulls in socket and ssl —
    unavoidable for a module whose job is HTTP, and the same is true of
    `reactions.search_adapter`. The invariant that actually matters is that the
    *corpus* stays free of all of it, so a host doing local lookups never loads
    a network stack. That is what this guards."""
    import subprocess
    import sys

    probe = (
        "import sys, jeles.corpus\n"
        "assert 'jeles.sources' not in sys.modules, 'corpus pulled in the collections'\n"
        "assert not ({'socket', 'ssl'} & set(sys.modules)), sorted(sys.modules)\n"
    )
    assert subprocess.run([sys.executable, "-c", probe]).returncode == 0


# ── The citation contract ───────────────────────────────────────────────────

CITATION_KEYS = {"title", "url", "source", "institution", "snippet", "date", "id"}


def test_result_shape_is_the_citation_contract():
    r = sources._result(
        title=" T ", url="u", source="s", institution="I", snippet="x" * 900, date="d", rid="7"
    )
    assert set(r) == CITATION_KEYS
    assert r["title"] == "T", "titles are stripped"
    assert len(r["snippet"]) <= 400, "snippets are capped"
    assert r["id"] == "7"


# ── Sources do not honour their own documented types ────────────────────────
#
# `_result` used to do `(value or "").strip()`, which is only correct if every
# API returns a string or a null. They do not, and the failure is silent rather
# than loud: `search()`'s `_call` catches the AttributeError, so the source
# lands in `failed` and contributes nothing on every query.


@pytest.mark.parametrize(
    ("value", "want"),
    [
        ("plain", "plain"),
        (None, ""),
        ([], ""),
        (["one", "two"], "one two"),
        (["kept", None, "also"], "kept also"),  # nulls inside the list drop out
        (("a", "b"), "a b"),  # tuples too — same JSON shape
        (2026, "2026"),  # a bare int is not a null
        (0, "0"),  # ...and falsy is not absent
        ([["deep"], "flat"], "deep flat"),  # nested, because IA does that
    ],
)
def test_text_coerces_what_the_apis_actually_return(value, want):
    assert sources._text(value) == want


def test_a_list_valued_field_reaches_the_citation_contract_as_a_string():
    """The regression, at the layer that broke. Against 0.6.0 this raises
    `AttributeError: 'list' object has no attribute 'strip'` on the title."""
    r = sources._result(
        title=["Title", "Subtitle"],
        url="u",
        source="s",
        institution=["Cornell", "Ithaca"],
        snippet=["a", "b"],
        date="",
        rid="",
    )
    assert set(r) == CITATION_KEYS
    assert r["title"] == "Title Subtitle"
    assert r["snippet"] == "a b"
    # All three coerced fields, not just the two the first version checked:
    # `institution=None` alone left the old `(v or "").strip()` spelling passing,
    # because on a null the two are the same. A list is what tells them apart.
    assert r["institution"] == "Cornell Ithaca"
    assert all(isinstance(r[k], str) for k in ("title", "institution", "snippet"))

    n = sources._result(
        title=None, url="u", source="s", institution=None, snippet=None, date="", rid=""
    )
    assert (n["title"], n["institution"], n["snippet"]) == ("", "", ""), (
        "a null field is empty, never the string 'None'"
    )


def test_internet_archive_survives_the_list_description_it_really_sends(monkeypatch):
    """End to end, because the layer above is where the damage showed. IA
    returns `description` as a list often enough that this source contributed
    nothing to any query that reached it."""
    _stub_json(
        monkeypatch,
        {
            "response": {
                "docs": [
                    {
                        "identifier": "moby-dick",
                        "title": ["Moby Dick", "or, The Whale"],
                        "description": ["A voyage.", "With a whale."],
                        "date": "1851",
                    }
                ]
            }
        },
    )
    hits = sources.search_internet_archive("whale", limit=1)
    assert hits and set(hits[0]) == CITATION_KEYS
    assert hits[0]["title"] == "Moby Dick or, The Whale"
    assert hits[0]["snippet"] == "A voyage. With a whale."
    assert hits[0]["url"] == "https://archive.org/details/moby-dick"


def test_a_list_valued_field_does_not_land_the_source_in_failed(monkeypatch):
    """The symptom, named. `search` catches the AttributeError, so the only
    outward sign was a source that was permanently in `failed` — which reads
    as an outage rather than a bug in this file."""
    _stub_json(
        monkeypatch,
        {"response": {"docs": [{"identifier": "x", "title": ["A", "B"], "description": ["c"]}]}},
    )
    out = sources.search("q", sources=["internet_archive"], limit_per_source=1, wall_clock_limit=5)
    assert out["failed"] == {}, "a list-valued field must not read as an outage"
    assert out["results"]["internet_archive"], "and it must actually contribute"


def test_chronicling_america_parses_the_loc_collections_shape(monkeypatch):
    """The legacy `chroniclingamerica.loc.gov` JSON API was retired: it
    308-redirects to a 404, so this source returned nothing at all. The
    replacement changes every field this parser reads — `results` not `items`,
    an absolute `url` rather than an id to concatenate, and a `description`
    that is a list."""
    payload = json.dumps(
        {
            "results": [
                {
                    "title": "The Evening Star",
                    "url": "https://www.loc.gov/item/sn83045462/1900-01-01/ed-1/",
                    "description": ["Washington, D.C.", "Chronicling America"],
                    "date": "1900-01-01",
                    "id": "sn83045462",
                }
            ]
        }
    ).encode()

    # Not `_stub_json`: that captures the url only, and the raised timeout is
    # half of what makes this source work — 15s is not enough for the
    # collections API under fan-out load, and losing it looks like an outage.
    captured: list[tuple[str, float | None]] = []

    class _Recording:
        @staticmethod
        def open(req, timeout=None):
            captured.append((req.full_url, timeout))
            return _Resp(payload)

    monkeypatch.setattr(sources, "_opener", lambda: _Recording)

    hits = sources.search_chronicling_america("star", limit=1)
    assert hits and set(hits[0]) == CITATION_KEYS
    assert hits[0]["title"] == "The Evening Star"
    assert hits[0]["url"].startswith("https://www.loc.gov/item/"), (
        "the absolute url is used, not concatenated onto the retired host"
    )
    assert hits[0]["snippet"] == "Washington, D.C. Chronicling America"
    assert hits[0]["institution"] == "Chronicling America (Library of Congress)"

    assert len(captured) == 1
    url, timeout = captured[0]
    assert "chroniclingamerica.loc.gov" not in url, "the retired host must not be requested"
    assert url.startswith("https://www.loc.gov/collections/chronicling-america/")
    assert "fo=json" in url, "the collections API answers html without fo=json"
    assert timeout == 25, "the raised timeout is part of the fix, not decoration"


def test_the_registry_host_matches_where_chronicling_america_now_goes():
    """`hosts` is what `registered_hosts()` reports and what the egress guard is
    reasoned about from. Moving the request without moving the entry leaves the
    retired host on the allowed list and the live one off it."""
    hosts = sources.SOURCES["chronicling_america"]["hosts"]
    assert "www.loc.gov" in hosts
    assert "chroniclingamerica.loc.gov" not in hosts


def test_patentsview_is_opt_in_while_half_its_api_is_dns_dead():
    """`search.patentsview.org` stopped resolving, so every default fan-out
    spent a full timeout on it. Opt-in rather than deleted: `patents.google.com`
    still answers, and the id stays addressable by name."""
    assert sources.SOURCES["patentsview"].get("opt_in") is True


def test_a_slow_source_can_raise_its_own_timeout_above_the_default(monkeypatch):
    """`_TIMEOUT` bounds a normal request; loc.gov's collections API is slow
    enough to trip it under fan-out load. The override has to survive four
    frames — `_get` → `_fetch` → `_urlopen` → the opener — and dropping it in
    any of them leaves the source dead in exactly the way that is hardest to
    tell from an outage."""
    seen: list[float | None] = []

    class _Recording:
        @staticmethod
        def open(req, timeout=None):
            seen.append(timeout)
            return _Resp(b"{}")

    monkeypatch.setattr(sources, "_opener", lambda: _Recording)

    sources._get("https://www.loc.gov/collections/x/?fo=json", timeout=25)
    assert seen == [25]

    seen.clear()
    sources._get("https://www.loc.gov/collections/x/?fo=json")
    assert seen == [sources._TIMEOUT], "an unset timeout still means the default"


def test_openalex_parses_json_into_the_contract(monkeypatch):
    _stub_json(
        monkeypatch,
        {
            "results": [
                {
                    "display_name": "Signed policy bundles",
                    "doi": "https://doi.org/10.1/x",
                    "publication_year": 2026,
                    "id": "https://openalex.org/W1",
                    "authorships": [{"institutions": [{"display_name": "Cornell University"}]}],
                }
            ]
        },
    )
    hits = sources.search_openalex("policy", limit=1)
    assert hits and set(hits[0]) == CITATION_KEYS
    assert hits[0]["title"] == "Signed policy bundles"
    assert hits[0]["institution"] == "Cornell University"
    assert hits[0]["url"] == "https://doi.org/10.1/x", "the DOI is the citable url"


def test_an_absent_institution_is_empty_not_invented(monkeypatch):
    """OpenAlex derives the institution from authorships, which are often
    missing. Empty is the honest answer; `institutional.to_hit` falls back to
    the source id for display rather than fabricating an affiliation."""
    _stub_json(
        monkeypatch,
        {"results": [{"display_name": "Anonymous preprint", "id": "https://openalex.org/W2"}]},
    )
    assert sources.search_openalex("q", limit=1)[0]["institution"] == ""


def test_arxiv_parses_xml_into_the_contract(monkeypatch):
    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>http://arxiv.org/abs/2601.00001v1</id>
        <title>Deterministic policy evaluation</title>
        <summary>An abstract.</summary>
        <published>2026-01-02T00:00:00Z</published>
      </entry>
    </feed>"""
    _stub_json(monkeypatch, atom)
    hits = sources.search_arxiv("policy", limit=1)
    assert hits and set(hits[0]) == CITATION_KEYS
    assert hits[0]["title"] == "Deterministic policy evaluation"
    assert "arxiv.org" in hits[0]["url"]


def test_a_keyed_source_is_skipped_without_its_key(monkeypatch):
    """Missing key means the source is absent, never an exception — that is
    what lets the default fan-out run unconfigured.

    Uses a *registered* source: this used to exercise `search_omdb`, a plugin
    function that was never in SOURCES, so it proved the behaviour on a code
    path the fan-out never took."""
    monkeypatch.delenv("EUROPEANA_API_KEY", raising=False)
    assert sources.search_europeana("vermeer", limit=1) == []


def test_a_malformed_response_yields_no_hits_rather_than_raising(monkeypatch):
    _stub_json(monkeypatch, "not json at all")
    assert sources.search_openalex("q", limit=1) == []


def test_polite_pool_identity_is_overridable(monkeypatch):
    """Crossref/OpenAlex/NCBI throttle anonymous traffic; the contact address
    is sent, and a deployment must be able to make it its own."""
    urls = []
    monkeypatch.setattr(sources, "_CONTACT_EMAIL", "ops@example.org")
    _stub_json(monkeypatch, {"results": []}, capture=urls)
    sources.search_openalex("q", limit=1)
    assert urls and "ops%40example.org" in urls[0]


# ── The egress path (added when vendoring; not inherited) ───────────────────


def test_no_source_function_opens_or_reads_a_response_itself():
    """The size cap was a rule each source had to remember, and six of the
    eight sites that opened a socket did not — including all three XML
    sources. Now the only way to a body is `_fetch`, which opens and reads in
    one call, and this is what stops the seventh from being added.
    """
    import ast
    import pathlib

    egress = {"_fetch", "_get", "_get_html", "_read_capped", "_urlopen"}
    tree = ast.parse(pathlib.Path(sources.__file__).read_text(encoding="utf-8"))
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name in egress:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Name) and f.id in {"_urlopen", "urlopen"}:
                offenders.append(f"{fn.name} opens its own response")
            elif isinstance(f, ast.Attribute) and f.attr in {"read", "urlopen"}:
                offenders.append(f"{fn.name} calls .{f.attr}() directly")
    assert not offenders, offenders


def test_the_opener_refuses_a_non_http_scheme():
    """URLs are built from queries and API responses, so the scheme is
    enforced rather than assumed."""
    req = sources.urllib.request.Request("file:///etc/passwd")
    with pytest.raises(ValueError, match=r"refusing URL scheme outside \['https'\]"):
        sources._urlopen(req)


@pytest.mark.parametrize(
    "url, allowed",
    [
        ("https://example.org/x", True),
        # Plain http is refused. Nothing here needs it: the two functions that
        # requested over http were never registered and have been deleted, and the
        # `http://` strings in the registered XML sources are namespace URIs, which
        # are identifiers, not addresses.
        ("http://example.org/x", False),
        ("HTTPS://example.org/x", True),
        ("ftp://example.org/x", False),
        ("file:///etc/passwd", False),
        ("data:text/plain,hello", False),
        ("gopher://example.org/x", False),
    ],
)
def test_the_allowed_schemes_are_what_the_docstring_says(url, allowed):
    """An allowed scheme reaches the network stub — which the no-network
    fixture turns into an AssertionError, so getting that far is the proof it
    passed the guard."""
    req = sources.urllib.request.Request(url)
    if allowed:
        with pytest.raises(AssertionError, match="must not make real requests"):
            sources._urlopen(req)
    else:
        with pytest.raises(ValueError, match=r"refusing URL scheme outside \['https'\]"):
            sources._urlopen(req)


@pytest.mark.parametrize(
    "newurl, allowed",
    [
        ("https://example.org/b", True),
        # A 302 downgrading https -> http is a real attack shape, not a typo.
        ("http://example.org/b", False),
        # stdlib's own filter permits http, https *and* ftp — this is the hop it
        # let through, verified against 3.11 by watching the connection arrive.
        ("ftp://evil.example/x", False),
        ("file:///etc/passwd", False),
    ],
)
def test_a_redirect_to_a_disallowed_scheme_is_refused(newurl, allowed):
    """The guard in `_urlopen` sees only the URL a source built; urllib
    follows 3xx internally. Before this handler a 302 to ftp:// was followed
    without the guard ever seeing it."""
    handler = sources._SchemeGuardedRedirects()
    req = sources.urllib.request.Request("https://example.org/a")
    args = (req, io.BytesIO(b""), 302, "Found", {}, newurl)
    if allowed:
        assert handler.redirect_request(*args).full_url == newurl
    else:
        with pytest.raises(
            sources.urllib.error.HTTPError,
            match=r"refusing redirect to a scheme outside \['https'\]",
        ):
            handler.redirect_request(*args)


def test_the_opener_is_assembled_with_the_guard_and_without_local_schemes():
    names = {type(h).__name__ for h in _REAL_OPENER(sources._ALLOWED_SCHEMES).handlers}
    assert "SchemeGuardedRedirects" in names
    assert "HTTPRedirectHandler" not in names, "the unguarded default must not also be in"
    assert "HTTPHandler" not in names, "https-only: plain http gets no transport either"
    assert not (names & {"FileHandler", "FTPHandler", "DataHandler"}), (
        "a scheme past both checks should have nothing able to open it"
    )


def test_the_opener_is_not_built_at_import():
    """Same promise as the thread pool: importing jeles builds no state."""
    import subprocess
    import sys

    probe = (
        "import jeles.sources  # noqa: F401\n"
        "from jeles import _egress\n"
        "assert not _egress._OPENERS, 'opener built at import'\n"
    )
    assert subprocess.run([sys.executable, "-c", probe]).returncode == 0


# One oversized-but-otherwise-valid body per egress site. Each parses into
# exactly one hit when it fits under the cap, so "returns []" means refused
# rather than merely unparseable.
_PAD = "z" * 4096

_OVERSIZED = [
    (
        "search_arxiv",
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        "<id>http://arxiv.org/abs/2601.1</id><title>T</title>"
        f"</entry><!--{_PAD}--></feed>",
    ),
    (
        "search_gallica",
        '<r xmlns:srw="http://www.loc.gov/zing/srw/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><srw:recordData>'
        "<dc:title>T</dc:title><dc:identifier>https://gallica.bnf.fr/ark:/1</dc:identifier>"
        f"</srw:recordData><!--{_PAD}--></r>",
    ),
    (
        "search_ndl",
        '<r xmlns:srw="http://www.loc.gov/zing/srw/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><srw:recordData>'
        "<dc:title>T</dc:title><dc:identifier>https://iss.ndl.go.jp/books/1</dc:identifier>"
        f"</srw:recordData><!--{_PAD}--></r>",
    ),
    ("search_sep", f'<html><a href="?entry=/entries/kant/"><b>Kant</b></a><!--{_PAD}--></html>'),
    (
        "search_nominatim",
        json.dumps([{"display_name": "Paris", "osm_id": 1, "osm_type": "node", "pad": _PAD}]),
    ),
    (
        "search_uk_legislation",
        json.dumps({"items": [{"title": "An Act", "href": "/ukpga/1", "year": 2020}], "pad": _PAD}),
    ),
    # Control: this already went through the capped helpers. (`search_sep`
    # above covers the `_get_html` side.)
    (
        "search_openalex",
        json.dumps(
            {"results": [{"display_name": "W", "id": "https://openalex.org/W1"}], "pad": _PAD}
        ),
    ),
]


@pytest.mark.parametrize("fn_name, body", _OVERSIZED, ids=[n for n, _ in _OVERSIZED])
def test_an_oversized_response_is_refused_at_every_egress_site(monkeypatch, fn_name, body):
    """A source that streams without end must fail, not exhaust memory — at
    every site, not only the two that remembered to call `_read_capped`."""
    served = []

    class _Counting(_Resp):
        def read(self, amt=-1):
            out = super().read(amt)
            served.append(len(out))
            return out

    monkeypatch.setattr(
        sources.urllib.request, "urlopen", lambda req, timeout=None: _Counting(body.encode())
    )
    fn = getattr(sources, fn_name)

    monkeypatch.setattr(sources, "_MAX_BYTES", 10_000_000)
    assert len(fn("q", limit=1)) == 1, "the fixture body must parse when it fits"

    served.clear()
    monkeypatch.setattr(sources, "_MAX_BYTES", 512)
    assert fn("q", limit=1) == []
    assert sum(served) <= 513, f"{fn_name} pulled {sum(served)} bytes past a 512-byte cap"


def test_bodies_are_capped(monkeypatch):
    monkeypatch.setattr(sources, "_MAX_BYTES", 8)
    with pytest.raises(ValueError, match="refusing"):
        sources._read_capped(io.BytesIO(b"x" * 64))


def test_a_body_at_the_cap_is_still_read(monkeypatch):
    monkeypatch.setattr(sources, "_MAX_BYTES", 8)
    assert sources._read_capped(io.BytesIO(b"x" * 8)) == b"x" * 8


def test_the_module_never_reaches_for_requests():
    """A zero-dependency package must not behave differently because something
    unrelated installed `requests`. The vendored original tried it first and
    fell back to urllib, which also meant a failed request was issued twice."""
    import pathlib

    assert "import requests" not in pathlib.Path(sources.__file__).read_text(encoding="utf-8")


# ── Unreachable is not empty ────────────────────────────────────────────────


def test_search_records_which_sources_failed(monkeypatch):
    def _resolve(fn_name):
        if fn_name == "search_arxiv":

            def _boom(q, n):
                raise RuntimeError("arxiv is down")

            return _boom
        return lambda q, n: [
            sources._result(title="t", url="https://loc.gov/1", source="loc", institution="LoC")
        ]

    monkeypatch.setattr(sources, "_resolve_fn", _resolve)
    out = sources.search("q", sources=["arxiv", "loc"])
    assert set(out["failed"]) == {"arxiv"}
    assert "arxiv is down" in out["failed"]["arxiv"]


def test_a_source_that_swallows_its_own_error_is_still_recorded(monkeypatch):
    """Most sources catch and return [] so one outage cannot sink the fan-out.
    Without the breadcrumb in `_urlopen`, that makes an unreachable source look
    identical to an empty one — which is how a fully blocked egress reported
    ok=true, total=0."""

    def _swallowing_source(q, n):
        try:
            with sources._urlopen(sources.urllib.request.Request("https://example.org")):
                pass
        except Exception:
            return []
        return []

    monkeypatch.setattr(sources, "_resolve_fn", lambda fn: _swallowing_source)

    def refuse(req, timeout=None):
        raise OSError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(sources.urllib.request, "urlopen", refuse)

    out = sources.search("q", sources=["arxiv"])
    assert "arxiv" in out["failed"], "a swallowed transport error must still surface"
    assert "403" in out["failed"]["arxiv"]


def test_an_empty_but_reachable_source_is_not_marked_failed(monkeypatch):
    monkeypatch.setattr(sources, "_resolve_fn", lambda fn: lambda q, n: [])
    out = sources.search("q", sources=["arxiv"])
    assert out["failed"] == {}, "reached it, it had nothing — that is not a failure"
    assert out["results"] == {"arxiv": []}, "and it is still accounted for"


def test_a_read_phase_failure_is_recorded_not_read_as_empty(monkeypatch):
    """The breadcrumb used to be left only by `_urlopen`, so it covered the
    connect and nothing after it. `_get` wraps the connect, the read and the
    JSON decode in one `try`, and a body that arrived and then failed to parse
    returned None with no trace — the source reported as merely empty.

    A captive portal or a proxy answering 200 with an HTML error page where
    JSON was expected is exactly this shape. Reproduced before the fix:
    `failed == {}`.
    """
    connected = []

    def html_error_page(req, timeout=None):
        connected.append(req.full_url)
        return _Resp(b"<html><body>407 Proxy Authentication Required</body></html>")

    monkeypatch.setattr(sources.urllib.request, "urlopen", html_error_page)
    out = sources.search("q", sources=["openalex"])

    assert connected, "the connect must have succeeded — this is not a connect failure"
    assert "openalex" in out["failed"]
    assert "JSONDecodeError" in out["failed"]["openalex"]
    assert "openalex" not in out["results"]


def test_a_body_that_dies_mid_read_is_recorded(monkeypatch):
    """The other half of the same gap: the connect succeeds and the socket dies
    while the body is being read."""

    class _Truncated(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

        def read(self, n=-1):
            raise OSError("Connection reset by peer during read")

    monkeypatch.setattr(
        sources.urllib.request, "urlopen", lambda req, timeout=None: _Truncated(b"")
    )
    out = sources.search("q", sources=["openalex"])
    assert "Connection reset by peer" in out["failed"].get("openalex", "")


# ── Every dispatched source lands in exactly one bucket ─────────────────────
#
# The invariant behind the whole response shape. Each of the four findings this
# section covers was an instance of it breaking: a source that produced no hits
# disappeared from the response rather than saying why, and a consumer deciding
# "outage or empty shelf?" from the remainder got a healthy-looking answer.

BUCKETS = ("results", "skipped", "failed", "timed_out")


def _assert_one_bucket_each(out):
    for sid in out["sources_queried"]:
        hit = [b for b in BUCKETS if sid in out[b]]
        assert len(hit) == 1, f"{sid} is in {hit or 'no bucket'}, want exactly one"
    for sid in out["unknown"]:
        assert sid not in out["sources_queried"], "unknown was never dispatched"


def test_the_bucket_invariant_catches_a_planted_double_and_a_planted_orphan():
    """Planted: the invariant helper every fan-out test below leans on must
    itself fire — on a source in two buckets, on one in none, and on an
    "unknown" source that was nevertheless dispatched — and stay quiet on a
    well-formed response. A helper that had stopped asserting would turn each
    of those tests green for nothing."""
    good = {
        "sources_queried": ["a", "b"],
        "unknown": ["zz"],
        "results": {"a": []},
        "skipped": {"b": "no key"},
        "failed": {},
        "timed_out": [],
    }
    _assert_one_bucket_each(good)

    doubled = dict(good, failed={"a": "boom"})
    with pytest.raises(AssertionError, match="want exactly one"):
        _assert_one_bucket_each(doubled)

    orphaned = dict(good, results={})
    with pytest.raises(AssertionError, match="no bucket"):
        _assert_one_bucket_each(orphaned)

    dispatched_unknown = dict(good, unknown=["a"])
    with pytest.raises(AssertionError, match="never dispatched"):
        _assert_one_bucket_each(dispatched_unknown)


def _fake_sources(monkeypatch, fns: dict):
    """Register `{sid: fn}` as the whole registry, so a test can build a
    fan-out with the exact mix of outcomes it wants."""
    reg = {
        sid: {
            "name": sid,
            "fn_name": f"search_{sid}",
            "key_required": False,
            "key_env": "",
            "opt_in": False,
            "enabled": True,
        }
        for sid in fns
    }
    monkeypatch.setattr(sources, "_load_registry", lambda: dict(reg))
    monkeypatch.setattr(sources, "_resolve_fn", lambda fn_name: fns.get(fn_name[len("search_") :]))
    return reg


def test_every_dispatched_source_lands_in_exactly_one_bucket(monkeypatch):
    """One fan-out with every outcome at once."""
    release = threading.Event()

    def _hits(q, n):
        return [sources._result(title="t", url="https://e.org/1", source="s", institution="I")]

    def _empty(q, n):
        return []

    def _boom(q, n):
        raise RuntimeError("down")

    def _slow(q, n):
        release.wait(30)
        return []

    def _never(q, n):
        raise AssertionError("a source missing its key must not be dispatched")

    fns = {"withhits": _hits, "empty": _empty, "broken": _boom, "slow": _slow, "keyed": _never}
    reg = _fake_sources(monkeypatch, fns)
    reg["keyed"] = {
        "name": "keyed",
        "fn_name": "search_keyed",
        "key_required": True,
        "key_env": "TEST_ONLY_KEY",
        "opt_in": False,
        "enabled": True,
    }
    monkeypatch.delenv("TEST_ONLY_KEY", raising=False)

    try:
        out = sources.search(
            "q",
            sources=["withhits", "empty", "broken", "slow", "keyed", "typo"],
            wall_clock_limit=0.5,
        )
    finally:
        release.set()

    _assert_one_bucket_each(out)
    assert sorted(out["sources_queried"]) == sorted(reg)
    assert out["unknown"] == ["typo"]
    assert list(out["results"]["withhits"]) and out["results"]["empty"] == []
    assert set(out["results"]) == {"withhits", "empty"}
    assert set(out["failed"]) == {"broken"}
    assert out["skipped"] == {"keyed": "TEST_ONLY_KEY is not set"}
    assert out["timed_out"] == ["slow"]
    assert out["total"] == 1


def test_a_blocked_egress_leaves_nothing_looking_healthy(monkeypatch):
    """The finding, end to end, over the real default fan-out. With every
    request refused, five key_required sources abstained with a bare `return []`
    and entered neither `results` nor `failed` — measured 60 queried, 55 failed
    — so a consumer's `len(failed) >= len(queried)` never fired and a wholly
    blocked egress reported as a successful empty search."""
    for var in (
        "RIJKSMUSEUM_API_KEY",
        "DPLA_API_KEY",
        "SMITHSONIAN_API_KEY",
        "EUROPEANA_API_KEY",
        "BHL_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    def refuse(req, timeout=None):
        raise OSError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(sources.urllib.request, "urlopen", refuse)
    out = sources.search("obscure", wall_clock_limit=60.0)

    _assert_one_bucket_each(out)
    assert not out["results"], "nothing was reachable, so nothing was reached"
    looked = (
        set(out["sources_queried"])
        - set(out["failed"])
        - set(out["skipped"])
        - set(out["timed_out"])
    )
    assert looked == set(), "no source actually looked — that must be visible"
    assert set(out["skipped"]) == {"rijksmuseum", "dpla", "smithsonian", "europeana", "bhl"}


def test_a_missing_key_is_reported_with_the_variable_named(monkeypatch):
    """`skipped` has to say *which* key, or a caller cannot act on it."""
    monkeypatch.delenv("SMITHSONIAN_API_KEY", raising=False)
    out = sources.search("q", sources=["smithsonian"])

    assert out["skipped"] == {"smithsonian": "SMITHSONIAN_API_KEY is not set"}
    assert out["sources_queried"] == ["smithsonian"], "abstaining is not vanishing"
    assert out["failed"] == {} and out["results"] == {}


def test_a_keyed_source_with_its_key_present_is_dispatched(monkeypatch):
    """The abstention check must gate on the variable, not on `key_required` —
    otherwise setting the key would not buy you the source."""
    monkeypatch.setenv("SMITHSONIAN_API_KEY", "sekret")
    monkeypatch.setattr(sources, "_resolve_fn", lambda fn: lambda q, n: [])
    out = sources.search("q", sources=["smithsonian"])
    assert out["skipped"] == {}
    assert out["results"] == {"smithsonian": []}


def test_key_env_declarations_match_what_the_functions_actually_do(monkeypatch):
    """The registry now claims a source abstains without a named variable, and
    `search` acts on that claim before dispatching. Pin the claim to the code:
    with the variable unset, the function must return [] without touching the
    network."""

    def explode(req, timeout=None):
        raise AssertionError("a key_env source must not reach the network unkeyed")

    monkeypatch.setattr(sources.urllib.request, "urlopen", explode)
    for sid, cfg in sources._load_registry().items():
        if not cfg["key_env"]:
            continue
        monkeypatch.delenv(cfg["key_env"], raising=False)
        fn = sources._resolve_fn(cfg["fn_name"])
        assert fn("q", 1) == [], f"{sid} does not abstain on {cfg['key_env']}"


def test_semantic_scholar_is_not_declared_as_abstaining(monkeypatch):
    """It has no `key_env` because it does not abstain: without
    SEMANTIC_SCHOLAR_API_KEY it queries anonymously and the key only lifts rate
    limits. Declaring one would skip a source that works. The registry said
    `key_required: True`, which `list_sources()` reported to callers."""
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    reached = []
    monkeypatch.setattr(
        sources.urllib.request,
        "urlopen",
        lambda req, timeout=None: reached.append(req.full_url) or _Resp(b'{"data": []}'),
    )

    assert sources._load_registry()["semantic_scholar"]["key_env"] == ""
    sources.search_semantic_scholar("q", limit=1)
    assert reached, "it queries without a key, so it must not be marked as needing one"


# ── The wall clock: dropped is not vanished, and not the next call's problem ─


def test_a_timed_out_source_is_recorded_not_dropped(monkeypatch):
    """It used to be logged as "still pending" and forgotten — in neither
    `results` nor `failed`. Asked and unanswered is its own outcome."""
    release = threading.Event()

    def _slow(q, n):
        release.wait(30)
        return []

    def _fast(q, n):
        return [sources._result(title="t", url="https://e.org/1", source="s", institution="I")]

    _fake_sources(monkeypatch, {"slow": _slow, "fast": _fast})
    try:
        out = sources.search("q", sources=["slow", "fast"], wall_clock_limit=0.4)
    finally:
        release.set()

    assert out["timed_out"] == ["slow"]
    assert set(out["results"]) == {"fast"}
    _assert_one_bucket_each(out)


def test_a_slow_call_does_not_starve_the_next_one(monkeypatch):
    """`_SEARCH_EXECUTOR` was one pool of 16 workers shared by every call. A
    straggler cannot be killed, so it kept its worker after `search` returned
    and the next call queued behind it. Measured on the shared pool: after 16
    sources were left sleeping past a 0.4s cap, a following call whose single
    source returned instantly produced no results at all in 3.0s."""
    release = threading.Event()

    def _blocked(q, n):
        release.wait(30)
        return []

    def _instant(q, n):
        return [sources._result(title="t", url="https://e.org/1", source="s", institution="I")]

    try:
        hogs = {f"hog{i}": _blocked for i in range(sources._MAX_WORKERS)}
        _fake_sources(monkeypatch, hogs)
        first = sources.search("q", sources=list(hogs), wall_clock_limit=0.3)
        assert len(first["timed_out"]) == sources._MAX_WORKERS

        _fake_sources(monkeypatch, {"instant": _instant})
        started = time.monotonic()
        out = sources.search("q", sources=["instant"], wall_clock_limit=3.0)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert out["results"].get("instant"), (
        "the previous call's abandoned work must not own this call's workers"
    )
    assert elapsed < 1.0, f"queued behind the stragglers ({elapsed:.2f}s)"


def test_the_returned_buckets_are_a_snapshot(monkeypatch):
    """`_call` used to write into the `failed` dict from its worker. After the
    wall clock fired, `search` returned while stragglers were still running and
    still writing, so a consumer iterating the result could hit
    `RuntimeError: dictionary changed size during iteration` — reproduced, with
    the dict growing from 3 to 15 entries after the return. Dict writes are
    individually atomic under the GIL, so nothing was corrupted; the returned
    value simply described no single moment. Workers now return their outcome
    and only the calling thread records it."""
    release = threading.Event()

    def _late_failure(q, n):
        release.wait(30)
        raise RuntimeError("late boom")

    def _now_failure(q, n):
        raise RuntimeError("immediate boom")

    fns = {f"late{i}": _late_failure for i in range(8)}
    fns.update({f"now{i}": _now_failure for i in range(3)})
    _fake_sources(monkeypatch, fns)

    try:
        out = sources.search("q", sources=list(fns), wall_clock_limit=0.4)
        snapshot = {b: copy.deepcopy(out[b]) for b in BUCKETS}
        assert len(out["failed"]) == 3, "the three immediate failures"
        release.set()
        # Let every straggler finish and raise, then check nothing moved.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for _ in list(out["failed"]):
                pass
            time.sleep(0.01)
    finally:
        release.set()

    assert {b: out[b] for b in BUCKETS} == snapshot, (
        "a straggler wrote into a dict the caller already holds"
    )


def test_no_registered_source_requests_over_plain_http():
    """The scheme guard is https-only, so a source pointing at `http://` fails
    at runtime rather than at review. This is what makes https-only free.

    Matches on request URLs rather than on the literal, because the `http://`
    strings inside `search_arxiv`, `search_gallica` and `search_ndl` are XML
    namespace URIs — identifiers, never fetched.
    """
    import inspect
    import re as _re

    registered = {cfg.get("fn_name") or f"search_{sid}" for sid, cfg in sources.SOURCES.items()}
    # A plain-http string that is *assigned to a url* or passed to a fetch
    # helper, as opposed to bound as a namespace constant.
    requesting = _re.compile(r'(?:url\s*=\s*|_get\w*\(|_fetch\()\s*\(?\s*f?["\']http://')

    offenders = sorted(
        name
        for name in registered
        if (fn := getattr(sources, name, None)) and requesting.search(inspect.getsource(fn))
    )
    assert not offenders, (
        f"registered source(s) request over plain http: {offenders} — the "
        "guard refuses these at runtime"
    )


def test_the_module_defines_no_function_the_registry_does_not_use():
    """Four functions were vendored and never registered — `search_omdb`,
    `search_isfdb`, `search_fbi_vault`, `search_ig_nobel`. Dead code that reads
    as capability: `list_sources` never showed them, no fan-out ever called
    them, and two of them shaped the https-only decision by looking like live
    sources that needed plain http. Deleted; this keeps the next one out."""
    import re as _re
    from pathlib import Path

    defined = set(
        _re.findall(
            r"^def (search_[a-z0-9_]+)",
            Path(sources.__file__).read_text(encoding="utf-8"),
            _re.M,
        )
    )
    registered = {cfg.get("fn_name") or f"search_{sid}" for sid, cfg in sources.SOURCES.items()}
    assert defined - registered == set(), "defined but unreachable — register it or delete it"
    assert registered - defined == set(), "registered with no function"


# ── question_to_intent: LLM-shaped intent extraction, ported from willow-2.0 ─
#
# jeles bundles no LLM client (zero-dependency promise), so `respond` is
# dependency-injected rather than imported — these tests cover both the
# injected path and the no-`respond` fallback, which is this function's only
# behaviour until a host supplies one.


def test_question_to_intent_without_a_responder_returns_the_question_unchanged():
    """The default, and upstream's own fallback whenever its LLM call raises —
    jeles simply has no other mode."""
    q = "Why do ships painted red on the bottom last longer?"
    assert sources.question_to_intent(q) == q


def test_question_to_intent_uses_the_responder_when_one_is_supplied():
    seen = []

    def fake_respond(system, question):
        seen.append((system, question))
        return "  antifouling copper paint ship hull protection  "

    out = sources.question_to_intent(
        "Why do ships painted red on the bottom last longer?",
        respond=fake_respond,
    )
    assert out == "antifouling copper paint ship hull protection", (
        "the responder's answer is stripped"
    )
    assert seen == [
        (sources._INTENT_SYSTEM_PROMPT, "Why do ships painted red on the bottom last longer?")
    ], "respond(system, question) — the exact upstream call shape"


def test_question_to_intent_caps_the_responder_at_200_chars():
    out = sources.question_to_intent("q", respond=lambda s, q: "x" * 500)
    assert len(out) == 200


def test_question_to_intent_falls_back_when_the_responder_raises():
    def boom(system, question):
        raise RuntimeError("llm edge unreachable")

    q = "What is habeas corpus?"
    assert sources.question_to_intent(q, respond=boom) == q


def test_question_to_intent_falls_back_when_the_responder_returns_nothing():
    q = "Who invented the telephone?"
    assert sources.question_to_intent(q, respond=lambda s, q: "") == q
    assert sources.question_to_intent(q, respond=lambda s, q: None) == q


# ── The prose gate: PROSE_UNSAFE_SOURCES / _is_prose ────────────────────────


@pytest.mark.parametrize(
    ("query", "prose"),
    [
        ("aspirin", False),
        ("USD EUR rate", False),
        ("one two three four five", False),  # 5 words — under threshold
        ("one two three four five six", True),  # 6 words — at threshold
        ("what pain reliever did Bayer patent in 1899", True),
    ],
)
def test_is_prose_is_a_word_count_threshold(query, prose):
    assert sources._is_prose(query) is prose


def test_prose_unsafe_sources_are_all_real_registered_sources():
    """A stale id here would silently gate nothing — pin every entry to the
    live registry so a rename on one side cannot drift from the other."""
    assert set(sources.SOURCES) >= sources.PROSE_UNSAFE_SOURCES


def test_a_prose_query_gates_structured_sources_before_dispatch(monkeypatch):
    called = []
    monkeypatch.setattr(sources, "_resolve_fn", lambda fn: lambda q, n: called.append(fn) or [])
    out = sources.search(
        "what pain reliever did Bayer patent in 1899",
        sources=["pubchem", "openalex"],
    )
    assert called == ["search_openalex"], (
        "pubchem must never reach `_resolve_fn` at all — gated pre-dispatch"
    )
    assert out["skipped"]["pubchem"] == "prose query vs structured-only source (willow-2.0 #650)"
    assert out["sources_queried"] == ["pubchem", "openalex"], (
        "gated is not vanished — it is accounted for, same as a missing key"
    )
    assert "pubchem" not in out["results"] and "pubchem" not in out["failed"]


def test_a_short_query_does_not_gate_structured_sources(monkeypatch):
    monkeypatch.setattr(sources, "_resolve_fn", lambda fn: lambda q, n: [])
    out = sources.search("aspirin", sources=["pubchem"])
    assert out["skipped"] == {}
    assert out["results"] == {"pubchem": []}


def test_naming_a_prose_unsafe_source_explicitly_does_not_bypass_the_gate(monkeypatch):
    """`sources=[...]` targets a source by id; it must not be a way around a
    gate that exists precisely because the query and the source disagree."""
    monkeypatch.setattr(
        sources,
        "_resolve_fn",
        lambda fn: (
            lambda q, n: (_ for _ in ()).throw(
                AssertionError("a gated source must not be dispatched")
            )
        ),
    )
    out = sources.search(
        "the constitutional rights of a sentient cheese republic",
        sources=["pubchem"],
    )
    assert out["results"] == {} and out["failed"] == {}
    assert "pubchem" in out["skipped"]


# ── The cache: _write_cache / _read_cache ────────────────────────────────────
#
# Ported from willow-2.0's `_write_cache` (append-only JSONL log) plus the
# load step of its read-side counterpart, `promote_jeles_cache.py`'s
# `_load_cache()`. Disabled unless `JELES_SOURCES_CACHE_DIR` is set — these
# tests point it at `tmp_path`, never a real path on the machine running them.


def test_caching_is_disabled_by_default():
    assert sources._CACHE_DIR == "", "no JELES_SOURCES_CACHE_DIR in the test environment means off"


def test_write_cache_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(sources, "_CACHE_DIR", "")
    sources._write_cache(
        "q",
        {
            "loc": [
                sources._result(title="t", url="https://loc.gov/1", source="loc", institution="LoC")
            ]
        },
    )
    assert sources._read_cache() == [], "disabled means nothing was written or read"


def test_read_cache_is_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(sources, "_CACHE_DIR", "")
    assert sources._read_cache() == []


def test_write_then_read_cache_round_trips_the_hit(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_CACHE_DIR", str(tmp_path))
    hit = sources._result(
        title="Signed policy bundles",
        url="https://doi.org/10.1/x",
        source="openalex",
        institution="Cornell University",
        snippet="An abstract.",
        date="2026",
        rid="W1",
    )

    sources._write_cache("policy bundle signing", {"openalex": [hit]})

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1, "one file per UTC day"

    records = sources._read_cache()
    assert len(records) == 1
    rec = records[0]
    assert rec["title"] == "Signed policy bundles"
    assert rec["url"] == hit["url"]
    assert rec["query"] == "policy bundle signing"
    assert rec["tier"] == "fetched"
    assert rec["promoted"] is False
    assert rec["confidence"] == sources._SOURCE_CONFIDENCE["openalex"]
    assert "openalex" in rec["tags"]
    assert rec["id"] and rec["id"] != hit["id"], (
        "the cache id is a hash of the url, distinct from the citation's own id"
    )
    assert rec["fetched_at"]


def test_write_cache_dedupes_and_caps_keywords(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_CACHE_DIR", str(tmp_path))
    hit = sources._result(title="t", url="https://loc.gov/1", source="loc", institution="LoC")
    sources._write_cache("history history history archive", {"loc": [hit]})
    rec = sources._read_cache()[0]
    assert rec["keywords"].count("history") == 1, "deduped, not repeated"
    assert len(rec["keywords"]) <= 10


def test_write_cache_falls_back_to_default_confidence_for_an_untiered_source(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_CACHE_DIR", str(tmp_path))
    hit = sources._result(
        title="t", url="https://e.org/1", source="not-a-real-source", institution="I"
    )
    sources._write_cache("q", {"not-a-real-source": [hit]})
    assert sources._read_cache()[0]["confidence"] == 0.80


def test_read_cache_skips_a_malformed_line_rather_than_failing_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_CACHE_DIR", str(tmp_path))
    good = json.dumps({"title": "good", "url": "https://e.org/1"})
    (tmp_path / "2026-08-10.jsonl").write_text(
        good + "\nnot json at all\n\n" + good + "\n", encoding="utf-8"
    )
    records = sources._read_cache()
    assert len(records) == 2
    assert all(r["title"] == "good" for r in records)


def test_write_cache_swallows_a_directory_it_cannot_create(tmp_path, monkeypatch, caplog):
    """A full disk or an unwritable path must not turn a successful search
    into a failed one — the same contract `_get`/`_get_html` hold for the
    network side of this module."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setattr(sources, "_CACHE_DIR", str(blocked / "cache"))
    hit = sources._result(title="t", url="https://e.org/1", source="loc", institution="LoC")
    sources._write_cache("q", {"loc": [hit]})  # must not raise
    assert sources._read_cache() == [], "nothing was written"


def test_search_writes_the_cache_only_when_there_are_hits(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(sources, "_resolve_fn", lambda fn: lambda q, n: [])
    sources.search("q", sources=["arxiv"])
    assert sources._read_cache() == [], "reached, nothing found — nothing to cache"

    monkeypatch.setattr(
        sources,
        "_resolve_fn",
        lambda fn: (
            lambda q, n: [
                sources._result(title="t", url="https://loc.gov/1", source="loc", institution="LoC")
            ]
        ),
    )
    sources.search("q", sources=["loc"])
    records = sources._read_cache()
    assert len(records) == 1 and records[0]["source"] == "loc"


# ── question_to_query ───────────────────────────────────────────────────────
#
# Exported, documented, and until now called by nothing in this package and
# covered by no test — a host-facing helper that had never been run in anger.
# Two defects were sitting in it, both invisible without a test:
#
#   * `_LONE_THE` was compiled with re.IGNORECASE, which also folds the
#     `[A-Z]` in its negative lookahead. The lookahead therefore succeeded
#     before every word and the pattern stripped nothing — the one article it
#     exists to remove survived every query.
#   * `a` sat in the IGNORECASE filler list, so a lone capital letter was
#     deleted: "drug A" and "drug B" produced the same search. `corpus.py`
#     already knew this and keeps "a" and "i" out of its stop set.


def test_question_words_and_fillers_are_stripped():
    from jeles.sources import question_to_query

    assert question_to_query("What is the capital of France?") == "capital France"
    assert question_to_query("Tell me about the primary color in Grove?") == "primary color Grove"


def test_a_lone_capital_letter_survives():
    """ "drug A" and "drug B" must not collapse into the same query — the
    query-side twin of test_corpus.py::test_single_letters_stay_meaning_bearing.
    """
    from jeles.sources import question_to_query

    a = question_to_query("Does drug A interact with X?")
    b = question_to_query("Does drug B interact with X?")
    assert a == "drug A interact X"
    assert a != b
    assert question_to_query("What is vitamin A deficiency?") == "vitamin A deficiency"


def test_a_lowercase_article_is_still_filler():
    """Keeping the capital letter must not keep the article it looks like."""
    from jeles.sources import question_to_query

    assert question_to_query("What is a nugget?") == "nugget"


def test_the_is_kept_when_it_introduces_a_proper_noun():
    """The negative lookahead is the whole mechanism, and it only works
    case-sensitively — which is why `_LONE_THE` is not IGNORECASE."""
    from jeles.sources import question_to_query

    assert question_to_query("Tell me about The Hague") == "The Hague"
    assert question_to_query("Who wrote The Beatles biography?") == "The Beatles biography"
    assert question_to_query("How did the Roman Empire fall?") == "the Roman Empire fall"


def test_the_is_dropped_when_it_introduces_nothing():
    from jeles.sources import question_to_query

    assert question_to_query("What is the answer?") == "answer"


def test_a_question_that_reduces_to_nothing_falls_back_to_itself():
    """The fallback returns the original rather than an empty query — a search
    for "" is worse than a search for the words the asker actually used."""
    from jeles.sources import question_to_query

    assert question_to_query("What is a?") == "What is a"
    assert question_to_query("is of the") == "is of the"


def test_punctuation_only_input_is_not_a_query():
    """Known edge, pinned rather than fixed: nothing survives and the fallback
    has nothing to fall back to. A caller must not send this to a source."""
    from jeles.sources import question_to_query

    assert question_to_query("???") == ""


def test_zenodo_names_the_repository_not_the_record_owner(monkeypatch):
    """`owners` is a list of dicts, and `[0]` put one in `institution` — a
    field `_result` declares as `str` and every other adapter fills with one.

    It did not raise, which is why it lasted: `_text` coerces with
    `str(value)`, so the institution silently became the literal text
    "{'id': '1342605'}". `verify._identity` counts this field to decide
    corroboration, so each owner rendered as a separate institution and two
    records by two owners read as two independent institutions agreeing —
    which is the one thing corroboration is supposed to mean.
    """
    _stub_json(
        monkeypatch,
        {
            "hits": {
                "hits": [
                    {
                        "id": 99,
                        "owners": [{"id": "1342605"}],
                        "metadata": {
                            "title": "A record with an owner",
                            "doi": "10.5281/zenodo.99",
                            "publication_date": "2026-01-01",
                            "description": "body",
                        },
                    }
                ]
            }
        },
    )
    hits = sources.search_zenodo("anything", limit=1)
    assert hits and set(hits[0]) == CITATION_KEYS
    assert isinstance(hits[0]["institution"], str), (
        "institution must be a string whatever the upstream record carries"
    )
    assert hits[0]["institution"] == "Zenodo / CERN"


def test_every_zenodo_hit_names_an_institution_even_with_no_owners(monkeypatch):
    _stub_json(
        monkeypatch,
        {
            "hits": {
                "hits": [
                    {
                        "id": 100,
                        "metadata": {"title": "No owners here", "publication_date": "2026-01-01"},
                    }
                ]
            }
        },
    )
    hits = sources.search_zenodo("anything", limit=1)
    assert hits[0]["institution"] == "Zenodo / CERN"
