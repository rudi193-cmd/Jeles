"""`SOURCES[...]["hosts"]` must keep matching the code it describes.

The registry gained a `hosts` field so a consumer can ask "which hostnames does
this fleet actually query?" as *data* rather than by re-deriving it. A declared
list that nothing checks is just a second copy waiting to drift — and drift here
is silent in the direction that matters: a source repointed at a new API host
keeps working, while every consumer's idea of what jeles talks to quietly goes
stale.

So this reads the module's own AST and compares both directions. It is a
superset rule rather than a call-site trace: any http(s) literal in a source's
body must be declared, whether or not this file can prove a request is made
from it. Tracing exactly which literals reach `_get`/`_fetch` breaks on
ordinary control flow — `search_thesportsdb` builds its endpoints in a
list-of-tuples and unpacks them in a `for`, which a naive tracer misses
entirely — and a rule that silently under-reports is worse than one that asks
for an explicit exemption.

The one exemption is `NAMESPACE_URI_HOSTS`: XML/RDF namespace identifiers look
like URLs and are never contacted. willow-mcp's trusted-domain list had picked
up `www.w3.org` from arXiv's Atom namespace and treated it as an institution.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from urllib.parse import urlparse

from jeles import sources

_SRC = Path(sources.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)
_URL_RE = re.compile(r"https?://[^\s\"'<>{}\\]+")
_FUNCS = {n.name: n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)}


def _literal_hosts(fn: ast.FunctionDef) -> set[str]:
    """Hosts in every http(s) literal in this function, f-string prefixes included."""
    hosts: set[str] = set()
    for sub in ast.walk(fn):
        text = ""
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            text = sub.value
        elif isinstance(sub, ast.JoinedStr):
            for value in sub.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    text += value.value
                else:
                    break  # stop at the first interpolation
        for match in _URL_RE.findall(text):
            host = (urlparse(match).netloc or "").lower()
            if host and "{" not in host and "%" not in host:
                hosts.add(host)
    return hosts


def test_the_host_scan_catches_every_planted_literal_shape():
    """Planted: a function carrying every shape the scan claims to read — a
    plain literal, an f-string whose host sits in the constant prefix, and an
    http (not https) URL — plus an f-string whose host is *interpolated*, which
    the scan must not report as a host it cannot name. A scan that lost the
    f-string prefix would clear most of `sources.py`, whose endpoints are
    written `f"https://host/path?q={q}"`."""
    fn = ast.parse(
        "def search_planted(q):\n"
        "    a = 'https://plain.example.org/api'\n"
        "    b = f'https://fmt.example.org/search?q={q}'\n"
        "    c = 'http://insecure.example.org/x'\n"
        "    d = f'https://{host}/search'\n"
        "    return a, b, c, d\n"
    ).body[0]
    assert _literal_hosts(fn) == {
        "plain.example.org",
        "fmt.example.org",
        "insecure.example.org",
    }


def test_every_source_declares_the_hosts_its_code_mentions():
    """Undeclared host -> a consumer's picture of jeles' egress is incomplete."""
    undeclared = {}
    for sid, cfg in sources.SOURCES.items():
        declared = set(cfg.get("hosts", ()))
        found = _literal_hosts(_FUNCS[f"search_{sid}"]) - sources.NAMESPACE_URI_HOSTS
        missing = found - declared
        if missing:
            undeclared[sid] = sorted(missing)
    assert not undeclared, (
        f"these sources contact hosts they do not declare: {undeclared}. Add "
        f"them to SOURCES[...]['hosts'], or to NAMESPACE_URI_HOSTS if the URL "
        f"is a namespace identifier that is never fetched."
    )


def test_no_source_declares_a_host_its_code_never_mentions():
    """The other direction. A phantom entry is how a list starts lying — it
    survives the source being repointed or removed, and reads as evidence."""
    phantom = {}
    for sid, cfg in sources.SOURCES.items():
        declared = set(cfg.get("hosts", ()))
        extra = declared - _literal_hosts(_FUNCS[f"search_{sid}"])
        if extra:
            phantom[sid] = sorted(extra)
    assert not phantom, f"declared but absent from the code: {phantom}"


def test_every_registered_source_declares_at_least_one_host():
    """A source that contacts nothing is either dead or not a source."""
    empty = [sid for sid, cfg in sources.SOURCES.items() if not cfg.get("hosts")]
    assert empty == [], empty


def test_registered_hosts_is_the_union_and_respects_opt_in():
    every = sources.registered_hosts()
    assert every == {h for cfg in sources.SOURCES.values() for h in cfg.get("hosts", ())}

    # wikipedia is the opt-in source; excluding it must drop its host and
    # nothing else. Pinned because the default fan-out is the set that a
    # consumer asking "what does jeles reach by default?" actually means.
    default_only = sources.registered_hosts(include_opt_in=False)
    assert default_only <= every
    assert "en.wikipedia.org" in every
    assert "en.wikipedia.org" not in default_only


def test_namespace_hosts_are_not_presented_as_sources():
    """`www.w3.org` reached willow-mcp's institutional trust list this way."""
    assert "www.w3.org" not in sources.registered_hosts()
    assert "purl.org" not in sources.registered_hosts()
    # ...and they really are still in the code, so the exemption is load-bearing
    # rather than a leftover.
    everything = set()
    for sid in sources.SOURCES:
        everything |= _literal_hosts(_FUNCS[f"search_{sid}"])
    assert everything >= sources.NAMESPACE_URI_HOSTS, (
        "NAMESPACE_URI_HOSTS names a host no source mentions any more — drop it "
        "rather than carrying an exemption for nothing"
    )


def test_list_sources_exposes_hosts():
    """The registry's public read surface has to carry it, or a consumer is
    back to importing SOURCES directly."""
    listed = {entry["id"]: entry for entry in sources.list_sources()}
    assert listed["arxiv"]["hosts"] == sources.SOURCES["arxiv"]["hosts"]
    assert all(isinstance(entry["hosts"], list) for entry in listed.values())


def test_no_source_reaches_for_plain_http():
    """The sources lane is `_egress.HTTPS_ONLY`, so an `http://` literal in a
    source function is a request that cannot be made — it fails at the guard,
    at runtime, as a transport error rather than as anything legible.

    This exists because two of the four functions recovered from the archived
    jeles-remote fork (`isfdb`, `omdb`) were plain http there, and that was the
    single thing that stopped them being pasted straight in. Nothing checked it;
    the fork had no such lane and no such test, which is part of how they sat
    unregistered and unnoticed. A `http://` added here now fails at review.
    """
    import ast

    # XML namespace URIs are identifiers, not addresses — nothing dereferences
    # them, and they are `http://` because the specs that minted them are.
    # Exempted at URI level rather than host level (unlike
    # `NAMESPACE_URI_HOSTS`) because `www.loc.gov` is *also* a host this package
    # really fetches, so exempting the host would blind the check to a genuine
    # plain-http call to the Library of Congress.
    namespace_uris = {
        "http://www.w3.org/2005/Atom",
        "http://purl.org/dc/elements/1.1/",
        "http://purl.org/dc/terms/",
        "http://www.loc.gov/zing/srw/",
    }

    offenders = {}
    for name, fn in _FUNCS.items():
        bad = sorted(
            {
                m.group(0)
                for node in ast.walk(fn)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                for m in [_URL_RE.match(node.value)]
                if m and m.group(0).startswith("http://") and m.group(0) not in namespace_uris
            }
        )
        if bad:
            offenders[name] = bad
    assert not offenders, (
        f"these source functions use plain http on an https-only lane: {offenders}"
    )


def test_the_sources_recovered_from_the_archived_fork_are_reachable():
    """They were written in jeles-remote's vendored copy and never registered
    there, so `search()` could not dispatch them — dead code in a repo that is
    now archived and private. Being *in* this package is not the point; being
    reachable is, which is exactly what they were not before.

    `isfdb` and `omdb` are opt-in on purpose: both were plain http upstream and
    their TLS is unverified, so they stay out of the default fan-out until
    someone confirms it. That is a deliberate state, not an oversight, so it is
    pinned here rather than left to be "tidied up" later.
    """
    recovered = {"fbi_vault", "ig_nobel", "isfdb", "omdb"}
    assert recovered <= set(sources.SOURCES), "a recovered source lost its registry entry"
    for sid in recovered:
        assert callable(getattr(sources, f"search_{sid}")), f"search_{sid} is gone"
        assert sources.SOURCES[sid]["hosts"], f"{sid} declares no hosts"

    unverified_tls = {"isfdb", "omdb"}
    for sid in unverified_tls:
        assert sources.SOURCES[sid].get("opt_in") is True, (
            f"{sid} was plain http in the fork; it stays opt-in until its TLS is confirmed"
        )
    for sid in recovered - unverified_tls:
        assert not sources.SOURCES[sid].get("opt_in"), (
            f"{sid} was already https upstream — no reason to hold it back"
        )
