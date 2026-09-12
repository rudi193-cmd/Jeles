"""sources — the institutional and academic collections themselves.

The third hop of the persona's mandate ("local KB → open web → special
collections"), and it lives *here* rather than behind a service. 65 registered
sources, 61 of them in the default fan-out — arXiv, PubMed, Crossref, OpenAlex,
Library of Congress, Europeana, CourtListener, the Smithsonian — each a small
function that queries one public API and returns citable results. (Read the
count off `SOURCES`; four files said "~65" and none of them was right.)

Originally `jeles-remote/sources.py`, itself ported from willow-2.0's
`core/jeles_sources.py`. Moving it into the package puts it in the same
relationship the corpus already has with its server:

    corpus.py   (pure)  ←  corpus_server.py  (thin MCP wrapper)
    sources.py  (pure)  ←  jeles-remote      (thin FastAPI wrapper)

A hosted deployment is then an optional convenience rather than a prerequisite:
nothing here needs a secret, a Fly.io app, or a second repository in the test
loop to work.

Each source function returns list[dict] with standard citation fields:
    title, url, source, institution, snippet, date, id

Each registry entry also declares `hosts` — the hostnames that source contacts —
reachable in aggregate via `registered_hosts()` and checked against the code by
`tests/test_source_hosts.py`, so it is data rather than a second list to keep in
step. Note what it is not: 46 of the 65 sources build their citation URL out of
the API response, so where a *result* points is not knowable from here at all.
OpenAlex or Crossref can legitimately hand back a link to any publisher on
earth. `hosts` answers "which institutions does jeles query", never "should this
arbitrary web result be believed" — willow-mcp's trusted-domain list conflated
the two, and inherited `www.w3.org` from arXiv's Atom namespace as a result.

Wikipedia is excluded from the default (non opt-in) set on purpose — every
default result here can appear in an academic bibliography.

Stdlib only (urllib, json, xml.etree, concurrent.futures) so the package keeps
its zero-runtime-dependency promise, and **no network at import**: the thread
pool is built by `search`, per call, not on load.

Sources needing an API key read a plain environment variable and abstain when
it is absent — an unkeyed source is missing, never a failure. `search` reports
each one in `skipped` with the variable named, because silently contributing
zero results is indistinguishable from the collection being empty:
    RIJKSMUSEUM_API_KEY      — register at data.rijksmuseum.nl
    DPLA_API_KEY             — free instant key at dp.la/info/developers/codex/
    SMITHSONIAN_API_KEY      — register at api.si.edu
    EUROPEANA_API_KEY        — register at apis.europeana.eu
    BHL_API_KEY              — Biodiversity Heritage Library
    SEMANTIC_SCHOLAR_API_KEY — free at semanticscholar.org. Optional, unlike the
                               rest: the source queries anonymously without it
                               and the key only lifts rate limits.
"""

from __future__ import annotations

import concurrent.futures as _cf
import hashlib
import json
import logging
import os
import re
import sys as _sys
import threading as _threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from jeles import _egress

log = logging.getLogger("jeles.sources")

_TIMEOUT = 15
# Several of these APIs (Crossref, NCBI) ask for a contact address in the
# User-Agent and rate-limit anonymous traffic harder. Override with
# JELES_SOURCES_UA to identify your own deployment.
# NCBI (PubMed) asks callers to identify themselves with tool + email and
# throttles anonymous traffic. Same idea as the User-Agent: overridable, and
# sent rather than omitted.
_NCBI_TOOL = os.environ.get("JELES_SOURCES_NCBI_TOOL", "jeles")
# Also used for OpenAlex/Crossref "polite pool" mailto, which buys better
# rate limits from both.
_CONTACT_EMAIL = os.environ.get("JELES_SOURCES_CONTACT_EMAIL", "rudi193@gmail.com")

_UA = os.environ.get(
    "JELES_SOURCES_UA",
    "Jeles/1.0 (academic librarian; +https://github.com/rudi193-cmd/Jeles)",
)

# Confidence by source tier — primary institutions > peer-reviewed > aggregators
_SOURCE_CONFIDENCE: dict[str, float] = {
    "loc": 0.92,
    "met": 0.92,
    "cleveland": 0.92,
    "vam": 0.92,
    "nasa": 0.92,
    "ndl": 0.92,
    "gallica": 0.92,
    "smithsonian": 0.92,
    "pubmed": 0.90,
    "arxiv": 0.90,
    "crossref": 0.90,
    "europepmc": 0.90,
    "openalex": 0.85,
    "core": 0.88,
    "doaj": 0.88,
    "hal": 0.88,
    "zenodo": 0.65,
    "datacite": 0.88,
    "scielo": 0.88,
    "usgs": 0.90,
    "europeana": 0.88,
    "rijksmuseum": 0.90,
    "openlibrary": 0.82,
    "internet_archive": 0.80,
    "wikidata": 0.80,
    "chronicling_america": 0.85,
    "dpla": 0.83,
    "semantic_scholar": 0.87,
    "pubchem": 0.92,
    "wikipedia": 0.60,
    "psychiatric_times": 0.75,
    "inspirehep": 0.92,
    "worldbank": 0.92,
    "openfoodfacts": 0.82,
    "carbon_intensity": 0.92,
    "nws": 0.92,
    "gdelt": 0.65,
    "who_gho": 0.95,
    "open_meteo": 0.90,
    "patentsview": 0.92,
    "imf": 0.95,
    "osf": 0.80,
    "thesportsdb": 0.78,
    "frankfurter": 0.95,
}


_MAX_BYTES = int(os.environ.get("JELES_SOURCES_MAX_BYTES", str(4 * 1024 * 1024)))


_TRANSPORT_ERRORS = _threading.local()


def _note_transport_failure(exc: Exception) -> None:
    """Record a fetch failure that `_get`/`_get_html` swallowed.

    Those helpers return None so one dead source cannot sink the fan-out — but
    that means the source function returns [] and looks merely empty. This
    breadcrumb is per-thread (each source runs on its own worker) so `search`
    can tell "reached it, nothing there" from "never reached it".

    Noted for the whole fetch-and-decode path, not just the connect. It was
    once only called from `_urlopen`, which meant a body that arrived and then
    failed to read or parse left no trace: a captive portal answering 200 with
    an HTML page where JSON was expected reported as an empty collection.
    Reproduced — `search("q", sources=["openalex"])` against such a page
    returned `failed == {}`.
    """
    _TRANSPORT_ERRORS.last = f"{type(exc).__name__}: {exc}"


def _take_transport_failure() -> str | None:
    err = getattr(_TRANSPORT_ERRORS, "last", None)
    _TRANSPORT_ERRORS.last = None
    return err


# https only, which is stricter than the other two egress lanes — those are
# aimed at an address the operator chose, this one at sixty public APIs.
#
# Nothing here needs plain http. The two functions that used it (`search_omdb`,
# `search_isfdb`) were vendored dead, never registered, and have been deleted;
# the remaining plain-`http://` strings, in `search_arxiv`, `search_gallica`
# and `search_ndl`, are XML namespace URIs — identifiers, never fetched.
# `test_no_registered_source_requests_over_plain_http` fails if a source is
# added that does request over http: confirm the host serves TLS and use it,
# rather than widening this back.
_ALLOWED_SCHEMES = _egress.HTTPS_ONLY

# Re-exported so the scheme/redirect tests in tests/test_sources.py keep naming
# this module. The definitions live in `jeles._egress`, shared with
# `institutional` and `reactions.search_adapter` — each of the three had written
# its own copy of this guard, and each had the same two bugs in it.
_scheme_ok = _egress.scheme_ok
_read_capped_impl = _egress.read_capped


def _SchemeGuardedRedirects() -> _egress.SchemeGuardedRedirects:
    return _egress.SchemeGuardedRedirects(_ALLOWED_SCHEMES)


def _opener() -> urllib.request.OpenerDirector:
    return _egress.opener(_ALLOWED_SCHEMES)


def _urlopen(req: urllib.request.Request, timeout: float | None = None):
    """The single egress point for every source, reached only through `_fetch`
    — so no source function ever holds a response it could read unbounded.

    Composed from `_egress` rather than calling `_egress.urlopen`, for two
    local reasons: the breadcrumb below, and keeping `_opener` a name on *this*
    module so the test suite can substitute it. Most sources catch their own
    errors and return [], so without the breadcrumb a source that could not be
    reached is indistinguishable from one that had nothing, and a whole blocked
    egress reports as a successful empty search.

    The pre-flight is `_egress.check_url`, not a local copy of it. This module
    used to restate the scheme check inline — and, having restated one of the
    two checks, silently did not have the other: a private *initial* URL was
    refused by `_egress.urlopen` and connected to here. On the one lane the
    destination guard is written for.
    """
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    _egress.check_url(url, _ALLOWED_SCHEMES)
    try:
        return _opener().open(req, timeout=_TIMEOUT if timeout is None else timeout)
    except Exception as exc:
        _note_transport_failure(exc)
        raise


def _read_capped(resp) -> bytes:
    """Read a bounded body, at this module's configured cap."""
    return _egress.read_capped(resp, _MAX_BYTES)


def _fetch(url: str, headers: dict | None = None, timeout: float | None = None) -> bytes:
    """Open a URL and return its bounded body — opening and reading in one
    call, so there is no moment where a caller holds an unread response.

    The cap used to be a rule each source had to remember, and six of the eight
    sites that opened a socket did not: `search_arxiv`, `search_gallica` and
    `search_ndl` handed a full `r.read()` to `_parse_xml`, which is precisely
    where that function's docstring claimed its input was bounded. Raises on
    failure; `_get`/`_get_html` are the swallowing variants.
    """
    h = {"User-Agent": _UA, **(headers or {})}
    with _urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return _read_capped(r)


def _parse_xml(raw: bytes):
    """Parse XML from a source.

    stdlib ElementTree does not resolve external entities, so the classic XXE
    file-read is not reachable here, and `_fetch` bounds the input — which it
    genuinely did not until every XML source was moved onto that helper. What
    remains is expansion-style abuse from a compromised or spoofed endpoint,
    which stdlib does not defend against — `defusedxml` is the upgrade if that
    threat model ever matters. Deliberately not a dependency: this package
    ships with none, and every XML source here is a fixed institutional
    endpoint over TLS.
    """
    return ET.fromstring(raw)  # nosec B314


def _get(url: str, headers: dict | None = None, timeout: float | None = None) -> dict | list | None:
    """Fetch JSON. Returns None on any failure — a dead source is a missing
    source, never an exception that sinks the fan-out."""
    try:
        return json.loads(_fetch(url, headers, timeout))
    except Exception as e:
        # Covers the read and the JSON decode as well as the connect. `_urlopen`
        # leaves its own breadcrumb, but the read and the decode are inside this
        # `try` too, and a truncated body or an HTML error page in place of JSON
        # is exactly the kind of failure that used to read as "nothing here".
        # Re-noting a connect error already noted is harmless: same exception.
        _note_transport_failure(e)
        log.warning("GET %s failed: %s", _egress.loggable(url), e)
        return None


def _get_html(url: str, headers: dict | None = None, timeout: float | None = None) -> str | None:
    """Fetch text. Same contract as `_get`: None rather than a raise."""
    try:
        return _fetch(url, headers, timeout).decode("utf-8", errors="replace")
    except Exception as e:
        _note_transport_failure(e)  # same reason as `_get`: read counts too
        log.warning("GET html %s failed: %s", _egress.loggable(url), e)
        return None


def _text(value) -> str:
    """Coerce an API field to a string.

    Sources do not honour their own documented types. Internet Archive returns
    `description` as a *list*; CORE returns a null journal title. `(value or
    "").strip()` raises AttributeError on the first and the exception is caught
    by `search()`'s `_call`, so the symptom is not a crash — the source lands in
    `failed` and contributes nothing, quietly, on every query. Ported from
    willow-2.0, which tracked it as #646/#647.
    """
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value if v is not None)
    return str(value) if value is not None else ""


def _result(
    title: str,
    url: str,
    source: str,
    institution: str,
    snippet: str = "",
    date: str = "",
    rid: str = "",
) -> dict:
    """Shape one hit into this module's citation dict.

    ``source`` here is the **registry key** — the adapter's own slug, e.g.
    ``"openalex"`` or ``"pubmed"`` (see ``SOURCES``/the module docstring's
    ``search_*`` list) — never the institution. ``institution`` is the
    per-record institution string each adapter assembles, which is the field
    that answers "who backs this" and can vary hit-to-hit even within one
    call to a single adapter (``search_openalex`` pulls a different author
    institution per paper).

    This is a *different* precedence from ``jeles.verify._identity``, which
    reads a citation's ``source`` field first and treats ``institution`` only
    as a fallback — correct for ``jeles.institutional``'s citations, where
    ``source`` genuinely *is* the institution, but wrong for a ``_result()``
    record passed through unchanged: every registry adapter fills ``source``
    with its own constant key, so ``institution`` — the one field that
    actually varies per record — is never read, and corroboration counting
    ends up counting *adapters*, not institutions. A host that hands
    ``search()``'s results to ``verify.verify_claims`` must re-label each
    citation first, e.g. ``{**hit, "source": hit["institution"] or
    hit["source"]}``, so the institution — not the registry key — is what
    ``_identity`` compares.
    """
    return {
        "title": _text(title).strip(),
        "url": url,
        "source": source,
        "institution": _text(institution).strip(),
        "snippet": _text(snippet).strip()[:400],
        "date": date,
        "id": rid,
    }


# ── ACADEMIC ──────────────────────────────────────────────────────────────────


def search_openalex(query: str, limit: int = 5) -> list[dict]:
    """OpenAlex — 200M+ scholarly works. No key required."""
    url = (
        "https://api.openalex.org/works?search="
        + urllib.parse.quote(query)
        + f"&per-page={limit}&mailto={urllib.parse.quote(_CONTACT_EMAIL)}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        doi = item.get("doi") or ""
        results.append(
            _result(
                title=item.get("display_name", ""),
                url=doi if doi else item.get("id", ""),
                source="openalex",
                institution=", ".join(
                    i.get("display_name", "")
                    for a in (item.get("authorships") or [])[:2]
                    for i in (a.get("institutions") or [])[:1]
                ),
                snippet=item.get("abstract", "") or "",
                date=str(item.get("publication_year", "")),
                rid=item.get("id", "").split("/")[-1],
            )
        )
    return results


def search_core(query: str, limit: int = 5) -> list[dict]:
    """CORE — open access full text. No key required."""
    url = (
        "https://api.core.ac.uk/v3/search/works?q=" + urllib.parse.quote(query) + f"&limit={limit}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        results.append(
            _result(
                title=item.get("title", ""),
                url=item.get("downloadUrl") or item.get("doi") or "",
                source="core",
                institution=item.get("publisher")
                or ", ".join(j.get("title", "") for j in (item.get("journals") or [])[:1]),
                snippet=item.get("abstract", "") or "",
                date=str(item.get("yearPublished", "")),
                rid=str(item.get("id", "")),
            )
        )
    return results


def search_doaj(query: str, limit: int = 5) -> list[dict]:
    """DOAJ — Directory of Open Access Journals. No key required."""
    url = "https://doaj.org/api/search/articles/" + urllib.parse.quote(query) + f"?pageSize={limit}"
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        bib = item.get("bibjson") or {}
        doi = next(
            (i.get("id", "") for i in (bib.get("identifier") or []) if i.get("type") == "doi"), ""
        )
        link = next((item.get("url", "") for item in (bib.get("link") or [])), "")
        results.append(
            _result(
                title=bib.get("title", ""),
                url=f"https://doi.org/{doi}" if doi else link,
                source="doaj",
                institution=(bib.get("journal") or {}).get("title", ""),
                snippet=bib.get("abstract", "") or "",
                date=f"{bib.get('year', '')}-{bib.get('month', '')}".strip("-"),
                rid=doi,
            )
        )
    return results


def search_europepmc(query: str, limit: int = 5) -> list[dict]:
    """Europe PMC — life sciences & biomedical. No key required."""
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query="
        + urllib.parse.quote(query)
        + f"&format=json&pageSize={limit}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in ((data.get("resultList") or {}).get("result") or [])[:limit]:
        pmid = item.get("pmid", "")
        doi = item.get("doi", "")
        results.append(
            _result(
                title=item.get("title", ""),
                url=f"https://doi.org/{doi}"
                if doi
                else f"https://europepmc.org/article/{item.get('source', '')}/{item.get('id', '')}",
                source="europepmc",
                institution=item.get("journalTitle", ""),
                snippet=item.get("abstractText", "") or "",
                date=item.get("firstPublicationDate", ""),
                rid=pmid or item.get("id", ""),
            )
        )
    return results


def search_semantic_scholar(query: str, limit: int = 5) -> list[dict]:
    """Semantic Scholar — AI-powered academic search. Free key recommended (SEMANTIC_SCHOLAR_API_KEY)."""
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    headers = {"x-api-key": key} if key else {}
    url = (
        "https://api.semanticscholar.org/graph/v1/paper/search?query="
        + urllib.parse.quote(query)
        + f"&fields=title,year,authors,externalIds,abstract,url&limit={limit}"
    )
    data = _get(url, headers=headers)
    if not data:
        return []
    results = []
    for item in (data.get("data") or [])[:limit]:
        ext = item.get("externalIds") or {}
        doi = ext.get("DOI", "")
        results.append(
            _result(
                title=item.get("title", ""),
                url=f"https://doi.org/{doi}" if doi else item.get("url", ""),
                source="semantic_scholar",
                institution=", ".join(a.get("name", "") for a in (item.get("authors") or [])[:3]),
                snippet=item.get("abstract", "") or "",
                date=str(item.get("year", "")),
                rid=item.get("paperId", ""),
            )
        )
    return results


def search_crossref(query: str, limit: int = 5) -> list[dict]:
    """Crossref DOI registry — journals, books, conference papers. No key required."""
    url = (
        "https://api.crossref.org/works?rows="
        + str(limit)
        + "&query="
        + urllib.parse.quote(query)
        + f"&mailto={urllib.parse.quote(_CONTACT_EMAIL)}"
    )
    data = _get(url)
    if not data:
        return []
    items = (data.get("message") or {}).get("items") or []
    results = []
    for item in items[:limit]:
        doi = item.get("DOI", "")
        titles = item.get("title") or [""]
        date_parts = (
            (item.get("published") or item.get("issued") or {}).get("date-parts") or [[]]
        )[0]
        date = "-".join(str(p) for p in date_parts) if date_parts else ""
        results.append(
            _result(
                title=titles[0] if titles else "",
                url=f"https://doi.org/{doi}" if doi else "",
                source="crossref",
                institution=item.get("publisher", ""),
                snippet=(item.get("abstract") or "")[:400],
                date=date,
                rid=doi,
            )
        )
    return results


def search_pubmed(query: str, limit: int = 5) -> list[dict]:
    """PubMed biomedical literature. No key required."""
    search_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        "?db=pubmed&retmode=json&retmax="
        + str(limit)
        + "&term="
        + urllib.parse.quote(query)
        + f"&tool={urllib.parse.quote(_NCBI_TOOL)}"
        + f"&email={urllib.parse.quote(_CONTACT_EMAIL)}"
    )
    search = _get(search_url)
    if not search:
        return []
    ids = (search.get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []
    summary_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        "?db=pubmed&retmode=json&id="
        + ",".join(ids)
        + f"&tool={urllib.parse.quote(_NCBI_TOOL)}"
        + f"&email={urllib.parse.quote(_CONTACT_EMAIL)}"
    )
    summary = _get(summary_url)
    if not summary:
        return []
    results = []
    for pmid in ids:
        doc = (summary.get("result") or {}).get(pmid) or {}
        if not doc or pmid == "uids":
            continue
        results.append(
            _result(
                title=doc.get("title", ""),
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                source="pubmed",
                institution="PubMed / National Library of Medicine",
                snippet=", ".join(a.get("name", "") for a in (doc.get("authors") or [])[:3]),
                date=doc.get("pubdate", ""),
                rid=pmid,
            )
        )
    return results


def search_arxiv(query: str, limit: int = 5) -> list[dict]:
    """arXiv preprints — STEM, CS, Math, Physics. No key required."""
    url = (
        "https://export.arxiv.org/api/query?search_query="
        + urllib.parse.quote(f"all:{query}")
        + f"&max_results={limit}&sortBy=relevance"
    )
    try:
        raw = _fetch(url)
    except Exception as e:
        log.warning("arXiv failed: %s", e)
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = _parse_xml(raw)
    except ET.ParseError:
        return []
    results = []
    for entry in root.findall("atom:entry", ns)[:limit]:
        arxiv_id = (entry.findtext("atom:id", "", ns) or "").split("/abs/")[-1]
        results.append(
            _result(
                title=(entry.findtext("atom:title", "", ns) or "").strip(),
                url=entry.findtext("atom:id", "", ns) or "",
                source="arxiv",
                institution="arXiv / Cornell University",
                snippet=(entry.findtext("atom:summary", "", ns) or "").strip(),
                date=entry.findtext("atom:published", "", ns) or "",
                rid=arxiv_id,
            )
        )
    return results


# ── DATA / SCIENCE ────────────────────────────────────────────────────────────


def search_zenodo(query: str, limit: int = 5) -> list[dict]:
    """Zenodo — CERN open research repository. No key required."""
    url = "https://zenodo.org/api/records?q=" + urllib.parse.quote(query) + f"&size={limit}"
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("hits", {}).get("hits") or [])[:limit]:
        meta = item.get("metadata", {})
        doi = meta.get("doi", "")
        results.append(
            _result(
                title=meta.get("title", ""),
                url=f"https://doi.org/{doi}" if doi else item.get("links", {}).get("html", ""),
                source="zenodo",
                # Always the repository, never the record's owner. Zenodo's
                # `owners` is a list of *dicts* (`{"id": "1342605"}`), so `[0]`
                # handed a dict to a parameter `_result` declares as `str`. It did
                # not crash, which is why it survived: `_text` coerces with
                # `str(value)`, so the institution became the literal text
                # "{'id': '1342605'}". That is worse than a crash for the one job
                # this field has — `verify._identity` counts `institution` to
                # decide corroboration, so every Zenodo record with an owner
                # contributed a distinct "institution" whose name is a rendered
                # dict, and two records by different owners looked like two
                # independent institutions agreeing.
                # It was wrong in intent too. An owner id names a *user account*.
                # `institution` answers "who backs this", and for a Zenodo record
                # that is the repository that published it.
                institution="Zenodo / CERN",
                snippet=meta.get("description", "") or "",
                date=meta.get("publication_date", ""),
                rid=doi or str(item.get("id", "")),
            )
        )
    return results


def search_datacite(query: str, limit: int = 5) -> list[dict]:
    """DataCite — DOI registry for research data. No key required."""
    url = (
        "https://api.datacite.org/dois?query=" + urllib.parse.quote(query) + f"&page[size]={limit}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("data") or [])[:limit]:
        attrs = item.get("attributes", {})
        doi = attrs.get("doi", "")
        titles = attrs.get("titles") or [{}]
        creators = attrs.get("creators") or []
        results.append(
            _result(
                title=titles[0].get("title", "") if titles else "",
                url=f"https://doi.org/{doi}" if doi else "",
                source="datacite",
                institution=", ".join(c.get("name", "") for c in creators[:2]),
                snippet=", ".join(
                    d.get("description", "") for d in (attrs.get("descriptions") or [])[:1]
                ),
                date=(attrs.get("publicationYear") or ""),
                rid=doi,
            )
        )
    return results


def search_wikidata(query: str, limit: int = 5) -> list[dict]:
    """Wikidata — structured linked open data (NOT Wikipedia). Citable as structured data source."""
    url = (
        "https://www.wikidata.org/w/api.php?action=wbsearchentities"
        "&search=" + urllib.parse.quote(query) + f"&language=en&format=json&limit={limit}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("search") or [])[:limit]:
        qid = item.get("id", "")
        results.append(
            _result(
                title=item.get("label", ""),
                url=item.get("concepturi", f"https://www.wikidata.org/wiki/{qid}"),
                source="wikidata",
                institution="Wikidata / Wikimedia Foundation",
                snippet=item.get("description", ""),
                date="",
                rid=qid,
            )
        )
    return results


def search_pubchem(query: str, limit: int = 5) -> list[dict]:
    """PubChem — NCBI chemistry database. No key required."""
    search_url = (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
        + urllib.parse.quote(query)
        + "/JSON"
    )
    data = _get(search_url)
    if not data:
        return []
    results = []
    for compound in (data.get("PC_Compounds") or [])[:limit]:
        cid = compound.get("id", {}).get("id", {}).get("cid", "")
        props = {
            p.get("urn", {}).get("label", ""): p.get("value", {})
            for p in (compound.get("props") or [])
        }
        iupac = props.get("IUPAC Name", {}).get("sval", "")
        formula = props.get("Molecular Formula", {}).get("sval", "")
        results.append(
            _result(
                title=iupac or query,
                url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                source="pubchem",
                institution="PubChem / NCBI",
                snippet=f"Formula: {formula}" if formula else "",
                date="",
                rid=str(cid),
            )
        )
    return results


def search_usgs(query: str, limit: int = 5) -> list[dict]:
    """USGS Publications Warehouse — geology, hydrology, earth science. No key required."""
    url = (
        "https://pubs.er.usgs.gov/pubs-services/publication?q="
        + urllib.parse.quote(query)
        + f"&pageSize={limit}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("records") or [])[:limit]:
        doi = item.get("doi", "")
        results.append(
            _result(
                title=item.get("title", ""),
                url=f"https://doi.org/{doi}" if doi else item.get("links", [{}])[0].get("url", ""),
                source="usgs",
                institution="U.S. Geological Survey",
                snippet=item.get("docAbstract", "") or "",
                date=str(item.get("publicationYear", "")),
                rid=doi or str(item.get("id", "")),
            )
        )
    return results


def search_nasa(query: str, limit: int = 5) -> list[dict]:
    """NASA Image & Video Library. No key required."""
    url = (
        "https://images-api.nasa.gov/search?q=" + urllib.parse.quote(query) + f"&page_size={limit}"
    )
    data = _get(url)
    if not data:
        return []
    items = (data.get("collection") or {}).get("items") or []
    results = []
    for item in items[:limit]:
        data_block = (item.get("data") or [{}])[0]
        links = item.get("links") or [{}]
        results.append(
            _result(
                title=data_block.get("title", ""),
                url=links[0].get("href", "") if links else "",
                source="nasa",
                institution="NASA",
                snippet=data_block.get("description", ""),
                date=data_block.get("date_created", "")[:10],
                rid=data_block.get("nasa_id", ""),
            )
        )
    return results


# ── MUSEUMS ───────────────────────────────────────────────────────────────────


def search_met(query: str, limit: int = 5) -> list[dict]:
    """Metropolitan Museum of Art — open access collection. No key required."""
    search_url = (
        "https://collectionapi.metmuseum.org/public/collection/v1/search?q="
        + urllib.parse.quote(query)
        + "&hasImages=true"
    )
    data = _get(search_url)
    if not data:
        return []
    object_ids = (data.get("objectIDs") or [])[:limit]
    results = []
    for oid in object_ids:
        obj = _get(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}")
        if not obj:
            continue
        results.append(
            _result(
                title=obj.get("title", ""),
                url=obj.get("objectURL", ""),
                source="met",
                institution="Metropolitan Museum of Art",
                snippet=f"{obj.get('artistDisplayName', '')} — {obj.get('objectDate', '')} — {obj.get('medium', '')}".strip(
                    " —"
                ),
                date=obj.get("objectDate", ""),
                rid=str(oid),
            )
        )
    return results


def search_cleveland(query: str, limit: int = 5) -> list[dict]:
    """Cleveland Museum of Art — open access. No key required."""
    url = (
        "https://openaccess-api.clevelandart.org/api/artworks/?q="
        + urllib.parse.quote(query)
        + f"&limit={limit}&has_image=1"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("data") or [])[:limit]:
        results.append(
            _result(
                title=item.get("title", ""),
                url=item.get("url", ""),
                source="cleveland",
                institution="Cleveland Museum of Art",
                snippet=f"{', '.join(c.get('description', '') for c in (item.get('creators') or [])[:2])} — {item.get('creation_date', '')}".strip(
                    " —"
                ),
                date=item.get("creation_date", ""),
                rid=str(item.get("id", "")),
            )
        )
    return results


def search_vam(query: str, limit: int = 5) -> list[dict]:
    """Victoria & Albert Museum — decorative arts & design. No key required."""
    url = (
        "https://api.vam.ac.uk/v2/objects/search?q="
        + urllib.parse.quote(query)
        + f"&page_size={limit}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("records") or [])[:limit]:
        sys_num = item.get("systemNumber", "")
        results.append(
            _result(
                title=item.get("_primaryTitle", ""),
                url=f"https://collections.vam.ac.uk/item/{sys_num}/",
                source="vam",
                institution="Victoria & Albert Museum",
                snippet=f"{item.get('_primaryMaker', {}).get('name', '')} — {item.get('_primaryDate', '')}".strip(
                    " —"
                ),
                date=item.get("_primaryDate", ""),
                rid=sys_num,
            )
        )
    return results


def search_rijksmuseum(query: str, limit: int = 5) -> list[dict]:
    """Rijksmuseum — Dutch art and history. Requires RIJKSMUSEUM_API_KEY env var."""
    key = os.environ.get("RIJKSMUSEUM_API_KEY", "")
    if not key:
        log.debug("Rijksmuseum: no API key — skipping")
        return []
    url = (
        "https://www.rijksmuseum.nl/api/en/collection?q="
        + urllib.parse.quote(query)
        + f"&ps={limit}&key={key}&imgonly=True"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("artObjects") or [])[:limit]:
        results.append(
            _result(
                title=item.get("title", ""),
                url=item.get("links", {}).get("web", ""),
                source="rijksmuseum",
                institution="Rijksmuseum",
                snippet=item.get("longTitle", ""),
                date=str(item.get("dating", {}).get("sortingDate", "")),
                rid=item.get("objectNumber", ""),
            )
        )
    return results


# ── INTERNATIONAL ─────────────────────────────────────────────────────────────


def search_gallica(query: str, limit: int = 5) -> list[dict]:
    """Gallica (BnF) — Bibliothèque nationale de France digital collections. No key required."""
    url = (
        "https://gallica.bnf.fr/SRU?operation=searchRetrieve"
        "&query=dc.subject+all+"
        + urllib.parse.quote(f'"{query}"')
        + f"&maximumRecords={limit}&version=1.2"
    )
    try:
        raw = _fetch(url)
    except Exception as e:
        log.warning("Gallica failed: %s", e)
        return []
    ns_dc = "http://purl.org/dc/elements/1.1/"
    ns_srw = "http://www.loc.gov/zing/srw/"
    try:
        root = _parse_xml(raw)
    except ET.ParseError:
        return []
    results = []
    for record in root.findall(f".//{{{ns_srw}}}recordData"):
        titles = record.findall(f"{{{ns_dc}}}title")
        identifiers = record.findall(f"{{{ns_dc}}}identifier")
        dates = record.findall(f"{{{ns_dc}}}date")
        descriptions = record.findall(f"{{{ns_dc}}}description")
        url_val = next((i.text for i in identifiers if i.text and i.text.startswith("http")), "")
        results.append(
            _result(
                title=titles[0].text if titles else "",
                url=url_val,
                source="gallica",
                institution="Gallica / Bibliothèque nationale de France",
                snippet=descriptions[0].text if descriptions else "",
                date=dates[0].text if dates else "",
                rid=url_val.split("/")[-1] if url_val else "",
            )
        )
    return results[:limit]


def search_hal(query: str, limit: int = 5) -> list[dict]:
    """HAL — French open access scientific archive. No key required."""
    url = (
        "https://api.archives-ouvertes.fr/search/?q="
        + urllib.parse.quote(query)
        + f"&rows={limit}&fl=title_s,uri_s,authFullName_s,producedDate_tdate,journalTitle_s&wt=json"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in ((data.get("response") or {}).get("docs") or [])[:limit]:
        titles = item.get("title_s") or [""]
        results.append(
            _result(
                title=titles[0] if titles else "",
                url=item.get("uri_s", ""),
                source="hal",
                institution=item.get("journalTitle_s", "HAL / archives-ouvertes.fr"),
                snippet=", ".join(item.get("authFullName_s") or []),
                date=(item.get("producedDate_tdate") or "")[:10],
                rid=item.get("uri_s", "").split("/")[-1],
            )
        )
    return results


def search_scielo(query: str, limit: int = 5) -> list[dict]:
    """SciELO — Latin American, Iberian & South African science. OAI-PMH, no key required."""
    # Use ArticleMeta search (JSON) for keyword search
    url = "https://articlemeta.scielo.org/api/v1/article/identifiers/?collection=scl&limit=" + str(
        limit
    )
    data = _get(url)
    if not data:
        return []
    pids = [obj.get("code") for obj in (data.get("objects") or []) if obj.get("code")][:limit]
    results = []
    for pid in pids[:limit]:
        art = _get(f"https://articlemeta.scielo.org/api/v1/article/?code={pid}&collection=scl")
        if not art:
            continue
        # SciELO uses internal ISIS field codes; v977=title, v10=authors, v30=journal
        title = ""
        for section in art.get("article", {}).get("v977") or []:
            title = section.get("_", "")
            if title:
                break
        results.append(
            _result(
                title=title,
                url=f"https://www.scielo.br/j/{pid.split('S')[1][:4].lower()}/a/{pid}/",
                source="scielo",
                institution="SciELO / FAPESP",
                snippet="",
                date="",
                rid=pid,
            )
        )
    return [r for r in results if r["title"]]


def search_ndl(query: str, limit: int = 5) -> list[dict]:
    """National Diet Library (Japan) — largest library in Japan. SRU, no key required."""
    url = (
        "https://iss.ndl.go.jp/api/sru?operation=searchRetrieve"
        "&query=title%3D"
        + urllib.parse.quote(f'"{query}"')
        + f"&maximumRecords={limit}&recordSchema=dcndl"
    )
    try:
        raw = _fetch(url)
    except Exception as e:
        log.warning("NDL failed: %s", e)
        return []
    try:
        root = _parse_xml(raw)
    except ET.ParseError:
        return []
    ns_dc = "http://purl.org/dc/elements/1.1/"
    ns_dcterms = "http://purl.org/dc/terms/"
    results = []
    for record in root.findall(".//{http://www.loc.gov/zing/srw/}recordData"):
        titles = record.findall(f".//{{{ns_dcterms}}}title") or record.findall(
            f".//{{{ns_dc}}}title"
        )
        dates = record.findall(f".//{{{ns_dcterms}}}issued") or record.findall(
            f".//{{{ns_dc}}}date"
        )
        publishers = record.findall(f".//{{{ns_dcterms}}}publisher") or record.findall(
            f".//{{{ns_dc}}}publisher"
        )
        ids = record.findall(f".//{{{ns_dc}}}identifier")
        url_val = next((i.text for i in ids if i.text and i.text.startswith("http")), "")
        title_text = titles[0].text if titles else ""
        if not title_text:
            continue
        results.append(
            _result(
                title=title_text,
                url=url_val,
                source="ndl",
                institution="National Diet Library / Japan",
                snippet=publishers[0].text if publishers else "",
                date=dates[0].text if dates else "",
                rid=url_val.split("/")[-1] if url_val else "",
            )
        )
    return results[:limit]


# ── LIBRARIES & ARCHIVES ──────────────────────────────────────────────────────


def search_loc(query: str, limit: int = 5) -> list[dict]:
    """Library of Congress digital collections. No key required."""
    url = "https://www.loc.gov/search/?q=" + urllib.parse.quote(query) + f"&fo=json&c={limit}"
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        desc = item.get("description", "")
        results.append(
            _result(
                title=item.get("title", ""),
                url=item.get("url", ""),
                source="loc",
                institution="Library of Congress",
                snippet=desc[0] if isinstance(desc, list) else desc,
                date=item.get("date", ""),
                rid=item.get("id", ""),
            )
        )
    return results


def search_openlibrary(query: str, limit: int = 5) -> list[dict]:
    """Open Library — books and historical texts. No key required."""
    url = (
        "https://openlibrary.org/search.json?q="
        + urllib.parse.quote(query)
        + f"&limit={limit}&fields=title,author_name,first_publish_year,key,edition_count"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for doc in (data.get("docs") or [])[:limit]:
        key = doc.get("key", "")
        results.append(
            _result(
                title=doc.get("title", ""),
                url=f"https://openlibrary.org{key}" if key else "",
                source="openlibrary",
                institution="Open Library / Internet Archive",
                snippet=", ".join(doc.get("author_name") or []),
                date=str(doc.get("first_publish_year", "")),
                rid=key,
            )
        )
    return results


def search_chronicling_america(query: str, limit: int = 5) -> list[dict]:
    """Chronicling America — historic US newspapers 1770-1963. No key required.

    The legacy `chroniclingamerica.loc.gov` JSON API was retired in 2026 and
    308-redirects to a 404, so this source returned nothing at all until the
    move to the loc.gov collections API. The response shape changed with it:
    `results` rather than `items`, an absolute `url`, and a `description` that
    can be a list — which is why `_text()` above is not optional here. 25s
    because the endpoint is slow enough to trip the 15s default under fan-out
    load. Ported from willow-2.0 (#649).
    """
    url = (
        "https://www.loc.gov/collections/chronicling-america/?q="
        + urllib.parse.quote(query)
        + f"&fo=json&c={limit}"
    )
    data = _get(url, timeout=25)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        results.append(
            _result(
                title=item.get("title", ""),
                url=item.get("url") or item.get("id") or "",
                source="chronicling_america",
                institution="Chronicling America (Library of Congress)",
                snippet=item.get("description", "") or "",
                date=item.get("date", ""),
                rid=item.get("id", ""),
            )
        )
    return results


def search_dpla(query: str, limit: int = 5) -> list[dict]:
    """DPLA — aggregates US libraries, archives, museums. Requires DPLA_API_KEY env var (free, instant)."""
    key = os.environ.get("DPLA_API_KEY", "")
    if not key:
        log.debug("DPLA: no API key — skipping")
        return []
    url = (
        "https://api.dp.la/v2/items?q="
        + urllib.parse.quote(query)
        + f"&page_size={limit}&api_key={key}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("docs") or [])[:limit]:
        src = item.get("sourceResource", {})
        results.append(
            _result(
                title=(src.get("title") or [""])[0]
                if isinstance(src.get("title"), list)
                else src.get("title", ""),
                url=item.get("isShownAt", ""),
                source="dpla",
                institution=item.get("dataProvider", ""),
                snippet=(src.get("description") or [""])[0]
                if isinstance(src.get("description"), list)
                else src.get("description", ""),
                date=(src.get("date") or {}).get("displayDate", "")
                if isinstance(src.get("date"), dict)
                else "",
                rid=item.get("id", ""),
            )
        )
    return results


def search_internet_archive(query: str, limit: int = 5) -> list[dict]:
    """Internet Archive — books, films, audio, web. No key required."""
    url = (
        "https://archive.org/advancedsearch.php?q="
        + urllib.parse.quote(query)
        + f"&fl[]=identifier,title,description,date,creator&rows={limit}&output=json"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for doc in ((data.get("response") or {}).get("docs") or [])[:limit]:
        identifier = doc.get("identifier", "")
        results.append(
            _result(
                title=doc.get("title", ""),
                url=f"https://archive.org/details/{identifier}" if identifier else "",
                source="internet_archive",
                institution="Internet Archive",
                snippet=doc.get("description", "") or "",
                date=str(doc.get("date", "")),
                rid=identifier,
            )
        )
    return results


# ── HERITAGE ──────────────────────────────────────────────────────────────────


def search_smithsonian(query: str, limit: int = 5) -> list[dict]:
    """Smithsonian Open Access. Requires SMITHSONIAN_API_KEY env var."""
    key = os.environ.get("SMITHSONIAN_API_KEY", "")
    if not key:
        log.debug("Smithsonian: no API key — skipping")
        return []
    url = (
        "https://api.si.edu/openaccess/api/v1.0/search?q="
        + urllib.parse.quote(query)
        + f"&rows={limit}&api_key={key}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for row in ((data.get("response") or {}).get("rows") or [])[:limit]:
        desc = row.get("content", {}).get("descriptiveNonRepeating", {})
        results.append(
            _result(
                title=row.get("title", ""),
                url=desc.get("record_link", ""),
                source="smithsonian",
                institution="Smithsonian Institution",
                snippet="",
                date=row.get("content", {}).get("indexedStructured", {}).get("date", [""])[0],
                rid=row.get("id", ""),
            )
        )
    return results


def search_wikipedia(query: str, limit: int = 3) -> list[dict]:
    """Wikipedia REST API — quick entity lookups. General reference only; not for academic citation."""
    import urllib.parse as _up

    encoded = _up.quote(query, safe="")
    data = _get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}")
    if data and data.get("type") not in ("disambiguation", "no-extract", None):
        return [
            _result(
                title=data.get("title", ""),
                url=(data.get("content_urls") or {}).get("desktop", {}).get("page", ""),
                source="wikipedia",
                institution="Wikimedia Foundation",
                snippet=data.get("extract", "")[:400],
                rid=data.get("pageid", ""),
            )
        ]
    # Fallback: search API
    search_data = _get(
        f"https://en.wikipedia.org/w/api.php?action=query&list=search"
        f"&srsearch={encoded}&format=json&srlimit={limit}"
    )
    if not search_data:
        return []
    items = (search_data.get("query") or {}).get("search", [])
    results = []
    for item in items[:limit]:
        title = item.get("title", "")
        snippet = (
            item.get("snippet", "").replace('<span class="searchmatch">', "").replace("</span>", "")
        )
        enc_title = _up.quote(title, safe="")
        results.append(
            _result(
                title=title,
                url=f"https://en.wikipedia.org/wiki/{enc_title}",
                source="wikipedia",
                institution="Wikimedia Foundation",
                snippet=snippet,
                rid=str(item.get("pageid", "")),
            )
        )
    return results


def search_sep(query: str, limit: int = 5) -> list[dict]:
    """Stanford Encyclopedia of Philosophy — peer-reviewed philosophical entries. No key required."""
    url = "https://plato.stanford.edu/search/searcher.py?query=" + urllib.parse.quote(query)
    # Bypassed `_get_html` only to set the User-Agent it already sets.
    html = _get_html(url)
    if not html:
        return []
    results = []
    import re as _re

    seen: set[str] = set()
    # SEP search HTML (2024+): entry=/entries/slug/ in redirect URLs, title often in <b>.
    for m in _re.finditer(
        r'entry=(/entries/[^/&"]+/)[^"]*"[^>]*>(?:<b>)?([^<\n]{3,120})',
        html,
    ):
        path, title = m.group(1), _re.sub(r"\s+", " ", m.group(2)).strip()
        if not title or title.lower().startswith("stanford"):
            continue
        slug = path.strip("/").split("/")[-1]
        if slug in seen:
            continue
        seen.add(slug)
        results.append(
            _result(
                title=title,
                url=f"https://plato.stanford.edu{path}",
                source="sep",
                institution="Stanford Encyclopedia of Philosophy",
                snippet="",
                date="",
                rid=slug,
            )
        )
        if len(results) >= limit:
            break
    return results


def search_gutenberg(query: str, limit: int = 5) -> list[dict]:
    """Project Gutenberg — public domain books via Gutendex API. No key required."""
    url = "https://gutendex.com/books/?search=" + urllib.parse.quote(query) + f"&page_size={limit}"
    data = _get(url)
    if not data:
        return []
    results = []
    for book in (data.get("results") or [])[:limit]:
        title = book.get("title", "")
        authors = ", ".join(a.get("name", "") for a in (book.get("authors") or []))
        bid = book.get("id", "")
        subjects = "; ".join((book.get("subjects") or [])[:3])
        results.append(
            _result(
                title=title,
                url=f"https://www.gutenberg.org/ebooks/{bid}" if bid else "",
                source="gutenberg",
                institution="Project Gutenberg",
                snippet=f"{authors} — {subjects}".strip(" —") if (authors or subjects) else "",
                date=str(book.get("copyright") or ""),
                rid=str(bid),
            )
        )
    return results


def search_bhl(query: str, limit: int = 5) -> list[dict]:
    """Biodiversity Heritage Library — natural history, taxonomy, ecology literature. API key required."""
    api_key = os.environ.get("BHL_API_KEY", "")
    if not api_key:
        return []
    url = (
        "https://www.biodiversitylibrary.org/api3?op=PublicationSearch"
        "&searchtype=F&searchterm="
        + urllib.parse.quote(query)
        + f"&page=1&pageSize={limit}&format=json&apikey={api_key}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("Result") or [])[:limit]:
        bid = item.get("BibliographyID") or item.get("TitleID", "")
        results.append(
            _result(
                title=item.get("FullTitle") or item.get("Title", ""),
                url=f"https://www.biodiversitylibrary.org/bibliography/{bid}" if bid else "",
                source="bhl",
                institution="Biodiversity Heritage Library",
                snippet=(item.get("Note") or "")[:200],
                date=str(item.get("PublicationDate") or item.get("Date", "")),
                rid=str(bid),
            )
        )
    return results


def search_courtlistener(query: str, limit: int = 5) -> list[dict]:
    """CourtListener — US federal and state case law. No key required (throttled)."""
    url = (
        "https://www.courtlistener.com/api/rest/v4/search/?q="
        + urllib.parse.quote(query)
        + f"&type=o&order_by=score+desc&format=json&page_size={limit}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        case_name = item.get("caseName") or item.get("case_name", "")
        court = item.get("court_id", "")
        date = (item.get("dateFiled") or "")[:10]
        abs_url = item.get("absolute_url", "")
        snippet = (item.get("snippet") or "")[:200]
        results.append(
            _result(
                title=case_name,
                url=f"https://www.courtlistener.com{abs_url}" if abs_url else "",
                source="courtlistener",
                institution="CourtListener",
                snippet=f"{court} — {snippet}".strip(" —") if (court or snippet) else "",
                date=date,
                rid=str(item.get("id", "")),
            )
        )
    return results


def search_base(query: str, limit: int = 5) -> list[dict]:
    """BASE (Bielefeld Academic Search Engine) — 350M+ open access documents. No key required."""
    url = (
        "https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi"
        "?func=PerformSearch&query=" + urllib.parse.quote(query) + f"&hits={limit}&format=json"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in ((data.get("response") or {}).get("docs") or [])[:limit]:
        title = item.get("dctitle", "") or ""
        if isinstance(title, list):
            title = title[0] if title else ""
        creator = item.get("dccreator", "") or ""
        if isinstance(creator, list):
            creator = "; ".join(creator[:2])
        link = item.get("dclink") or item.get("dcidentifier", "")
        if isinstance(link, list):
            link = link[0] if link else ""
        date = str(item.get("dcyear", "") or "")
        desc = item.get("dcdescription", "") or ""
        if isinstance(desc, list):
            desc = desc[0] if desc else ""
        results.append(
            _result(
                title=title,
                url=link,
                source="base",
                institution="BASE / Bielefeld University",
                snippet=f"{creator} — {str(desc)[:150]}".strip(" —") if (creator or desc) else "",
                date=date,
                rid=item.get("dcidentifier", [""])[0]
                if isinstance(item.get("dcidentifier"), list)
                else item.get("dcidentifier", ""),
            )
        )
    return results


def search_dblp(query: str, limit: int = 5) -> list[dict]:
    """DBLP — computer science bibliography. No key required."""
    url = (
        "https://dblp.org/search/publ/api?q="
        + urllib.parse.quote(query)
        + f"&format=json&h={limit}"
    )
    data = _get(url)
    if not data:
        return []
    hits = (data.get("result") or {}).get("hits") or {}
    results = []
    for item in (hits.get("hit") or [])[:limit]:
        info = item.get("info") or {}
        authors = info.get("authors") or {}
        author_list = authors.get("author", [])
        if isinstance(author_list, dict):
            author_list = [author_list]
        author_str = ", ".join(
            (a.get("text") or a) if isinstance(a, dict) else str(a) for a in author_list[:3]
        )
        results.append(
            _result(
                title=info.get("title", ""),
                url=info.get("url", ""),
                source="dblp",
                institution="DBLP",
                snippet=f"{author_str} — {info.get('venue', '')}".strip(" —")
                if (author_str or info.get("venue"))
                else "",
                date=str(info.get("year", "")),
                rid=item.get("@id", ""),
            )
        )
    return results


def search_openfda(query: str, limit: int = 5) -> list[dict]:
    """OpenFDA — drug labels, adverse events, food/device safety. No key required."""
    url = (
        "https://api.fda.gov/drug/label.json?search="
        + urllib.parse.quote(f'description:"{query}" OR indications_and_usage:"{query}"')
        + f"&limit={limit}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        openfda = item.get("openfda") or {}
        brand = (openfda.get("brand_name") or [""])[0]
        generic = (openfda.get("generic_name") or [""])[0]
        title = brand or generic or "Drug Label"
        manuf = (openfda.get("manufacturer_name") or [""])[0]
        indications = (item.get("indications_and_usage") or [""])[0][:200]
        app_num = (openfda.get("application_number") or [""])[0]
        results.append(
            _result(
                title=title,
                url=f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={app_num.replace('NDA', '').replace('ANDA', '').strip()}"
                if app_num
                else "",
                source="openfda",
                institution="U.S. FDA",
                snippet=f"{manuf} — {indications}".strip(" —") if (manuf or indications) else "",
                date="",
                rid=app_num,
            )
        )
    return results


def search_eol(query: str, limit: int = 5) -> list[dict]:
    """Encyclopedia of Life — species taxonomy and ecology. No key required."""
    url = "https://eol.org/api/search/1.0.json?q=" + urllib.parse.quote(query) + "&page=1"
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        eid = item.get("id", "")
        title = item.get("title", "")
        content = item.get("content", "")
        results.append(
            _result(
                title=title,
                url=f"https://eol.org/pages/{eid}" if eid else "",
                source="eol",
                institution="Encyclopedia of Life",
                snippet=content[:200] if content else "",
                date="",
                rid=str(eid),
            )
        )
    return results


def search_gbif(query: str, limit: int = 5) -> list[dict]:
    """GBIF (Global Biodiversity Information Facility) — occurrence records. No key required."""
    url = (
        "https://api.gbif.org/v1/species/search?q=" + urllib.parse.quote(query) + f"&limit={limit}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        key = item.get("key") or item.get("nubKey", "")
        sci_name = item.get("scientificName", "")
        canonical = item.get("canonicalName", "")
        rank = item.get("rank", "")
        kingdom = item.get("kingdom", "")
        snippet = f"{rank} — Kingdom: {kingdom}".strip(" —") if (rank or kingdom) else ""
        results.append(
            _result(
                title=sci_name or canonical,
                url=f"https://www.gbif.org/species/{key}" if key else "",
                source="gbif",
                institution="GBIF",
                snippet=snippet,
                date="",
                rid=str(key),
            )
        )
    return results


def search_nominatim(query: str, limit: int = 5) -> list[dict]:
    """OpenStreetMap Nominatim — geographic place search. No key, 1 req/sec limit."""
    url = (
        "https://nominatim.openstreetmap.org/search?q="
        + urllib.parse.quote(query)
        + f"&format=json&limit={limit}&addressdetails=1"
    )
    items = _get(url, {"Accept-Language": "en"})
    results = []
    for item in (items or [])[:limit]:
        osm_id = item.get("osm_id", "")
        osm_type = item.get("osm_type", "")
        addr = item.get("address") or {}
        country = addr.get("country", "")
        place_type = item.get("type", "") or item.get("class", "")
        snippet = f"{place_type} — {country}".strip(" —") if (place_type or country) else ""
        results.append(
            _result(
                title=item.get("display_name", ""),
                url=f"https://www.openstreetmap.org/{osm_type}/{osm_id}" if osm_id else "",
                source="nominatim",
                institution="OpenStreetMap",
                snippet=snippet,
                date="",
                rid=str(osm_id),
            )
        )
    return results


def search_openaire(query: str, limit: int = 5) -> list[dict]:
    """OpenAIRE — European open research publications. No key required."""
    url = (
        "https://api.openaire.eu/search/publications?title="
        + urllib.parse.quote(query)
        + f"&format=json&page=1&size={limit}"
    )
    data = _get(url)
    if not data:
        return []
    try:
        results_raw = data.get("response", {}).get("results", {}).get("result") or []
    except Exception:
        return []
    results = []
    for item in (results_raw or [])[:limit]:
        metadata = (item.get("metadata") or {}).get("oaf:entity", {}).get("oaf:result", {})
        if not metadata:
            continue
        title_obj = metadata.get("title") or {}
        title = (
            title_obj.get("$")
            if isinstance(title_obj, dict)
            else (title_obj[0].get("$") if isinstance(title_obj, list) and title_obj else "")
        )
        pid_list = metadata.get("pid") or []
        if isinstance(pid_list, dict):
            pid_list = [pid_list]
        doi = next(
            (p.get("$") for p in pid_list if isinstance(p, dict) and p.get("@classid") == "doi"), ""
        )
        date = (
            (metadata.get("dateofacceptance") or {}).get("$", "")[:10]
            if isinstance(metadata.get("dateofacceptance"), dict)
            else ""
        )
        results.append(
            _result(
                title=title or "",
                url=f"https://doi.org/{doi}" if doi else "",
                source="openaire",
                institution="OpenAIRE",
                snippet="",
                date=date,
                rid=doi,
            )
        )
    return results


def search_inaturalist(query: str, limit: int = 5) -> list[dict]:
    """iNaturalist — citizen science species observations. No key required for search."""
    url = (
        "https://api.inaturalist.org/v1/taxa?q="
        + urllib.parse.quote(query)
        + f"&per_page={limit}&order_by=observations_count"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        taxon_id = item.get("id", "")
        name = item.get("name", "")
        preferred = item.get("preferred_common_name", "")
        rank = item.get("rank", "")
        obs_count = item.get("observations_count", 0)
        title = f"{preferred} ({name})" if preferred else name
        snippet = f"{rank.capitalize()} — {obs_count:,} observations" if rank else ""
        results.append(
            _result(
                title=title,
                url=f"https://www.inaturalist.org/taxa/{taxon_id}" if taxon_id else "",
                source="inaturalist",
                institution="iNaturalist",
                snippet=snippet,
                date="",
                rid=str(taxon_id),
            )
        )
    return results


def search_federal_register(query: str, limit: int = 5) -> list[dict]:
    """Federal Register (US) — federal rulemaking, executive orders, notices. No key required."""
    url = (
        "https://www.federalregister.gov/api/v1/documents.json"
        "?conditions%5Bterm%5D=" + urllib.parse.quote(query) + f"&per_page={limit}&order=relevance"
        "&fields%5B%5D=title&fields%5B%5D=document_number&fields%5B%5D=type"
        "&fields%5B%5D=publication_date&fields%5B%5D=abstract"
        "&fields%5B%5D=html_url&fields%5B%5D=agency_names"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("results") or [])[:limit]:
        agencies = ", ".join((item.get("agency_names") or [])[:2])
        doc_type = item.get("type", "")
        snippet = f"{doc_type} — {agencies} — {(item.get('abstract') or '')[:150]}".strip(" —")
        results.append(
            _result(
                title=item.get("title", ""),
                url=item.get("html_url", ""),
                source="federal_register",
                institution="U.S. Federal Register",
                snippet=snippet,
                date=(item.get("publication_date") or "")[:10],
                rid=item.get("document_number", ""),
            )
        )
    return results


def search_datagov(query: str, limit: int = 5) -> list[dict]:
    """data.gov — US government open datasets (CKAN). No key required."""
    # Use safe query encoding — keep only alnum+spaces, collapse spaces
    safe_q = urllib.parse.quote_plus(" ".join(query.split()[:8]))
    url = f"https://catalog.data.gov/api/3/action/package_search?q={safe_q}&rows={limit}"
    data = _get(url)
    if not data:
        return []
    results = []
    for item in ((data.get("result") or {}).get("results") or [])[:limit]:
        org = (item.get("organization") or {}).get("title", "")
        notes = (item.get("notes") or "")[:200]
        results.append(
            _result(
                title=item.get("title", ""),
                url=f"https://catalog.data.gov/dataset/{item.get('name', '')}",
                source="datagov",
                institution="data.gov (U.S. Government)",
                snippet=f"{org} — {notes}".strip(" —") if (org or notes) else "",
                date=(item.get("metadata_modified") or "")[:10],
                rid=item.get("id", ""),
            )
        )
    return results


def search_uk_legislation(query: str, limit: int = 5) -> list[dict]:
    """legislation.gov.uk — UK Acts of Parliament, statutory instruments. No key required."""
    url = (
        "https://www.legislation.gov.uk/search?title=" + urllib.parse.quote(query) + "&format=json"
    )
    data = _get(url, {"Accept": "application/json"})
    if not data:
        return []
    results = []
    for item in (data.get("items") or [])[:limit]:
        leg_type = item.get("type", {})
        if isinstance(leg_type, dict):
            leg_type = leg_type.get("value", "")
        year = str(item.get("year", ""))
        title = item.get("title", "")
        href = item.get("href", "")
        results.append(
            _result(
                title=title,
                url=f"https://www.legislation.gov.uk{href}"
                if href and not href.startswith("http")
                else href,
                source="uk_legislation",
                institution="legislation.gov.uk",
                snippet=f"{leg_type} {year}".strip(),
                date=year,
                rid=href,
            )
        )
    return results


def search_eu_data(query: str, limit: int = 5) -> list[dict]:
    """data.europa.eu — EU open data portal (CKAN). No key required."""
    url = (
        "https://data.europa.eu/api/hub/search/datasets?query="
        + urllib.parse.quote(query)
        + f"&limit={limit}&facets=false"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("result", {}).get("results", []) or [])[:limit]:
        pub = (item.get("publisher") or {}).get("name", "")
        desc = item.get("description") or {}
        if isinstance(desc, dict):
            desc = desc.get("en", "") or next(iter(desc.values()), "")
        results.append(
            _result(
                title=(item.get("title") or {}).get("en", "") or item.get("title", "")
                if isinstance(item.get("title"), dict)
                else item.get("title", ""),
                url=item.get("landingPage", ""),
                source="eu_data",
                institution="data.europa.eu (EU)",
                snippet=f"{pub} — {str(desc)[:150]}".strip(" —") if (pub or desc) else "",
                date=(item.get("modified") or "")[:10],
                rid=item.get("id", ""),
            )
        )
    return results


def search_musicbrainz(query: str, limit: int = 5) -> list[dict]:
    """MusicBrainz — open music encyclopedia. Artists, recordings, albums. No key required.
    Searches release-groups (albums/singles) first; falls back to recordings for track queries."""
    q_lower = query.lower()
    use_releases = any(
        w in q_lower for w in ["album", "release", "discography", "ep", "lp", "record"]
    )

    if use_releases:
        # Strip the type word to get artist name, then use Lucene artist: syntax
        artist_name = re.sub(
            r"\b(albums?|discography|ep|lp|records?|singles?|releases?)\b",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip()
        # Filter to Albums only when query is album/discography context
        type_filter = (
            " AND primarytype:Album"
            if any(w in q_lower for w in ["album", "discography", "lp"])
            else ""
        )
        mb_query = f'artist:"{artist_name}"{type_filter}' if artist_name else query
        url = (
            "https://musicbrainz.org/ws/2/release-group?query="
            + urllib.parse.quote(mb_query)
            + f"&limit={limit}&fmt=json"
        )
        data = _get(url)
        items = (data or {}).get("release-groups") or []
        results = []
        for item in items[:limit]:
            artist = ", ".join(
                c.get("artist", {}).get("name", "")
                for c in (item.get("artist-credit") or [])
                if isinstance(c, dict)
            )
            mbid = item.get("id", "")
            rtype = item.get("primary-type", "")
            results.append(
                _result(
                    title=item.get("title", ""),
                    url=f"https://musicbrainz.org/release-group/{mbid}" if mbid else "",
                    source="musicbrainz",
                    institution="MusicBrainz",
                    snippet=f"{artist} — {rtype}".strip(" —") if (artist or rtype) else "",
                    date=(item.get("first-release-date") or "")[:10],
                    rid=mbid,
                )
            )
        if results:
            return results

    # Fall through to recording search
    url = (
        "https://musicbrainz.org/ws/2/recording?query="
        + urllib.parse.quote(query)
        + f"&limit={limit}&fmt=json"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("recordings") or [])[:limit]:
        artist = ", ".join(
            c.get("artist", {}).get("name", "")
            for c in (item.get("artist-credit") or [])
            if isinstance(c, dict)
        )
        releases = item.get("releases") or []
        release_title = releases[0].get("title", "") if releases else ""
        date = (releases[0].get("date", "") if releases else "") or item.get(
            "first-release-date", ""
        )
        mbid = item.get("id", "")
        results.append(
            _result(
                title=item.get("title", ""),
                url=f"https://musicbrainz.org/recording/{mbid}" if mbid else "",
                source="musicbrainz",
                institution="MusicBrainz",
                snippet=f"{artist} — {release_title}".strip(" —"),
                date=date[:10] if date else "",
                rid=mbid,
            )
        )
    return results


def search_europeana(query: str, limit: int = 5) -> list[dict]:
    """Europeana — European cultural heritage. Requires EUROPEANA_API_KEY env var."""
    key = os.environ.get("EUROPEANA_API_KEY", "")
    if not key:
        log.debug("Europeana: no API key — skipping")
        return []
    url = (
        "https://api.europeana.eu/record/v2/search.json?wskey="
        + key
        + "&query="
        + urllib.parse.quote(query)
        + f"&rows={limit}&profile=rich"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in (data.get("items") or [])[:limit]:
        results.append(
            _result(
                title=(item.get("title") or [""])[0],
                url=item.get("guid", ""),
                source="europeana",
                institution=(item.get("dataProvider") or ["Europeana"])[0],
                snippet=(item.get("dcDescription") or [""])[0],
                date=(item.get("year") or [""])[0],
                rid=item.get("id", ""),
            )
        )
    return results


def search_psychiatric_times(query: str, limit: int = 5) -> list[dict]:
    """Psychiatric Times — clinical psychiatry news, case reports, and review articles.
    HTML scraper (no API key). Press tier — trade press, not peer-reviewed."""
    import re as _re

    url = "https://www.psychiatrictimes.com/search#q=" + urllib.parse.quote(query) + "&t=All"
    html = _get_html(url)
    if not html:
        return []

    results: list[dict] = []
    # Article links under /view/ path
    link_re = _re.compile(
        r'href="(/view/[a-z0-9-]{5,120})"[^>]*>\s*([^<]{10,200})\s*</a>',
        _re.S,
    )
    date_re = _re.compile(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4})\b"
    )
    seen: set[str] = set()
    for m in link_re.finditer(html):
        path = m.group(1)
        title = _re.sub(r"\s+", " ", m.group(2)).strip()
        if path in seen or not title:
            continue
        seen.add(path)
        nearby = html[max(0, m.start() - 200) : m.start() + 400]
        date_m = date_re.search(nearby)
        date = date_m.group(1) if date_m else ""
        results.append(
            _result(
                title=title,
                url=f"https://www.psychiatrictimes.com{path}",
                source="psychiatric_times",
                institution="Psychiatric Times",
                snippet="",
                date=date,
                rid=path,
            )
        )
        if len(results) >= limit:
            break
    return results


# ── HIGH-ENERGY PHYSICS ───────────────────────────────────────────────────────


def search_inspirehep(query: str, limit: int = 5) -> list[dict]:
    """InspireHEP — CERN/SLAC high-energy physics literature. No key required."""
    url = (
        "https://inspirehep.net/api/literature?q="
        + urllib.parse.quote(query)
        + f"&sort=mostrecent&size={limit}"
        + "&fields=titles,abstracts,dois,arxiv_eprints,publication_info,authors"
    )
    data = _get(url)
    if not data:
        return []
    hits = (data.get("hits") or {}).get("hits") or []
    results = []
    for item in hits[:limit]:
        meta = item.get("metadata") or {}
        title = ((meta.get("titles") or [{}])[0]).get("title", "")
        abstract = ((meta.get("abstracts") or [{}])[0]).get("value", "")
        doi = ((meta.get("dois") or [{}])[0]).get("value", "")
        arxiv = ((meta.get("arxiv_eprints") or [{}])[0]).get("value", "")
        pub = (meta.get("publication_info") or [{}])[0]
        year = str(pub.get("year", ""))
        link = (
            f"https://doi.org/{doi}" if doi else (f"https://arxiv.org/abs/{arxiv}" if arxiv else "")
        )
        results.append(
            _result(
                title=title,
                url=link,
                source="inspirehep",
                institution="INSPIRE-HEP (CERN)",
                snippet=abstract,
                date=year,
                rid=doi or arxiv,
            )
        )
    return results


# ── ECONOMICS ─────────────────────────────────────────────────────────────────


def search_worldbank(query: str, limit: int = 5) -> list[dict]:
    """World Bank Open Data — global development indicators (WDI). No key required.
    The v2 /indicator endpoint has no free-text search (the `q=` param is ignored and
    returns indicators alphabetically), so fetch the WDI indicator list and rank
    client-side by whole-word relevance — title weighted over description."""
    url = "https://api.worldbank.org/v2/indicator?format=json&per_page=2000&source=2"
    data = _get(url)
    if not data or not isinstance(data, list) or len(data) < 2:
        return []
    indicators = data[1] or []
    q_terms = [t for t in query.lower().split() if len(t) > 2]

    def _words(s: str) -> set:
        return set("".join(c if c.isalnum() else " " for c in (s or "").lower()).split())

    if q_terms:
        # Whole-word match (so "product" does not hit "production"), ranked by
        # relevance — alphabetical order otherwise buries the real match under AG.*.
        scored = []
        for item in indicators:
            name_w = _words(item.get("name"))
            note_w = _words(item.get("sourceNote"))
            score = sum((3 if t in name_w else 0) + (1 if t in note_w else 0) for t in q_terms)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: -x[0])
        matches = [item for _, item in scored[:limit]]
    else:
        matches = indicators[:limit]
    results = []
    for item in matches:
        iid = item.get("id", "")
        name = item.get("name", "")
        source = (item.get("sourceNote") or "")[:300]
        topic = ", ".join(t.get("value", "") for t in (item.get("topics") or []) if t.get("value"))
        results.append(
            _result(
                title=name,
                url=f"https://data.worldbank.org/indicator/{iid}"
                if iid
                else "https://data.worldbank.org",
                source="worldbank",
                institution="World Bank Open Data",
                snippet=source or topic,
                date="",
                rid=iid,
            )
        )
    return results


# ── FOOD & NUTRITION ──────────────────────────────────────────────────────────


def search_openfoodfacts(query: str, limit: int = 5) -> list[dict]:
    """Open Food Facts — global food product & nutrient database. No key required."""
    url = (
        "https://world.openfoodfacts.org/cgi/search.pl?search_terms="
        + urllib.parse.quote(query)
        + f"&search_simple=1&action=process&json=1&page_size={limit}"
    )
    data = _get(url)
    if not data:
        return []
    products = (data.get("products") or [])[:limit]
    results = []
    for p in products:
        name = p.get("product_name", "").strip()
        if not name:
            continue
        categories = (p.get("categories") or "").split(",")[0].strip()
        brand = p.get("brands", "").split(",")[0].strip()
        quantity = p.get("quantity", "")
        nut = p.get("nutriments") or {}
        kcal = nut.get("energy-kcal_100g", "")
        snippet_parts = [x for x in [brand, categories, (f"{kcal} kcal/100g" if kcal else "")] if x]
        pid = p.get("_id") or p.get("id", "")
        results.append(
            _result(
                title=f"{name}{' (' + quantity + ')' if quantity else ''}",
                url=p.get("url")
                or (
                    f"https://world.openfoodfacts.org/product/{pid}"
                    if pid
                    else "https://world.openfoodfacts.org"
                ),
                source="openfoodfacts",
                institution="Open Food Facts",
                snippet=", ".join(snippet_parts),
                date="",
                rid=pid,
            )
        )
    return results


# ── ENVIRONMENT ───────────────────────────────────────────────────────────────


def search_carbon_intensity(query: str, limit: int = 5) -> list[dict]:
    """UK Carbon Intensity API — official National Grid ESO data. No key required.
    Returns current carbon intensity and generation fuel mix regardless of query."""
    intensity_data = _get("https://api.carbonintensity.org.uk/intensity")
    generation_data = _get("https://api.carbonintensity.org.uk/generation")
    results = []
    if intensity_data:
        entry = (intensity_data.get("data") or [{}])[0]
        intensity = entry.get("intensity") or {}
        actual = intensity.get("actual")
        forecast = intensity.get("forecast")
        index = intensity.get("index", "")
        from_ts = entry.get("from", "")[:10]
        snippet = (
            f"Actual: {actual} gCO₂/kWh · Forecast: {forecast} gCO₂/kWh · Index: {index}"
        ).strip(" ·")
        results.append(
            _result(
                title="UK Grid Carbon Intensity — current",
                url="https://carbonintensity.org.uk",
                source="carbon_intensity",
                institution="National Grid ESO (UK Government)",
                snippet=snippet,
                date=from_ts,
                rid="intensity-current",
            )
        )
    if generation_data:
        gen_entry = generation_data.get("data") or {}
        gen_mix = gen_entry.get("generationmix") or []
        fuels = ", ".join(f"{g['fuel']} {g['perc']:.1f}%" for g in gen_mix if g.get("perc", 0) > 1)
        from_ts = gen_entry.get("from", "")[:10]
        results.append(
            _result(
                title="UK Grid Generation Mix — current",
                url="https://carbonintensity.org.uk",
                source="carbon_intensity",
                institution="National Grid ESO (UK Government)",
                snippet=fuels,
                date=from_ts,
                rid="generation-current",
            )
        )
    return results[:limit]


# ── WEATHER ───────────────────────────────────────────────────────────────────


def search_nws(query: str, limit: int = 5) -> list[dict]:
    """US National Weather Service — active alerts and official forecasts. No key required."""
    url = "https://api.weather.gov/alerts/active?status=actual&message_type=alert"
    data = _get(url)
    if not data:
        return []
    features = data.get("features") or []
    query_lower = query.lower()
    query_words = [w for w in query_lower.split() if len(w) > 3]
    scored = []
    for f in features:
        props = f.get("properties") or {}
        event = props.get("event", "") or ""
        headline = props.get("headline", "") or ""
        area = props.get("areaDesc", "") or ""
        combined = f"{event} {headline} {area}".lower()
        score = sum(1 for w in query_words if w in combined)
        scored.append((score, f))
    scored.sort(key=lambda x: -x[0])
    results = []
    for _, f in scored[:limit]:
        props = f.get("properties") or {}
        event = props.get("event", "")
        headline = props.get("headline", "")
        area = props.get("areaDesc", "")
        effective = (props.get("effective") or "")[:10]
        fid = props.get("id", "")
        url_link = "https://www.weather.gov"
        results.append(
            _result(
                title=f"{event} — {area}" if area else event,
                url=url_link,
                source="nws",
                institution="U.S. National Weather Service (NOAA)",
                snippet=headline,
                date=effective,
                rid=fid,
            )
        )
    return results


# ── NEWS ──────────────────────────────────────────────────────────────────────


def search_gdelt(query: str, limit: int = 5) -> list[dict]:
    """GDELT — global news event stream, 100+ languages indexed. No key required."""
    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc?query="
        + urllib.parse.quote(query)
        + f"&mode=artlist&maxrecords={limit}&format=json&sourcelang=english"
    )
    data = _get(url)
    if not data:
        return []
    articles = (data.get("articles") or [])[:limit]
    results = []
    for a in articles:
        title = (a.get("title") or "").strip()
        url_ = a.get("url", "")
        domain = a.get("domain", "")
        raw_date = (a.get("seendate") or "")[:8]
        date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else ""
        results.append(
            _result(
                title=title or domain,
                url=url_,
                source="gdelt",
                institution=f"GDELT ({domain})",
                snippet=domain,
                date=date,
                rid=url_,
            )
        )
    return results


# ── PUBLIC HEALTH ──────────────────────────────────────────────────────────────


def search_who_gho(query: str, limit: int = 5) -> list[dict]:
    """WHO Global Health Observatory — official global health statistics. No key required."""
    url = (
        "https://ghoapi.azureedge.net/api/Indicator?$filter=contains(IndicatorName,"
        + f"'{urllib.parse.quote(query)}')"
        + f"&$top={limit}"
    )
    data = _get(url)
    if not data:
        return []
    indicators = (data.get("value") or [])[:limit]
    results = []
    for ind in indicators:
        code = ind.get("IndicatorCode", "")
        name = ind.get("IndicatorName", "")
        results.append(
            _result(
                title=name,
                url=f"https://www.who.int/data/gho/data/indicators/indicator-details/GHO/{code}",
                source="who_gho",
                institution="World Health Organization (WHO)",
                snippet=code,
                date="",
                rid=code,
            )
        )
    return results


# ── CLIMATE / WEATHER (global) ─────────────────────────────────────────────────


def search_open_meteo(query: str, limit: int = 5) -> list[dict]:
    """Open-Meteo — global weather and climate data via ECMWF. No key required.
    Geocodes the location in the query then returns current conditions + 3-day forecast."""
    _WEATHER_STOP = {
        "weather",
        "forecast",
        "climate",
        "temperature",
        "rain",
        "snow",
        "wind",
        "humidity",
        "today",
        "now",
        "current",
    }
    geo_term = " ".join(w for w in query.split() if w.lower() not in _WEATHER_STOP) or query
    geo_url = (
        "https://geocoding-api.open-meteo.com/v1/search?name="
        + urllib.parse.quote(geo_term)
        + "&count=1&language=en&format=json"
    )
    geo = _get(geo_url)
    if not geo or not geo.get("results"):
        return []
    loc = geo["results"][0]
    lat, lon = loc["latitude"], loc["longitude"]
    name = loc.get("name", query)
    country = loc.get("country", "")
    tz = loc.get("timezone", "UTC")
    wx_url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        f"&timezone={urllib.parse.quote(tz)}&forecast_days=3"
    )
    wx = _get(wx_url)
    if not wx:
        return []
    current = wx.get("current") or {}
    daily = wx.get("daily") or {}
    temp = current.get("temperature_2m", "")
    humid = current.get("relative_humidity_2m", "")
    wind = current.get("wind_speed_10m", "")
    # strict: these three arrays are parallel by contract. A bare zip() would
    # truncate to the shortest and hand back a short forecast that reads as a
    # complete one; ragged here means the response is malformed, and the source
    # should land in `failed` rather than quietly answer with less.
    days = list(
        zip(
            (daily.get("time") or [])[:3],
            (daily.get("temperature_2m_max") or [])[:3],
            (daily.get("temperature_2m_min") or [])[:3],
            strict=True,
        )
    )
    forecast = " | ".join(f"{d}: {hi}/{lo}°C" for d, hi, lo in days)
    snippet = f"Current: {temp}°C, humidity {humid}%, wind {wind} km/h | Forecast: {forecast}"
    return [
        _result(
            title=f"{name}, {country} — weather",
            url="https://open-meteo.com",
            source="open_meteo",
            institution="Open-Meteo (ECMWF data)",
            snippet=snippet,
            date=(current.get("time") or "")[:10],
            rid=f"{lat},{lon}",
        )
    ]


# ── PATENTS ───────────────────────────────────────────────────────────────────


def search_patentsview(query: str, limit: int = 5) -> list[dict]:
    """USPTO PatentsView — full US patent database. No key required."""
    import json as _json

    q = _json.dumps({"_text_any": {"patent_title": query, "patent_abstract": query}})
    f = _json.dumps(["patent_id", "patent_title", "patent_abstract", "patent_date"])
    o = _json.dumps({"per_page": limit, "sort": {"patent_date": "desc"}})
    url = (
        "https://search.patentsview.org/api/v1/patent?q="
        + urllib.parse.quote(q)
        + "&f="
        + urllib.parse.quote(f)
        + "&o="
        + urllib.parse.quote(o)
    )
    data = _get(url)
    if not data:
        return []
    patents = (data.get("patents") or [])[:limit]
    results = []
    for p in patents:
        pid = p.get("patent_id", "")
        title = p.get("patent_title", "")
        abstract = (p.get("patent_abstract") or "")[:300]
        date = (p.get("patent_date") or "")[:10]
        results.append(
            _result(
                title=title,
                url=f"https://patents.google.com/patent/US{pid}",
                source="patentsview",
                institution="USPTO PatentsView",
                snippet=abstract,
                date=date,
                rid=pid,
            )
        )
    return results


# ── MACROECONOMICS ────────────────────────────────────────────────────────────


def search_imf(query: str, limit: int = 5) -> list[dict]:
    """IMF DataMapper — 132 global macroeconomic indicators. No key required."""
    data = _get("https://www.imf.org/external/datamapper/api/v1/indicators")
    if not data:
        return []
    indicators = data.get("indicators") or {}
    q_terms = [t for t in query.lower().split() if len(t) > 2]

    def _matches(v: dict) -> bool:
        text = ((v.get("label") or "") + " " + (v.get("description") or "")).lower()
        return any(t in text for t in q_terms)

    matches = [(k, v) for k, v in indicators.items() if _matches(v)][:limit]
    results = []
    for code, meta in matches:
        label = meta.get("label", code)
        desc = (meta.get("description") or "")[:280]
        unit = meta.get("unit", "")
        results.append(
            _result(
                title=f"{label} ({unit})" if unit else label,
                url=f"https://www.imf.org/external/datamapper/{code}",
                source="imf",
                institution="International Monetary Fund (IMF)",
                snippet=desc,
                date="",
                rid=code,
            )
        )
    return results


# ── SOCIAL SCIENCE PREPRINTS ───────────────────────────────────────────────────


def search_osf(query: str, limit: int = 5) -> list[dict]:
    """OSF Preprints — open social science, psychology, medicine preprints. No key required."""
    url = (
        "https://api.osf.io/v2/preprints/?filter[title]="
        + urllib.parse.quote(query)
        + f"&page[size]={limit}&filter[is_published]=true"
    )
    data = _get(url, headers={"Accept": "application/vnd.api+json"})
    if not data:
        return []
    items = (data.get("data") or [])[:limit]
    results = []
    for item in items:
        attrs = item.get("attributes") or {}
        links = item.get("links") or {}
        title = attrs.get("title", "").strip()
        desc = (attrs.get("description") or "")[:280]
        doi = attrs.get("doi") or ""
        date = (attrs.get("date_published") or attrs.get("date_created") or "")[:10]
        url_ = links.get("html") or links.get("iri") or ""
        results.append(
            _result(
                title=title,
                url=url_,
                source="osf",
                institution="Open Science Framework (OSF)",
                snippet=desc or doi,
                date=date,
                rid=item.get("id", ""),
            )
        )
    return results


# ── SPORTS ───────────────────────────────────────────────────────────────────


def search_thesportsdb(query: str, limit: int = 5) -> list[dict]:
    """TheSportsDB — teams, players, leagues, and events. No key required (demo tier)."""
    results = []
    for kind, _key, endpoint in [
        (
            "teams",
            "teams",
            f"https://www.thesportsdb.com/api/v1/json/3/searchteams.php?t={urllib.parse.quote(query)}",
        ),
        (
            "players",
            "players",
            f"https://www.thesportsdb.com/api/v1/json/3/searchplayers.php?p={urllib.parse.quote(query)}",
        ),
    ]:
        if len(results) >= limit:
            break
        data = _get(endpoint)
        if not data or not data.get(kind):
            continue
        for item in (data[kind] or [])[: limit - len(results)]:
            if kind == "teams":
                name = item.get("strTeam", "")
                sport = item.get("strSport", "")
                league = item.get("strLeague", "")
                desc = (item.get("strDescriptionEN") or "")[:280]
                url_ = f"https://www.thesportsdb.com/team/{item.get('idTeam', '')}"
                snippet = f"{sport} · {league}" if sport else league
            else:
                name = item.get("strPlayer", "")
                sport = item.get("strSport", "")
                nation = item.get("strNationality", "")
                desc = (item.get("strDescriptionEN") or "")[:280]
                url_ = f"https://www.thesportsdb.com/player/{item.get('idPlayer', '')}"
                snippet = f"{sport} · {nation}" if sport else nation
            results.append(
                _result(
                    title=name,
                    url=url_,
                    source="thesportsdb",
                    institution="TheSportsDB",
                    snippet=snippet + (f" — {desc[:200]}" if desc else ""),
                    date="",
                    rid=item.get("idTeam") or item.get("idPlayer", ""),
                )
            )
    return results[:limit]


# ── FINANCE / FX ─────────────────────────────────────────────────────────────


def search_frankfurter(query: str, limit: int = 5) -> list[dict]:
    """Frankfurter — ECB official exchange rates. No key required."""
    # Determine base currency from query; default EUR
    q_upper = query.upper()
    known = {
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "CHF",
        "AUD",
        "CAD",
        "CNY",
        "SEK",
        "NOK",
        "DKK",
        "NZD",
        "SGD",
        "HKD",
        "KRW",
        "INR",
        "BRL",
        "MXN",
        "ZAR",
        "TRY",
    }
    base = next((tok for tok in q_upper.split() if tok in known), "EUR")
    url = f"https://api.frankfurter.app/latest?from={base}"
    data = _get(url)
    if not data or "rates" not in data:
        return []
    rates = data["rates"]
    date = data.get("date", "")
    results = []
    # Score currencies by query relevance (mentioned first, otherwise alphabetical)
    scored = sorted(rates.items(), key=lambda kv: (0 if kv[0] in q_upper else 1, kv[0]))
    for currency, rate in scored[:limit]:
        results.append(
            _result(
                title=f"1 {base} = {rate} {currency}",
                url="https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/",
                source="frankfurter",
                institution="European Central Bank (ECB) via Frankfurter",
                snippet=f"Base: {base} · As of {date}",
                date=date,
                rid=f"{base}/{currency}",
            )
        )
    return results


# ── Recovered from the jeles-remote fork ───────────────────────────────────────
#
# These four were written in that service's vendored copy of this file and never
# registered there, so `search()` could not reach them — dead code in a repo that
# is now archived and private. They are the only thing the fork had that this
# package did not, and "unreachable" is not the same as "not worth keeping".
#
# Two changes were needed to bring them in. Every URL is https: `isfdb` and
# `omdb` were plain http upstream, which is why they could not simply be pasted
# onto a lane that is `HTTPS_ONLY`. And both of those are `opt_in`, because TLS
# on those two hosts is *unverified* — it could not be checked from the
# environment this port was written in, and a source that silently fails is
# worse than one you have to ask for. If they answer over https, drop the flag;
# if they do not, `search()` reports them under `failed` with the transport
# error, which is the mechanism that tells you.


#: Written out once and reused in the fallback regex below. A pattern spelled
#: `vault\.fbi\.gov` parses as the host `vault` to the AST reader in
#: `tests/test_source_hosts.py`, which exists to check that a source declares
#: every host it contacts — so the literal has to be the host.
_FBI_VAULT_HOST = "vault.fbi.gov"


def search_fbi_vault(query: str, limit: int = 5) -> list[dict]:
    """FBI Records Vault — declassified FBI files on persons, events, organizations."""
    url = (
        "https://vault.fbi.gov/@search?SearchableText="
        + urllib.parse.quote(query)
        + f"&portal_type:list=File&b_size={limit}"
    )
    data = _get(url)
    items: list = []
    if isinstance(data, dict):
        items = data.get("items") or data.get("@components", {}).get("items", [])
    elif isinstance(data, list):
        items = data
    results = [
        _result(
            title=item.get("title", ""),
            url=item.get("@id", ""),
            source="fbi_vault",
            institution="FBI Records Vault (Declassified)",
            snippet=(item.get("description") or "")[:200],
            date=(item.get("effective") or "")[:10],
            rid=item.get("@id", ""),
        )
        for item in items[:limit]
    ]
    if results:
        return results
    # The JSON endpoint is a Plone @search view and has changed shape before, so
    # the HTML listing is the fallback rather than the primary.
    html = _get_html("https://vault.fbi.gov/search?SearchableText=" + urllib.parse.quote(query))
    if not html:
        return []
    links = re.findall(
        rf'href="(https://{re.escape(_FBI_VAULT_HOST)}/[^"#]{{5,200}})"'
        r"[^>]*>([^<]{5,120})</a>",
        html,
    )
    return [
        _result(
            title=title.strip(),
            url=found,
            source="fbi_vault",
            institution="FBI Records Vault (Declassified)",
            rid=found,
        )
        for found, title in links[:limit]
    ]


def search_ig_nobel(query: str, limit: int = 5) -> list[dict]:
    """Ig Nobel Prize archive — research that makes you laugh, then think."""
    html = _get_html("https://www.improbable.com/?s=" + urllib.parse.quote(query))
    if not html:
        return []
    titles = re.findall(
        r'class="entry-title[^"]*">\s*<a href="([^"]+)"[^>]*>([^<]+)</a>', html, re.S
    )
    excerpts = [
        e.strip()
        for e in re.findall(r'class="entry-summary[^"]*">\s*<p>([^<]{10,400})</p>', html, re.S)
    ]
    return [
        _result(
            title=title.strip(),
            url=link,
            source="ig_nobel",
            institution="Improbable Research (Ig Nobel)",
            snippet=excerpts[i] if i < len(excerpts) else "",
            rid=link,
        )
        for i, (link, title) in enumerate(titles[:limit])
    ]


def search_isfdb(query: str, limit: int = 5) -> list[dict]:
    """ISFDB — Internet Speculative Fiction Database. Sci-fi, horror, fantasy, pulp."""
    html = _get_html(
        "https://www.isfdb.org/cgi-bin/se.cgi?arg="
        + urllib.parse.quote(query)
        + "&type=Fiction+Titles"
    )
    if not html:
        return []
    titles = re.findall(r'title\.cgi\?(\d+)">([^<]{3,120})</a>', html)
    authors = re.findall(r'author\.cgi\?[^"]+">([^<]+)</a>', html)
    years = re.findall(r'<td[^>]*class="[^"]*year[^"]*"[^>]*>(\d{4})</td>', html)
    return [
        _result(
            title=title.strip(),
            url=f"https://www.isfdb.org/cgi-bin/title.cgi?{tid}",
            source="isfdb",
            institution="Internet Speculative Fiction Database",
            snippet=authors[i] if i < len(authors) else "",
            date=years[i] if i < len(years) else "",
            rid=tid,
        )
        for i, (tid, title) in enumerate(titles[:limit])
    ]


def search_omdb(query: str, limit: int = 5) -> list[dict]:
    """OMDb — Open Movie Database. Movies, B-movies, horror, cult cinema."""
    api_key = os.environ.get("OMDB_API_KEY", "")
    if not api_key:
        return []
    data = _get(
        "https://www.omdbapi.com/?s=" + urllib.parse.quote(query) + f"&type=movie&apikey={api_key}"
    )
    if not data:
        return []
    results = []
    for item in (data.get("Search") or [])[:limit]:
        imdb_id = item.get("imdbID", "")
        results.append(
            _result(
                title=item.get("Title", ""),
                url=f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else "",
                source="omdb",
                institution="OMDb / IMDb",
                snippet=f"{item.get('Type', '').title()} · {item.get('Year', '')}",
                date=item.get("Year", ""),
                rid=imdb_id,
            )
        )
    return results


# ── Source registry ────────────────────────────────────────────────────────────
#
# `key_env` names the environment variable a source abstains without. It is
# here, rather than only inside the function body, so `search` can put the
# abstention in `skipped` with a reason before dispatching: the five registered
# functions that abstain do it with a bare `return []`, which is
# indistinguishable from an empty collection once it reaches the fan-out.
# Measured: a default fan-out on a machine with no keys reported 60 queried,
# 55 failed, and five sources — rijksmuseum, dpla, smithsonian, europeana,
# bhl — in no bucket at all, which is what kept `institutional`'s outage check
# from ever firing.
#
# Only declare `key_env` for a source whose function really does return before
# any egress. `tests/test_sources.py` pins each declaration against the actual
# behaviour, so the two cannot drift apart silently.

SOURCES: dict[str, dict] = {
    # Academic
    "openalex": {
        "name": "OpenAlex",
        "domain": ["academic", "science", "humanities"],
        "key_required": False,
        "hosts": ["api.openalex.org"],
    },
    "core": {
        "name": "CORE",
        "domain": ["academic", "science"],
        "key_required": False,
        "hosts": ["api.core.ac.uk"],
    },
    "doaj": {
        "name": "DOAJ",
        "domain": ["academic", "open_access"],
        "key_required": False,
        "hosts": ["doaj.org", "doi.org"],
    },
    "europepmc": {
        "name": "Europe PMC",
        "domain": ["biology", "medicine", "health"],
        "key_required": False,
        "hosts": ["doi.org", "europepmc.org", "www.ebi.ac.uk"],
    },
    # key_required is False because `search_semantic_scholar` does not abstain
    # without SEMANTIC_SCHOLAR_API_KEY — it queries anonymously and the key only
    # lifts rate limits. This entry said True, which made `list_sources()` claim
    # a key that is not needed; the fan-out reached it keyless in every run.
    "semantic_scholar": {
        "name": "Semantic Scholar",
        "domain": ["academic", "cs", "science"],
        "key_required": False,
        "hosts": ["api.semanticscholar.org", "doi.org"],
    },
    "crossref": {
        "name": "Crossref",
        "domain": ["academic", "general"],
        "key_required": False,
        "hosts": ["api.crossref.org", "doi.org"],
    },
    "pubmed": {
        "name": "PubMed",
        "domain": ["biology", "medicine"],
        "key_required": False,
        "hosts": ["eutils.ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov"],
    },
    "arxiv": {
        "name": "arXiv",
        "domain": ["science", "cs", "math", "physics"],
        "key_required": False,
        "hosts": ["export.arxiv.org"],
    },
    # Data / Science
    "zenodo": {
        "name": "Zenodo",
        "domain": ["science", "data", "general"],
        "key_required": False,
        "hosts": ["doi.org", "zenodo.org"],
    },
    "datacite": {
        "name": "DataCite",
        "domain": ["science", "data"],
        "key_required": False,
        "hosts": ["api.datacite.org", "doi.org"],
    },
    "wikidata": {
        "name": "Wikidata",
        "domain": ["general", "reference"],
        "key_required": False,
        "hosts": ["www.wikidata.org"],
    },
    "pubchem": {
        "name": "PubChem",
        "domain": ["chemistry", "science"],
        "key_required": False,
        "hosts": ["pubchem.ncbi.nlm.nih.gov"],
    },
    "usgs": {
        "name": "USGS Publications",
        "domain": ["geology", "earth_science"],
        "key_required": False,
        "hosts": ["doi.org", "pubs.er.usgs.gov"],
    },
    "nasa": {
        "name": "NASA",
        "domain": ["space", "science"],
        "key_required": False,
        "hosts": ["images-api.nasa.gov"],
    },
    # Museums
    "met": {
        "name": "Met Museum",
        "domain": ["art", "culture", "history"],
        "key_required": False,
        "hosts": ["collectionapi.metmuseum.org"],
    },
    "cleveland": {
        "name": "Cleveland Museum of Art",
        "domain": ["art", "culture"],
        "key_required": False,
        "hosts": ["openaccess-api.clevelandart.org"],
    },
    "vam": {
        "name": "V&A Museum",
        "domain": ["art", "design", "culture"],
        "key_required": False,
        "hosts": ["api.vam.ac.uk", "collections.vam.ac.uk"],
    },
    "rijksmuseum": {
        "name": "Rijksmuseum",
        "domain": ["art", "history"],
        "key_required": True,
        "key_env": "RIJKSMUSEUM_API_KEY",
        "hosts": ["www.rijksmuseum.nl"],
    },
    # Libraries & Archives
    "loc": {
        "name": "Library of Congress",
        "domain": ["humanities", "history", "general"],
        "key_required": False,
        "hosts": ["www.loc.gov"],
    },
    "openlibrary": {
        "name": "Open Library",
        "domain": ["books", "humanities"],
        "key_required": False,
        "hosts": ["openlibrary.org"],
    },
    "chronicling_america": {
        "name": "Chronicling America",
        "domain": ["history", "journalism"],
        "key_required": False,
        "hosts": ["www.loc.gov"],
    },
    "internet_archive": {
        "name": "Internet Archive",
        "domain": ["general", "books", "media"],
        "key_required": False,
        "hosts": ["archive.org"],
    },
    "dpla": {
        "name": "DPLA",
        "domain": ["humanities", "history", "general"],
        "key_required": True,
        "key_env": "DPLA_API_KEY",
        "hosts": ["api.dp.la"],
    },
    # Heritage
    "smithsonian": {
        "name": "Smithsonian",
        "domain": ["art", "history", "science"],
        "key_required": True,
        "key_env": "SMITHSONIAN_API_KEY",
        "hosts": ["api.si.edu"],
    },
    "europeana": {
        "name": "Europeana",
        "domain": ["art", "culture", "history"],
        "key_required": True,
        "key_env": "EUROPEANA_API_KEY",
        "hosts": ["api.europeana.eu"],
    },
    # International
    "gallica": {
        "name": "Gallica (BnF)",
        "domain": ["humanities", "history", "france"],
        "key_required": False,
        "hosts": ["gallica.bnf.fr", "www.loc.gov"],
    },
    "hal": {
        "name": "HAL Open Access",
        "domain": ["academic", "science", "france"],
        "key_required": False,
        "hosts": ["api.archives-ouvertes.fr"],
    },
    "scielo": {
        "name": "SciELO",
        "domain": ["science", "latin_america", "iberia"],
        "key_required": False,
        "hosts": ["articlemeta.scielo.org", "www.scielo.br"],
    },
    "ndl": {
        "name": "National Diet Library",
        "domain": ["general", "japan", "asia"],
        "key_required": False,
        "hosts": ["iss.ndl.go.jp", "www.loc.gov"],
    },
    # Music
    "musicbrainz": {
        "name": "MusicBrainz",
        "domain": ["music", "art", "culture"],
        "key_required": False,
        "hosts": ["musicbrainz.org"],
    },
    # Philosophy & humanities
    "sep": {
        "name": "Stanford Encyclopedia of Philosophy",
        "domain": ["philosophy", "humanities"],
        "key_required": False,
        "hosts": ["plato.stanford.edu"],
    },
    # Literature — public domain
    "gutenberg": {
        "name": "Project Gutenberg",
        "domain": ["literature", "books", "humanities"],
        "key_required": False,
        "hosts": ["gutendex.com", "www.gutenberg.org"],
    },
    # Natural history
    "bhl": {
        "name": "Biodiversity Heritage Library",
        "domain": ["biology", "ecology", "natural_history"],
        "key_required": True,
        "key_env": "BHL_API_KEY",
        "hosts": ["www.biodiversitylibrary.org"],
    },
    # Law
    # Declassified records, prize archives, genre bibliography — recovered from
    # the archived jeles-remote fork, where they were written but never registered.
    "fbi_vault": {
        "name": "FBI Records Vault",
        "domain": ["history", "government", "records"],
        "key_required": False,
        "hosts": ["vault.fbi.gov"],
    },
    "ig_nobel": {
        "name": "Improbable Research",
        "domain": ["science", "humor"],
        "key_required": False,
        "hosts": ["www.improbable.com"],
    },
    # opt_in: these two were plain http in the fork and their TLS is unverified.
    "isfdb": {
        "name": "ISFDB",
        "domain": ["literature", "science_fiction"],
        "key_required": False,
        "opt_in": True,
        "hosts": ["www.isfdb.org"],
    },
    "omdb": {
        "name": "OMDb / IMDb",
        "domain": ["film", "media"],
        "key_required": True,
        "key_env": "OMDB_API_KEY",
        "opt_in": True,
        "hosts": ["www.omdbapi.com", "www.imdb.com"],
    },
    "courtlistener": {
        "name": "CourtListener",
        "domain": ["law", "legal"],
        "key_required": False,
        "hosts": ["www.courtlistener.com"],
    },
    # Broad academic open access
    "base": {
        "name": "BASE (Bielefeld)",
        "domain": ["academic", "general", "open_access"],
        "key_required": False,
        "hosts": ["api.base-search.net"],
    },
    # Computer science
    "dblp": {
        "name": "DBLP",
        "domain": ["computer_science", "academic"],
        "key_required": False,
        "hosts": ["dblp.org"],
    },
    # Drug / medical safety
    "openfda": {
        "name": "OpenFDA",
        "domain": ["medicine", "drug", "safety"],
        "key_required": False,
        "hosts": ["api.fda.gov", "www.accessdata.fda.gov"],
    },
    # Species / ecology
    "eol": {
        "name": "Encyclopedia of Life",
        "domain": ["biology", "ecology", "species"],
        "key_required": False,
        "hosts": ["eol.org"],
    },
    "gbif": {
        "name": "GBIF",
        "domain": ["biology", "ecology", "biodiversity"],
        "key_required": False,
        "hosts": ["api.gbif.org", "www.gbif.org"],
    },
    "inaturalist": {
        "name": "iNaturalist",
        "domain": ["biology", "ecology", "species"],
        "key_required": False,
        "hosts": ["api.inaturalist.org", "www.inaturalist.org"],
    },
    # Geography
    "nominatim": {
        "name": "OpenStreetMap Nominatim",
        "domain": ["geography", "places"],
        "key_required": False,
        "hosts": ["nominatim.openstreetmap.org", "www.openstreetmap.org"],
    },
    # European open research
    "openaire": {
        "name": "OpenAIRE",
        "domain": ["academic", "europe", "open_access"],
        "key_required": False,
        "hosts": ["api.openaire.eu", "doi.org"],
    },
    # Government open data
    "federal_register": {
        "name": "U.S. Federal Register",
        "domain": ["law", "government", "us"],
        "key_required": False,
        "hosts": ["www.federalregister.gov"],
    },
    "datagov": {
        "name": "data.gov",
        "domain": ["government", "data", "us"],
        "key_required": False,
        "hosts": ["catalog.data.gov"],
    },
    "uk_legislation": {
        "name": "legislation.gov.uk",
        "domain": ["law", "government", "uk"],
        "key_required": False,
        "hosts": ["www.legislation.gov.uk"],
    },
    "eu_data": {
        "name": "data.europa.eu",
        "domain": ["government", "data", "europe"],
        "key_required": False,
        "hosts": ["data.europa.eu"],
    },
    # Clinical trade press
    "psychiatric_times": {
        "name": "Psychiatric Times",
        "domain": ["psychiatry", "mental_health", "medicine"],
        "key_required": False,
        "hosts": ["www.psychiatrictimes.com"],
    },
    # High-energy physics
    "inspirehep": {
        "name": "INSPIRE-HEP",
        "domain": ["physics", "high_energy_physics", "science"],
        "key_required": False,
        "hosts": ["arxiv.org", "doi.org", "inspirehep.net"],
    },
    # Economics / macroeconomics
    "worldbank": {
        "name": "World Bank Open Data",
        "domain": ["economics", "finance", "government"],
        "key_required": False,
        "hosts": ["api.worldbank.org", "data.worldbank.org"],
    },
    # Food & nutrition
    "openfoodfacts": {
        "name": "Open Food Facts",
        "domain": ["food", "nutrition", "science"],
        "key_required": False,
        "hosts": ["world.openfoodfacts.org"],
    },
    # Environment / energy
    "carbon_intensity": {
        "name": "UK Carbon Intensity",
        "domain": ["environment", "energy", "climate"],
        "key_required": False,
        "hosts": ["api.carbonintensity.org.uk", "carbonintensity.org.uk"],
    },
    # Weather
    "nws": {
        "name": "National Weather Service",
        "domain": ["weather", "government", "science"],
        "key_required": False,
        "hosts": ["api.weather.gov", "www.weather.gov"],
    },
    # News
    "gdelt": {
        "name": "GDELT",
        "domain": ["news", "current_events"],
        "key_required": False,
        "hosts": ["api.gdeltproject.org"],
    },
    # Public health
    "who_gho": {
        "name": "WHO Global Health Observatory",
        "domain": ["public_health", "medicine"],
        "key_required": False,
        "hosts": ["ghoapi.azureedge.net", "www.who.int"],
    },
    # Global weather/climate
    "open_meteo": {
        "name": "Open-Meteo",
        "domain": ["climate", "weather"],
        "key_required": False,
        "hosts": ["api.open-meteo.com", "geocoding-api.open-meteo.com", "open-meteo.com"],
    },
    # Patents
    # opt_in because search.patentsview.org has been DNS-dead since 2026
    # (willow-2.0 #648): every default fan-out spent a full timeout on a name
    # that does not resolve. `patents.google.com` is the half that still works,
    # so the entry stays registered and reachable by name rather than deleted.
    "patentsview": {
        "name": "USPTO PatentsView",
        "domain": ["patents", "technology", "science"],
        "key_required": False,
        "opt_in": True,
        "hosts": ["patents.google.com", "search.patentsview.org"],
    },
    # Macroeconomics
    "imf": {
        "name": "IMF DataMapper",
        "domain": ["macroeconomics", "economics"],
        "key_required": False,
        "hosts": ["www.imf.org"],
    },
    # Social science preprints
    "osf": {
        "name": "Open Science Framework",
        "domain": ["social_science", "psychology", "medicine"],
        "key_required": False,
        "hosts": ["api.osf.io"],
    },
    # Sports
    "thesportsdb": {
        "name": "TheSportsDB",
        "domain": ["sports"],
        "key_required": False,
        "hosts": ["www.thesportsdb.com"],
    },
    # Finance / FX
    "frankfurter": {
        "name": "ECB via Frankfurter",
        "domain": ["finance", "economics"],
        "key_required": False,
        "hosts": ["api.frankfurter.app", "www.ecb.europa.eu"],
    },
    # Opt-in only — general reference, not suitable for academic citation
    "wikipedia": {
        "name": "Wikipedia",
        "domain": ["general", "reference"],
        "fn_name": "search_wikipedia",
        "key_required": False,
        "opt_in": True,
        "hosts": ["en.wikipedia.org"],
    },
}

# ── Static source registry ────────────────────────────────────────────────────
# No Postgres in this service — SOURCES above is the single source of truth.
# Dispatch resolves fn_name strings via getattr — no function pointers needed.

# Upper bound on sockets a single fan-out opens at once. A default search asks
# ~60 sources; opening 60 connections in parallel is not politeness.
_MAX_WORKERS = 16


def _executor(workers: int) -> _cf.ThreadPoolExecutor:
    """A pool for one `search` call, disposed of by that call.

    This was a module-level pool shared across every call, and that starved
    later calls. A source that outlives `wall_clock_limit` cannot be killed —
    Python threads never are — so its worker stayed occupied after `search`
    returned and the next call queued behind abandoned work. Measured on the
    shared pool: 16 sources still sleeping past a 0.4s cap made the *next*
    call, whose single source returned instantly, produce no results at all
    within a 3.0s limit. A per-call pool keeps that cost inside the call that
    incurred it.

    What this does not do is stop the straggler. Nothing here can; the thread
    runs until its own work finishes, bounded in practice by the per-request
    socket timeout (`_TIMEOUT`) times however many requests the source makes.
    `concurrent.futures` joins any such thread at interpreter exit, so a
    process that exits mid-straggler waits for it — true of the shared pool
    too, and unchanged by this.

    Still built on demand, never at import: an idle pool in every process that
    merely imports jeles would be a side effect this module promises not to have.
    """
    return _cf.ThreadPoolExecutor(
        max_workers=max(1, min(_MAX_WORKERS, workers)),
        thread_name_prefix="jeles-src",
    )


def _load_registry() -> dict[str, dict]:
    """Static {source_id: {name, fn_name, key_required, key_env, opt_in, hosts,
    enabled}} from SOURCES. `key_env` is "" for a source that needs no key."""
    registry: dict[str, dict] = {}
    for sid, cfg in SOURCES.items():
        registry[sid] = {
            "name": cfg.get("name", sid),
            "fn_name": cfg.get("fn_name") or f"search_{sid}",
            "key_required": cfg.get("key_required", False),
            "key_env": cfg.get("key_env", ""),
            "opt_in": cfg.get("opt_in", False),
            # Rebuilt key by key, so anything not listed here is dropped —
            # which is how `hosts` read as empty everywhere the first time.
            "hosts": tuple(cfg.get("hosts", ())),
            "enabled": True,
        }
    return registry


def _resolve_fn(fn_name: str):
    """Resolve a search function by name from this module."""
    return getattr(_sys.modules[__name__], fn_name, None)


# ── Domain routing ────────────────────────────────────────────────────────────
# Each entry: (keyword_list, source_ids). First match wins. Keyword-based only —
# no embeddings/Ollama in this service.

# High-priority history queries — checked before broad government/policy keywords.
_HISTORY_QUERY_OVERRIDES: list[tuple[list[str], list[str]]] = [
    (
        [
            "french revolution",
            "revolution of 1789",
            "bastille",
            "reign of terror",
            "napoleonic wars",
            "louis xvi",
        ],
        ["gallica", "loc", "internet_archive", "openlibrary"],
    ),
]

_DOMAIN_ROUTES: list[tuple[list[str], list[str]]] = [
    *_HISTORY_QUERY_OVERRIDES,
    (
        [
            "law",
            "legal",
            "court",
            "case law",
            "statute",
            "legislation",
            "judicial",
            "ruling",
            "verdict",
            "judge",
            "attorney",
            "plaintiff",
            "defendant",
            "precedent",
            "supreme court",
            "amendment",
            "regulation",
            "act of congress",
            "bill passed",
            "federal law",
            "constitution",
        ],
        ["courtlistener", "federal_register", "openalex"],
    ),
    (
        [
            "government",
            "policy",
            "federal",
            "parliament",
            "senate",
            "congress",
            "ministry",
            "department of",
            "executive order",
            "public sector",
            "cabinet",
            "prime minister",
            "president policy",
            "uk law",
            "eu law",
            "european union regulation",
            "government data",
            "open data",
        ],
        ["federal_register", "datagov", "uk_legislation", "eu_data"],
    ),
    (
        [
            "species",
            "animal",
            "bird",
            "fish",
            "insect",
            "plant",
            "mammal",
            "reptile",
            "amphibian",
            "fungus",
            "microbe",
            "bacteria",
            "wildlife",
            "observed in the wild",
            "sighting",
            "habitat",
            "endangered",
            "iucn",
        ],
        ["inaturalist", "gbif", "eol", "bhl"],
    ),
    (
        [
            "geography",
            "country",
            "city",
            "capital",
            "river",
            "mountain",
            "continent",
            "population density",
            "location of",
            "where is",
            "coordinates",
            "region",
            "territory",
            "border between",
            "nation",
            "province",
            "county",
            "lake",
            "ocean",
            "sea",
            "bay",
            "peninsula",
            "island",
        ],
        ["nominatim", "wikidata", "openalex"],
    ),
    (
        [
            "music",
            "song",
            "album",
            "band",
            "artist",
            "musician",
            "rapper",
            "hip hop",
            "hip-hop",
            "jazz",
            "blues",
            "rock",
            "pop",
            "genre",
            "record",
            "track",
            "lyrics",
            "singer",
            "producer",
            "discography",
            "discogs",
            "recording",
        ],
        ["musicbrainz", "openlibrary"],
    ),
    (
        [
            "ship",
            "vessel",
            "hull",
            "marine",
            "nautical",
            "barnacle",
            "antifouling",
            "corrosion",
            "rust",
            "copper",
            "boat",
            "submarine",
            "naval",
            "dock",
            "buoyancy",
            "ballast",
            "keel",
        ],
        ["pubchem", "crossref", "openalex"],
    ),
    (
        [
            "paint",
            "artwork",
            "sculpture",
            "portrait",
            "drawing",
            "exhibition",
            "canvas",
            "fresco",
            "engraving",
            "watercolor",
            "print",
            "photograph",
            "illustration",
            "tapestry",
            "mosaic",
            "rembrandt",
            "vermeer",
            "picasso",
            "van gogh",
            "monet",
            "museum collection",
            "art history",
        ],
        ["met", "cleveland", "vam", "wikidata", "europeana"],
    ),
    (
        [
            "psychiatry",
            "psychiatric",
            "mental health",
            "mental illness",
            "depression",
            "anxiety",
            "bipolar",
            "schizophrenia",
            "psychosis",
            "ptsd",
            "adhd",
            "autism",
            "ocd",
            "personality disorder",
            "substance use",
            "addiction",
            "suicide",
            "self-harm",
            "antidepressant",
            "antipsychotic",
            "ssri",
            "snri",
            "benzodiazepine",
            "therapy",
            "psychotherapy",
            "cbt",
            "dbt",
            "dsm",
            "psychiatric medication",
            "mental disorder",
        ],
        ["psychiatric_times", "pubmed", "europepmc"],
    ),
    (
        [
            "disease",
            "drug",
            "medicine",
            "treatment",
            "syndrome",
            "virus",
            "bacteria",
            "health",
            "clinical",
            "therapy",
            "gene",
            "protein",
            "vaccine",
            "cancer",
            "surgery",
            "diagnosis",
            "pharmacology",
        ],
        ["pubmed", "europepmc", "pubchem"],
    ),
    (
        [
            "chemical",
            "compound",
            "molecule",
            "element",
            "reaction",
            "formula",
            "acid",
            "polymer",
            "catalyst",
            "synthesis",
            "isotope",
        ],
        ["pubchem", "crossref", "arxiv"],
    ),
    (
        [
            "physics",
            "quantum",
            "algorithm",
            "machine learning",
            "neural network",
            "mathematics",
            "theorem",
            "computer science",
            "programming",
            "deep learning",
            "artificial intelligence",
            "ai",
            "cryptography",
            "compiler",
        ],
        ["arxiv", "semantic_scholar", "openalex"],
    ),
    (
        [
            "space",
            "nasa",
            "planet",
            "star",
            "galaxy",
            "asteroid",
            "orbit",
            "telescope",
            "astronomy",
            "cosmos",
            "lunar",
            "solar system",
            "comet",
            "exoplanet",
        ],
        ["nasa", "arxiv", "openalex"],
    ),
    (
        [
            "geology",
            "earthquake",
            "volcano",
            "mineral",
            "hydrology",
            "fossil",
            "sediment",
            "tectonic",
            "seismic",
            "groundwater",
        ],
        ["usgs", "openalex", "zenodo"],
    ),
    (
        [
            "history",
            "historical",
            "century",
            "war",
            "revolution",
            "colonial",
            "ancient",
            "newspaper",
            "archive",
            "president",
            "congress",
            "empire",
            "dynasty",
            "civil war",
            "world war",
            "medieval",
            "renaissance",
        ],
        ["loc", "chronicling_america", "internet_archive", "openlibrary"],
    ),
    (
        [
            "philosophy",
            "ethics",
            "epistemology",
            "metaphysics",
            "kant",
            "aristotle",
            "plato",
            "hegel",
            "nietzsche",
            "descartes",
            "hume",
            "wittgenstein",
            "locke",
            "moral",
            "ontology",
            "phenomenology",
            "consciousness",
            "free will",
            "logic",
            "categorical imperative",
            "utilitarianism",
            "existentialism",
        ],
        ["sep", "openalex", "crossref"],
    ),
    (
        [
            "natural history",
            "species",
            "taxonomy",
            "ecology",
            "evolution",
            "darwin",
            "botany",
            "zoology",
            "entomology",
            "ornithology",
            "flora",
            "fauna",
            "biodiversity",
            "specimen",
            "genus",
            "phylum",
            "habitat",
        ],
        ["bhl", "openalex", "crossref"],
    ),
    (
        [
            "book",
            "novel",
            "author",
            "literature",
            "poem",
            "fiction",
            "publish",
            "writer",
            "text",
            "manuscript",
            "edition",
            "play",
            "essay",
            "anthology",
        ],
        ["gutenberg", "openlibrary", "loc"],
    ),
    (
        ["france", "french", "paris", "napoleon", "versailles", "de gaulle", "alsace", "bretagne"],
        ["gallica", "hal", "europeana"],
    ),
    (["japan", "japanese", "tokyo", "kyoto", "manga", "samurai", "meiji"], ["ndl", "openalex"]),
]

_DEFAULT_SOURCES = ["base", "openalex", "crossref", "wikidata"]
_MAX_ROUTE_SOURCES = 6


def _route_override(query: str) -> list[str] | None:
    q = query.lower()
    for keywords, sources in _HISTORY_QUERY_OVERRIDES:
        if any(kw in q for kw in keywords):
            return sources[:_MAX_ROUTE_SOURCES]
    return None


def route_sources(query: str) -> list[str]:
    """Select sources for a query based on domain keyword matching.
    Static routes only (_DOMAIN_ROUTES) — fast, no HTTP, no LLM, no DB."""
    override = _route_override(query)
    if override:
        return override
    q = query.lower()
    for keywords, sources in _DOMAIN_ROUTES:
        if any(kw in q for kw in keywords):
            return sources[:_MAX_ROUTE_SOURCES]
    return _DEFAULT_SOURCES


NO_WIKIPEDIA_NOTE = (
    "Wikipedia is excluded — results are from primary institutions "
    "and peer-reviewed sources suitable for academic citation."
)


_QUESTION_WORDS = re.compile(
    r"^(what|who|when|where|why|how|which|tell me about|find|look up|search for|"
    r"can you find|give me|show me)\s+",
    re.IGNORECASE,
)
_FILLER_WORDS = re.compile(
    r"\b(did|was|were|is|are|has|have|had|do|does|an|and|or|of|in|on|at|by|"
    r"for|with|about|release|released|make|made|create|created|write|wrote|publish|"
    r"published|appear|appeared|come|came|from|to|into)\b",
    re.IGNORECASE,
)

#: The article "a", stripped in lowercase only — deliberately NOT part of the
#: IGNORECASE list above. A lone capital letter carries meaning: "drug A" and
#: "drug B" are different questions, and "vitamin A" is not "vitamin". Folded
#: into `_FILLER_WORDS` it matched either case, so "Does drug A interact with
#: X?" became "drug interact X" and the two drugs asked the same query.
#: `corpus.py` learned this on the storage side and keeps "a" and "i" out of
#: its stop set for the same reason (tests/test_corpus.py::
#: test_single_letters_stay_meaning_bearing); this is that rule on the query
#: side, which is where the question is still a sentence.
_LOWERCASE_A = re.compile(r"\ba\b")
# "the" stripped separately — only remove standalone "the" not preceding a capital (proper noun)
#: A lowercase "the" that does not introduce a proper noun. Case-sensitive on
#: purpose, and that is the whole mechanism: the negative lookahead is what
#: preserves "The Hague" and "The Beatles". Compiled with `re.IGNORECASE`, as
#: it was, `[A-Z]` also matched lowercase — so the lookahead succeeded before
#: every word and this pattern stripped nothing at all except before
#: punctuation or end-of-string. "What is the capital of France?" came back as
#: "the capital France", carrying the article it exists to remove.
_LONE_THE = re.compile(r"\bthe\b(?!\s+[A-Z])")


def question_to_query(question: str) -> str:
    """Derive a search-friendly query from a natural language question.
    Strips question words and common fillers; preserves proper nouns and key terms."""
    q = question.strip().rstrip("?").rstrip(".")
    q = _QUESTION_WORDS.sub("", q)
    q = _FILLER_WORDS.sub(" ", q)
    q = _LOWERCASE_A.sub(" ", q)
    q = _LONE_THE.sub(" ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q or question.rstrip("?")


# The verbatim system message willow-2.0's `question_to_intent` sends its LLM
# (jeles_sources.py:3305-3330) — kept as a public constant so a `respond`
# passed in below reproduces the exact few-shot framing upstream tuned,
# rather than a paraphrase that quietly routes differently.
_INTENT_SYSTEM_PROMPT = (
    "Extract the core factual query from this question as a search phrase. "
    "Output ONLY 4-8 domain-specific keywords — NO filler words like "
    "'description', 'information', 'facts', 'overview', 'details', 'history'. "
    "Keep proper nouns, technical terms, and subject-matter context. "
    "No punctuation. No explanation. Examples:\n"
    "Q: Why do ships painted red on the bottom last longer? "
    "→ antifouling copper paint ship hull protection\n"
    "Q: What albums did The Streets release? "
    "→ The Streets discography albums UK rap\n"
    "Q: Who invented the telephone? "
    "→ telephone invention Bell Gray patent\n"
    "Q: What is the International Space Station? "
    "→ International Space Station NASA orbital laboratory crew\n"
    "Q: What is habeas corpus? "
    "→ habeas corpus writ legal custody court"
)


def question_to_intent(
    question: str,
    *,
    respond: Callable[[str, str], str] | None = None,
) -> str:
    """Extract the factual core of a natural-language question as a search phrase.

    Converts trivia framing ("Why do ships painted red on the bottom last
    longer?") into a clean, keyword-only phrase ("antifouling copper paint
    ship hull protection") via an LLM call — one step ahead of
    `question_to_query`'s regex heuristic in the pipeline willow-2.0 runs
    (`sap/sap_mcp.py`'s `mem_jeles_ask`: `question_to_intent` first, then
    `route_sources_semantic`/`route_sources` on the intent, then
    `question_to_query(intent)` for the actual search string). Ported from
    willow-2.0's `question_to_intent` (jeles_sources.py:3305-3330), which is
    itself never called *inside* `search()` there — it is a preprocessing step
    the caller runs on the question before calling search, exactly like
    `question_to_query` already is in this module. This function is that same
    kind of standalone helper, not something `search()` below calls itself.

    jeles promises zero runtime dependencies and no network beyond the source
    APIs (`_egress`) — see the module docstring — so it cannot bundle the LLM
    client (`core.llm_edge`) upstream imports inline. `respond`, if given, is
    called as `respond(_INTENT_SYSTEM_PROMPT, question)` and must return the
    keyword phrase as a bare string: the shape of one LLM chat completion,
    left unopinionated about which provider answers it, so a host wires in
    whatever it already has rather than this module choosing for it.

    Without `respond` — the default, and upstream's own fallback whenever its
    LLM call raises, since the whole thing there sits inside one `try: ...
    except Exception: return question` — this returns `question` unchanged.
    jeles has no LLM dependency at all, so that fallback is this function's
    only behaviour until a host supplies one; it is not a degraded case, it is
    the normal one. Never raises: a `respond` that throws, times out, or hands
    back garbage is treated the same as not having one, because this is a
    search-quality nicety and no fan-out result should depend on it.
    """
    if respond is not None:
        try:
            result = (respond(_INTENT_SYSTEM_PROMPT, question) or "").strip()[:200]
            if result:
                return result
        except Exception as e:
            log.debug("question_to_intent: respond() failed, falling back: %s", e)
    return question


def list_sources() -> list[dict]:
    """Return source registry metadata.

    `key_env` names the variable a source abstains without, so a caller can see
    *which* key is missing rather than only that one is — the same string
    `search` puts in `skipped`."""
    registry = _load_registry()
    return [
        {
            "id": sid,
            "name": cfg["name"],
            "fn_name": cfg["fn_name"],
            "key_required": cfg["key_required"],
            "key_env": cfg.get("key_env", ""),
            "opt_in": cfg.get("opt_in", False),
            "hosts": list(cfg.get("hosts", ())),
        }
        for sid, cfg in registry.items()
    ]


# Hosts that appear as strings inside a source function but are never contacted:
# XML/RDF namespace *identifiers*, which look like URLs and are not. They are
# named here rather than filtered by guesswork, because a consumer treating them
# as "somewhere jeles talks to" would draw a real conclusion from a fiction —
# willow-mcp's trust list did exactly that with `www.w3.org`, picked up from
# arXiv's Atom namespace.
NAMESPACE_URI_HOSTS = frozenset({"www.w3.org", "purl.org"})


def registered_hosts(*, include_opt_in: bool = True) -> set[str]:
    """Every hostname the registered sources contact.

    Declared per source in :data:`SOURCES` under ``hosts`` and checked against
    the code by ``tests/test_source_hosts.py``, so this is data rather than a
    second list to maintain.

    This is what the sources *query*, which is not the same as where their
    results *point*: 46 of the 61 build the citation URL out of the API
    response, so OpenAlex or Crossref can legitimately return a link to any
    publisher on earth. A consumer wanting "is this hostname an institution
    this fleet queries?" is answered here; "should I believe this arbitrary
    web result?" is not a question this set can answer.
    """
    return {
        host
        for cfg in _load_registry().values()
        if include_opt_in or not cfg.get("opt_in", False)
        for host in cfg.get("hosts", ())
    }


# ── Prose gate ─────────────────────────────────────────────────────────────────
# Structured-lookup APIs 4xx or return nonsense on prose queries: a
# compound/drug/dataset/rate/weather/sports lookup wants an entity or a bare
# keyword, never a sentence. `search_pubchem("aspirin")` finds the compound;
# `search_pubchem("what pain reliever did Bayer patent in 1899")` 4xxs, and the
# only visible effect is one more entry in `failed` that reads exactly like an
# outage — indistinguishable, from `search()`'s return value alone, from
# PubChem actually being down. Ported from willow-2.0's PROSE_UNSAFE_SOURCES /
# `_is_prose` (jeles_sources.py:3421-3435, willow-2.0 #650) — a deliberately
# dumb, cheap gate ahead of the real fix upstream planned (a router step that
# runs the query through `question_to_query`/`question_to_intent` before these
# sources are even considered, willow-2.0 ADR-20260702). `search` below applies
# it to every dispatched source, an explicit `sources=[...]` list included, so
# naming a structured source directly does not bypass the gate — the mismatch
# is the same either way.
PROSE_UNSAFE_SOURCES = frozenset(
    {
        "pubchem",
        "openfda",
        "datagov",
        "eu_data",  # named in willow-2.0 #650
        "frankfurter",
        "imf",
        "worldbank",  # rates / macro indicators
        "open_meteo",
        "nws",
        "carbon_intensity",  # weather / grid
        "thesportsdb",
        "who_gho",
        "nominatim",  # sports / health stats / geocoding
    }
)

_PROSE_WORD_THRESHOLD = 6


def _is_prose(query: str) -> bool:
    """Dumb prose test: 6+ words reads as a sentence, not an entity lookup.

    ``"aspirin"`` or ``"USD EUR rate"`` pass through; ``"what pain reliever did
    Bayer patent in 1899"`` gets gated. Word count only, no punctuation or
    question-mark check — deliberately, per willow-2.0: a keyword list a
    caller pastes in ("aspirin ibuprofen acetaminophen naproxen paracetamol
    codeine") is exactly as long as a real sentence and reads as prose here
    too. That false positive is the cost of a test that needs no NLP and runs
    on every dispatched source with no measurable overhead.
    """
    return len(query.split()) >= _PROSE_WORD_THRESHOLD


# ── Cache ──────────────────────────────────────────────────────────────────────
# Opt-in, append-only JSONL log of everything `search` finds: one line per hit,
# annotated with the source's confidence tier and a set of keyword/domain tags.
# Ported from willow-2.0's `_write_cache` (jeles_sources.py:180-213, called from
# `search()` at jeles_sources.py:3538).
#
# Its read-side counterpart there is not another function in the same module —
# `jeles_sources.py` never reads its own cache back. The reader is a separate
# offline pass, `scripts/promote_jeles_cache.py`: it globs every `*.jsonl` file
# this writes, groups hits by URL, and promotes the ones that clear a hit-count
# or confidence bar into willow-mcp's corpus over Postgres. That promotion step
# is infrastructure this package does not have and, per its zero-runtime-
# dependency promise (`pyproject.toml`'s `dependencies = []`), cannot bundle.
# `_read_cache` below is that reader's load step only — glob + parse
# (`promote_jeles_cache.py:75-89`, `_load_cache`) — without the Postgres write:
# the offline-testable, dependency-free half of the mechanism a host embedding
# jeles can build its own promotion pass on top of, using the same record shape
# `_write_cache` produces.
#
# Disabled unless `JELES_SOURCES_CACHE_DIR` is set, unlike everything upstream
# of it in this module: writing to disk on every call is a side effect the rest
# of this file goes out of its way not to have — no thread pool, no opener,
# nothing at import (see `_executor`'s docstring). A host that wants the audit
# trail turns it on; one that does not gets exactly today's behaviour.
_CACHE_DIR = os.environ.get("JELES_SOURCES_CACHE_DIR", "")


def _write_cache(query: str, results: dict[str, list]) -> None:
    """Append one JSONL record per hit to today's UTC-dated cache file.

    Each record is the hit's own citation dict (`_result`'s shape) plus: a
    per-URL ``id`` (an 8-char, non-security md5 of the url — distinct from the
    citation's own ``id`` field, which is per-source and not stable across a
    rerun), the ``query`` that found it, ``keywords`` (query words over 3
    chars, deduped against the source id and its registry ``domain`` tags),
    ``tags`` (the source id, a hash of the query, and those same domain tags),
    a ``tier`` of ``"fetched"``, the source's ``_SOURCE_CONFIDENCE`` (0.80 if
    untiered — the same default the rest of this module uses for "reputable
    but not scored"), a UTC ``fetched_at``, and ``promoted: False`` — the flag
    a downstream promotion pass flips once a record is folded into a corpus,
    so re-running that pass stays idempotent.

    No-ops with a logged warning on any failure (unwritable directory, full
    disk, ...): called for its side effect only, after `search` has already
    built the value it is about to return, so nothing here may turn a
    successful search into a failed one.
    """
    if not _CACHE_DIR:
        return
    try:
        cache_dir = Path(_CACHE_DIR).expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cache_file = cache_dir / f"{today}.jsonl"
        fetched_at = datetime.now(timezone.utc).isoformat()
        query_words = [w.lower() for w in query.split() if len(w) > 3]
        query_hash = hashlib.md5(query.encode(), usedforsecurity=False).hexdigest()[:8]
        with cache_file.open("a", encoding="utf-8") as fh:
            for source_id, hits in results.items():
                confidence = _SOURCE_CONFIDENCE.get(source_id, 0.80)
                domain_tags = SOURCES.get(source_id, {}).get("domain", [])
                for hit in hits:
                    url = hit.get("url", "")
                    cache_id = (
                        hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:8]
                        if url
                        else ""
                    )
                    keywords = list(dict.fromkeys([*query_words, source_id, *domain_tags]))[:10]
                    tags = [source_id, f"query:{query_hash}", *domain_tags]
                    record = {
                        **hit,
                        "id": cache_id,
                        "query": query,
                        "keywords": keywords,
                        "tags": tags,
                        "tier": "fetched",
                        "confidence": confidence,
                        "fetched_at": fetched_at,
                        "promoted": False,
                    }
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("jeles cache write failed: %s", e)


def _read_cache() -> list[dict]:
    """Load every record `_write_cache` has appended, across every dated file.

    Mirrors `promote_jeles_cache.py`'s `_load_cache()`: glob ``*.jsonl`` in
    filename (date) order, parse each line as its own JSON object, and skip a
    line that fails to parse rather than aborting the file it is in — a
    promotion pass or an audit query over a partially-written or hand-edited
    cache file should see every good record, not zero of them because one
    line near the top is truncated.

    Returns ``[]``, with a warning logged rather than raised, when caching is
    disabled (``JELES_SOURCES_CACHE_DIR`` unset) or the directory cannot be
    read — the same "absence is not an error" contract `_get`/`_get_html` use
    for a source that cannot be reached.
    """
    if not _CACHE_DIR:
        return []
    records: list[dict] = []
    try:
        cache_dir = Path(_CACHE_DIR).expanduser()
        for jsonl_file in sorted(cache_dir.glob("*.jsonl")):
            try:
                for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            except OSError as e:
                log.warning("jeles cache read failed for %s: %s", jsonl_file.name, e)
    except OSError as e:
        log.warning("jeles cache read failed: %s", e)
    return records


def search(
    query: str,
    sources: list[str] | None = None,
    limit_per_source: int = 3,
    wall_clock_limit: float = 20.0,
) -> dict:
    """Search across trusted sources. Static-registry dispatch via fn_name strings.
    sources=None → all non-opt-in sources. Pass a list to target specific ones.

    Concurrent: up to `_MAX_WORKERS` sources run in parallel in a pool belonging
    to this call. `wall_clock_limit` caps the total wait; the per-request socket
    timeout (`_TIMEOUT`) bounds an individual request but not a source function,
    which may issue several in sequence.

    Returns::

        {
          "query":           str,
          "sources_queried": [sid, ...],   # dispatched — not merely requested
          "unknown":         [sid, ...],   # requested, no registry entry / no function
          "skipped":         {sid: reason},# abstained before any egress (missing key,
                                            # or a prose query vs a PROSE_UNSAFE_SOURCES
                                            # entry — see `_is_prose`)
          "failed":          {sid: error}, # attempted egress and failed
          "timed_out":       [sid, ...],   # unfinished at wall_clock_limit
          "results":         {sid: [hit, ...]},   # [] means reached, had nothing
          "total":           int,
          "note":            str,
        }

    **Every sid in `sources_queried` appears in exactly one of `results`,
    `skipped`, `failed`, `timed_out`.** That is the point of the four buckets:
    a caller deciding "outage or empty shelf?" needs the ones that produced no
    hits to say *why*, and previously they could vanish from the response
    entirely. `unknown` is deliberately outside `sources_queried` — nothing was
    dispatched for it, so counting it as queried let a single typo disarm a
    consumer's `len(failed) >= len(queried)` check.

    `results[sid] == []` is a real answer: that source was reached and had
    nothing. Absence from `results` is not.

    A query that reads as prose (`_is_prose`, 6+ words) gates any requested
    source in `PROSE_UNSAFE_SOURCES` into `skipped` before it is dispatched —
    those APIs 4xx or return nonsense on a sentence, and letting them through
    only relabels that as a `failed` entry that looks exactly like an outage.
    The gate runs on an explicit `sources=[...]` list too, so asking for
    `pubchem` by name does not bypass it.

    On success (`total > 0`), results are appended to the on-disk cache via
    `_write_cache` — a no-op unless `JELES_SOURCES_CACHE_DIR` is set.
    """
    registry = _load_registry()
    if sources:
        requested = list(sources)
    else:
        requested = [
            sid
            for sid, cfg in registry.items()
            if not cfg.get("opt_in") and cfg.get("enabled", True)
        ]

    prose = _is_prose(query)

    unknown: list[str] = []
    skipped: dict[str, str] = {}
    resolved: list[tuple[str, object]] = []
    for sid in requested:
        if prose and sid in PROSE_UNSAFE_SOURCES:
            skipped[sid] = "prose query vs structured-only source (willow-2.0 #650)"
            continue
        cfg = registry.get(sid)
        if not cfg:
            log.warning("Unknown source: %s", sid)
            unknown.append(sid)
            continue
        fn = _resolve_fn(cfg["fn_name"])
        if not fn:
            log.warning("No function found for source %s (fn_name=%s)", sid, cfg["fn_name"])
            unknown.append(sid)
            continue
        # Checked here rather than inside the source function, which signals the
        # same condition with a bare `return []` that the fan-out cannot tell
        # from an empty collection. Naming the variable is the useful part: a
        # caller can act on "SMITHSONIAN_API_KEY is not set".
        key_env = cfg.get("key_env") or ""
        if key_env and not os.environ.get(key_env):
            skipped[sid] = f"{key_env} is not set"
            continue
        resolved.append((sid, fn))

    # Dispatched, so `skipped` sources belong here — they were selected and
    # accounted for, just never asked anything of the network. `unknown` does
    # not: nothing was dispatched for it. Kept in the caller's order.
    _dispatched = {sid for sid, _ in resolved} | set(skipped)
    queried = [sid for sid in requested if sid in _dispatched]

    results: dict[str, list] = {}
    failed: dict[str, str] = {}
    timed_out: list[str] = []

    def _call(sid: str, fn) -> tuple[str, list | None, str | None]:
        """Run one source on a worker and *return* its outcome.

        Returns (sid, hits, error) with exactly one of hits/error set. It writes
        nothing the caller can see: a straggler that finishes after
        `wall_clock_limit` used to keep mutating the `failed` dict `search` had
        already returned, so a consumer iterating it could get
        `RuntimeError: dictionary changed size during iteration` — reproduced,
        with the dict growing 3 → 15 entries after the return. Individual dict
        writes are atomic under the GIL, so that was a snapshot bug rather than
        a corruption one; either way the returned value described no moment in
        time. Recording only in the calling thread makes it a snapshot by
        construction.
        """
        _take_transport_failure()  # clear anything left on this worker
        try:
            hits = list(fn(query, limit_per_source) or [])
            if not hits:
                # A source that could not be reached and one that genuinely had
                # nothing both return []; the breadcrumb is what keeps them apart.
                err = _take_transport_failure()
                if err:
                    return sid, None, err
            return sid, hits, None
        except Exception as e:
            log.warning("Source %s failed: %s", sid, e)
            return sid, None, f"{type(e).__name__}: {e}"

    def _record(sid: str, hits: list | None, err: str | None) -> None:
        if err is not None:
            failed[sid] = err
        else:
            results[sid] = hits or []

    if resolved:
        pool = _executor(len(resolved))
        try:
            futures = {pool.submit(_call, sid, fn): sid for sid, fn in resolved}
            try:
                for fut in _cf.as_completed(futures, timeout=wall_clock_limit):
                    try:
                        _record(*fut.result())
                    except Exception as e:  # _call catches Exception; belt and braces
                        log.warning("Source %s result error: %s", futures[fut], e)
                        failed[futures[fut]] = f"{type(e).__name__}: {e}"
            except _cf.TimeoutError:
                # Recorded, not just logged. These sources were asked and did not
                # answer in time, which is neither "had nothing" nor "failed" —
                # and dropping them silently was the same vanishing act as an
                # unreported abstention.
                for fut, sid in futures.items():
                    if sid in results or sid in failed:
                        continue
                    # `as_completed` stops yielding at the timeout, so a source
                    # that did finish may simply not have been read yet. Keep
                    # that work rather than calling it a timeout.
                    if fut.done() and not fut.cancelled():
                        try:
                            _record(*fut.result())
                            continue
                        except Exception as e:
                            log.warning("Source %s result error: %s", sid, e)
                            failed[sid] = f"{type(e).__name__}: {e}"
                            continue
                    timed_out.append(sid)
                log.warning(
                    "jeles.search wall-clock limit %.1fs reached — %d source(s) timed out",
                    wall_clock_limit,
                    len(timed_out),
                )
        finally:
            # Never `wait=True`: the whole point is that this call does not hold
            # the caller past its wall clock. `cancel_futures` reclaims the ones
            # still queued; the ones already running cannot be cancelled, but
            # they are this pool's problem now and not the next call's.
            pool.shutdown(wait=False, cancel_futures=True)

    total = sum(len(v) for v in results.values())
    if total:
        _write_cache(query, results)

    return {
        "query": query,
        "sources_queried": queried,
        "unknown": unknown,
        "skipped": skipped,
        "failed": failed,
        "timed_out": timed_out,
        "total": total,
        "results": results,
        "note": NO_WIKIPEDIA_NOTE,
    }
