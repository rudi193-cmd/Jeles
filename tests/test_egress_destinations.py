"""A scheme guard says *how* a request travels. Nothing said *where*.

`_egress` re-checked the scheme on every redirect hop, which closed an
https -> http downgrade and an ftp hop. It said nothing about the destination,
so a redirect from a source could reach **any** https address — including
`169.254.169.254`, the cloud metadata endpoint, and `127.0.0.1:8888`, where the
SearXNG instance this package documents as its zero-config search backend
listens.

Reaching that needs a redirect from one of the ~60 upstreams: a hostile or
compromised one, an open redirect, or DNS pointing a public name at a private
address. Not all of them are hardened institutions — `gutendex.com`,
`thesportsdb.com`, `frankfurter.app` and `open-meteo.com` are third-party
conveniences.

It became worth closing when willow-mcp 2.2.0 made `jeles.institutional`
reachable as an MCP tool, on the same permission line as `willow_web_fetch` —
which blocks exactly these addresses. Two tools, one grant, and the newer one
was the weaker path.

**The split follows the one already there.** `sources` is https-only and has no
legitimate private destination. The two operator-pointed lanes — a SearXNG
instance on `http://127.0.0.1:8888`, a `JELES_REMOTE_URL` on a private network —
are aimed at an address the operator chose, and refusing private there would
break the sovereign case this package is built around. So the default is
"public only" and those two opt out, visibly.

**On the shape of these tests.** The first version of this file pinned the
policy with IP literals and with `"allow_private=True" in source_text`. An
audit killed it: deleting the DNS resolution entirely — reproducing verbatim
the willow-mcp hole this module's docstring claims to improve on — left every
redirect test green, and the call-site assertions were satisfied by *comments*
containing that string and defeated by passing the flag positionally. So the
posture assertions below run the lanes and observe what they pass, and the
resolution property is pinned on the path that actually uses it.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from typing import ClassVar

import pytest
from conftest import real_opener

from jeles import _egress

_PRIVATE = [
    ("cloud metadata", "https://169.254.169.254/latest/meta-data/iam/"),
    ("IPv4 loopback", "https://127.0.0.1:8888/admin"),
    ("IPv6 loopback", "https://[::1]/x"),
    ("localhost by name", "https://localhost:5432/"),
    ("RFC1918", "https://10.0.0.5/internal"),
    # 192.0.0.0/24 is IETF Protocol Assignments, which Python classifies as
    # is_private. An earlier revision labelled this "carrier-grade NAT" — wrong
    # block, and the mislabel hid that real CGNAT was not covered at all. It is
    # its own case below.
    ("IETF protocol assignments", "https://192.0.0.1/"),
    ("carrier-grade NAT (RFC6598)", "https://100.64.0.1/"),
    ("IPv4 multicast", "https://224.0.0.1/"),
    # 64:ff9b::/96 wraps an IPv4 address for translation. On a NAT64 network
    # this one reaches 169.254.169.254 — and `is_global` calls it global.
    ("NAT64-wrapped metadata", "https://[64:ff9b::a9fe:a9fe]/latest/"),
]

#: Every one of these reached a socket before the percent-decoding fix, driven
#: from a `Location:` header a hostile upstream controls. The guard read
#: `urlsplit().hostname`, which does not decode; `Request._parse` runs the host
#: through `unquote` before handing it to the connection. One escaped character
#: was enough.
_ENCODED = [
    ("one dot escaped", "https://127.0.0%2e1:8888/admin"),
    ("all dots escaped", "https://127%2e0%2e0%2e1:8888/admin"),
    ("fully escaped literal", "https://%31%32%37%2e%30%2e%30%2e%31:8888/admin"),
    ("metadata, one dot", "https://169.254.169%2e254/latest/meta-data/iam/"),
    ("one digit escaped", "https://12%37.0.0.1/x"),
    ("escaped name", "https://loc%61lhost:8888/admin"),
    ("escaped brackets", "https://%5b::1%5d:8888/admin"),
    ("escaped decimal literal", "https://%32%31%33%30%37%30%36%34%33%33/x"),
    ("userinfo plus escape", "https://arxiv.org@127.0.0%2e1/x"),
]

#: Forms `ipaddress.ip_address` rejects and every resolver accepts. They must be
#: refused without a DNS lookup, because there are two paths where none happens:
#: behind a proxy, and offline.
_ALTERNATE_LITERALS = [
    ("decimal", "https://2130706433/x"),
    ("octal", "https://0177.0.0.1/x"),
    ("hex", "https://0x7f.0.0.1/x"),
    ("short form", "https://127.1/x"),
    ("zero padded", "https://127.000.000.001/x"),
]


@pytest.fixture
def unproxied(monkeypatch):
    """Force the direct-dial path, where the guard resolves names itself.

    Not ambient: `_proxy_dials_for` reads the environment, so without this the
    same test asserts different things on a developer's laptop and in a
    container with HTTPS_PROXY set — and the resolution tests below would pass
    vacuously in the second.
    """
    monkeypatch.setattr(_egress.urllib.request, "getproxies", lambda: {})


@pytest.fixture
def proxied(monkeypatch):
    """Force the proxied path, where the destination is not the TCP peer."""
    monkeypatch.setattr(
        _egress.urllib.request,
        "getproxies",
        lambda: {"http": "http://proxy:8080", "https": "http://proxy:8080"},
    )
    monkeypatch.setattr(_egress.urllib.request, "proxy_bypass", lambda host: False)


@pytest.fixture(autouse=True)
def _no_live_dns(monkeypatch, request):
    """Nothing in this file may reach a real resolver.

    `test_a_source_redirect_to_a_public_address_still_works` used to resolve
    `arxiv.org` for real; offline, `private_destination` short-circuits on
    OSError and allows, so that test degraded into a tautology in exactly the
    environment where it most needed to mean something.
    """
    if "live_dns" in request.keywords:
        return
    table = {"arxiv.org": "151.101.3.42", "api.crossref.org": "13.222.48.122"}

    def fake(host, port, *a, **k):
        if host in table:
            return [(2, 1, 6, "", (table[host], port or 0))]
        raise OSError(f"unstubbed lookup of {host!r}")

    monkeypatch.setattr(_egress.socket, "getaddrinfo", fake)


class _Req:
    """The minimum urllib's redirect handler touches."""

    full_url = "https://api.crossref.org/works?query=x"
    headers: ClassVar[dict] = {}
    unredirected_hdrs: ClassVar[dict] = {}
    timeout = None
    origin_req_host = "api.crossref.org"
    unverifiable = False
    data = None

    def get_full_url(self):
        return self.full_url

    def get_method(self):
        return "GET"


def _redirect(handler, url, req=None):
    return handler.redirect_request(req or _Req(), io.BytesIO(b""), 302, "Found", {}, url)


def _lower(req):
    """urllib's `Request.add_header` capitalizes keys — `X-Api-Key` becomes
    `X-api-key`. Asserting `"X-Api-Key" not in headers` therefore passes whether
    or not anything was stripped, which is a vacuous test wearing the shape of a
    real one. Compare on normalised keys."""
    return {k.lower(): v for k, v in req.headers.items()}


def _req(url, **headers):
    """A request with its own header dict — `_Req.headers` is a ClassVar, so
    mutating it would leak into every other test in the file."""
    r = _Req()
    r.full_url = url
    r.headers = dict(headers)
    r.unredirected_hdrs = {}
    return r


@pytest.mark.parametrize(("label", "url"), _PRIVATE, ids=[p[0] for p in _PRIVATE])
def test_a_source_redirect_cannot_reach_a_private_address(label, url):
    """The hole, closed. Every one of these was followed before."""
    handler = _egress.SchemeGuardedRedirects(_egress.HTTPS_ONLY)
    with pytest.raises(urllib.error.HTTPError, match="private destination"):
        _redirect(handler, url)


@pytest.mark.parametrize(("label", "url"), _ENCODED, ids=[p[0] for p in _ENCODED])
def test_a_percent_encoded_host_cannot_smuggle_a_private_address(label, url):
    """Parser-vs-connector disagreement, which was a working bypass.

    The guard inspected `urlsplit().hostname` and the socket got
    `Request(url).host` — and only the second is percent-decoded. All nine of
    these were ALLOWED by the guard and dialled the private address; the
    metadata one is the live vector, needing nothing but a `Location:` header
    from one of ~60 upstreams.
    """
    handler = _egress.SchemeGuardedRedirects(_egress.HTTPS_ONLY)
    with pytest.raises(urllib.error.HTTPError, match="private destination"):
        _redirect(handler, url)


@pytest.mark.parametrize(
    ("label", "url"), _ALTERNATE_LITERALS, ids=[p[0] for p in _ALTERNATE_LITERALS]
)
def test_alternate_literal_encodings_are_refused_without_a_lookup(label, url, proxied):
    """`inet_aton` reads these; `ip_address` does not.

    Run on the proxied path *deliberately* — that is the path with no DNS at
    all, so if these were relying on the resolver to normalise them, this
    fails. The autouse fixture would raise on any lookup anyway.
    """
    assert _egress.private_destination(url) is not None


def test_a_source_redirect_to_a_public_address_still_works(unproxied):
    """The other half. A guard that refuses everything is not a guard."""
    handler = _egress.SchemeGuardedRedirects(_egress.HTTPS_ONLY)
    assert _redirect(handler, "https://arxiv.org/abs/1234") is not None


@pytest.mark.parametrize(("label", "url"), _PRIVATE, ids=[p[0] for p in _PRIVATE])
def test_the_operator_pointed_lanes_may_still_reach_private_addresses(label, url):
    """`allow_private=True` is not a loophole, it is the point of those lanes.
    The documented zero-config default is SearXNG on `http://127.0.0.1:8888`;
    refusing it would break the out-of-the-box case rather than protect it."""
    handler = _egress.SchemeGuardedRedirects(_egress.HTTP_OR_HTTPS, allow_private=True)
    assert _redirect(handler, url) is not None


def test_the_scheme_guard_still_holds_alongside_the_destination_one():
    """Adding a second check must not displace the first — an https -> http
    downgrade on the sources lane is still refused, and for its own reason."""
    handler = _egress.SchemeGuardedRedirects(_egress.HTTPS_ONLY)
    with pytest.raises(urllib.error.HTTPError, match="scheme outside"):
        _redirect(handler, "http://arxiv.org/abs/1")


def test_a_name_that_resolves_private_is_caught(monkeypatch, unproxied):
    """A name-only check is the obvious thing to write and is trivially bypassed
    by pointing a public name at 127.0.0.1. willow-mcp's own fetch guard
    inspects the literal host and stops, so it has exactly that hole."""
    monkeypatch.setattr(
        _egress.socket, "getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))]
    )
    reason = _egress.private_destination("https://totally-legit.example/x")
    assert reason is not None
    assert "127.0.0.1" in reason
    assert "totally-legit.example" in reason, "should say the name it resolved from"


def test_the_resolving_check_runs_on_the_redirect_path_not_only_standalone(monkeypatch, unproxied):
    """The property above, pinned where it is actually used.

    Every entry in `_PRIVATE` is a literal, so deleting the resolution step —
    reproducing willow-mcp's hole exactly — left all of them green. The claim
    that this guard is the stronger of the two was tested by one direct call to
    `private_destination` and by nothing on the redirect path.
    """
    monkeypatch.setattr(
        _egress.socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("169.254.169.254", 0))],
    )
    handler = _egress.SchemeGuardedRedirects(_egress.HTTPS_ONLY)
    with pytest.raises(urllib.error.HTTPError, match=r"169\.254\.169\.254"):
        _redirect(handler, "https://looks-fine.example/x")


def test_a_name_that_does_not_resolve_is_not_refused(monkeypatch, unproxied):
    """The connection is about to fail on its own. Refusing here would report a
    security decision for what is really a DNS failure."""

    def boom(*a, **k):
        raise OSError("Name or service not known")

    monkeypatch.setattr(_egress.socket, "getaddrinfo", boom)
    assert _egress.private_destination("https://nx.invalid/x") is None


def test_a_host_neither_parser_can_read_is_refused():
    """`urlsplit` raises on this netloc and so does `Request`. Neither view can
    say where it goes, and "nobody could tell" is not permission. It also keeps
    a bare ValueError from escaping a parser, which is not what callers catch.
    """
    assert _egress.private_destination("https://[%3a%3a1]/x") is not None


def test_the_opener_cache_does_not_share_across_destination_policies():
    """The cache key is (schemes, allow_private). Keyed on schemes alone, the
    first lane to build an opener would hand its redirect handler to the other —
    and a sources call could inherit the operator lane's permission to reach
    localhost."""
    # `real_opener`, not `_egress.opener`: conftest's autouse fixture swaps the
    # latter for a delegating shim, which would make both sides the same object
    # and the assertion vacuous rather than true.
    strict = real_opener(_egress.HTTP_OR_HTTPS, allow_private=False)
    loose = real_opener(_egress.HTTP_OR_HTTPS, allow_private=True)
    assert strict is not loose

    def guard_of(o):
        return next(h for h in o.handlers if isinstance(h, _egress.SchemeGuardedRedirects))

    assert guard_of(strict).allow_private is False
    assert guard_of(loose).allow_private is True


def test_the_first_url_is_checked_too_not_only_redirects():
    """A redirect is the realistic vector, but a caller passing a private URL
    directly should not be the one hole left open."""
    req = urllib.request.Request("https://127.0.0.1:9/x")
    with pytest.raises(ValueError, match="private destination"):
        _egress.urlopen(req, allowed=_egress.HTTPS_ONLY, timeout=1)


def test_the_sources_lane_checks_its_first_url_as_well():
    """`sources` composes its own opener rather than calling `_egress.urlopen`,
    and had restated the scheme check inline without the destination one. So
    the test above passed while the single lane this guard exists for connected
    to `127.0.0.1` — the check was real only for a caller sources is not."""
    from jeles import sources

    with pytest.raises(ValueError, match="private destination"):
        sources._urlopen(urllib.request.Request("https://127.0.0.1:9/x"))


# --- what each lane actually passes, observed rather than grepped -------------
#
# The previous version of these asserted on the text of the modules:
# `"allow_private" not in sources` and `"allow_private=True" in institutional`.
# Both are defeated: the second is satisfied by the string appearing in a
# *comment* (it does, at both call sites), and the first by writing
# `SchemeGuardedRedirects(_ALLOWED_SCHEMES, True)` positionally — which passed
# the whole 320-test suite while opting the sources lane out of the guard.


def test_the_sources_lane_takes_the_secure_default(monkeypatch):
    """Both of the module's two constructions, because the positional-argument
    mutant lived in the second one and `_opener` alone would not have seen it.
    """
    from jeles import sources

    assert sources._SchemeGuardedRedirects().allow_private is False

    seen = {}
    monkeypatch.setattr(_egress, "opener", lambda allowed, **kw: seen.update(kw) or object())
    sources._opener()
    assert seen.get("allow_private", False) is False


def test_the_remote_delegate_lane_opts_out(monkeypatch):
    """`institutional`'s opt-out had no coverage at all — removing it left the
    suite entirely green. JELES_REMOTE_URL on a private network is the
    sovereign deployment this package exists for."""
    from jeles import institutional

    seen = {}
    monkeypatch.setattr(_egress, "fetch", lambda url, **kw: seen.update(kw) or b"{}")
    institutional._post_remote("https://box.internal", {"q": "x"}, "s3cret")
    assert seen["allow_private"] is True


def test_the_open_web_lane_no_longer_opts_out_for_every_backend(monkeypatch):
    """This test used to assert that `_get_json` always passed
    `allow_private=True`, which is the defect an audit later found: that
    function is also the fetch path for Brave, Tavily and DuckDuckGo. The
    per-backend behaviour it should have been pinning is asserted at the bottom
    of this file; here we only hold the line that the shared helper takes the
    secure default."""
    from jeles.reactions import search_adapter

    seen = {}
    monkeypatch.setattr(_egress, "fetch", lambda url, **kw: seen.update(kw) or b"{}")
    search_adapter._get_json("https://api.duckduckgo.com/?q=x")
    assert seen["allow_private"] is False


def test_fetch_forwards_the_destination_policy(monkeypatch):
    """`fetch` threading `allow_private` through to `urlopen` is the whole
    mechanism of the two opt-outs above, and dropping it passed every test in
    this file."""
    seen = {}

    class _Resp:
        def __enter__(self):
            return io.BytesIO(b"{}")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(_egress, "urlopen", lambda req, **kw: seen.update(kw) or _Resp())
    _egress.fetch(
        "https://arxiv.org/x",
        allowed=_egress.HTTPS_ONLY,
        timeout=1,
        max_bytes=10,
        allow_private=True,
    )
    assert seen["allow_private"] is True


# --- the proxy case ----------------------------------------------------------


def test_behind_a_proxy_a_name_is_not_resolved_here(proxied, monkeypatch):
    """Measured on a real proxied request: `getaddrinfo` was called for the
    proxy and never for the destination. The TCP peer is the proxy and the
    hostname travels to it in a CONNECT line, so resolving here answers a
    question nothing asked — and answers it wrong in both directions.

    The direction that matters is the false refusal: under split-horizon DNS a
    legitimate source resolves privately for us, and refusing on that would
    take the sources lane down outright.
    """
    monkeypatch.setattr(
        _egress.socket,
        "getaddrinfo",
        lambda *a, **k: pytest.fail("resolved a name on the proxied path"),
    )
    assert _egress.private_destination("https://api.crossref.org/works") is None


def test_behind_a_proxy_a_literal_private_address_is_still_refused(proxied):
    """The half that stays meaningful: the proxy will CONNECT to whatever it is
    named, so naming `169.254.169.254` is still a request to reach it."""
    assert _egress.private_destination("https://169.254.169.254/latest/") is not None


# --- found by audit after 0.5.1 shipped ---------------------------------------


def test_a_trailing_dot_does_not_launder_a_literal_address(proxied):
    """`127.0.0.1.` is the same host to every resolver, but `inet_aton` rejects
    it — so the literal took the resolver path and was waved through wherever
    no resolution happens. Behind a proxy urllib emitted
    `CONNECT 169.254.169.254.:443` and the proxy resolved it, breaking this
    module's stated invariant that a literal is refused proxy or not.
    Run on the proxied path deliberately: that is where it was reachable."""
    for url in (
        "https://127.0.0.1./x",
        "https://169.254.169.254./latest/",
        "https://127.0.0.1%2e/x",
    ):
        assert _egress.private_destination(url) is not None, url


def test_a_credential_header_is_dropped_when_a_redirect_changes_host():
    """urllib carries every non-content header to the redirect target. A 302
    from a JELES_REMOTE_URL to an attacker produced a request still holding
    `X-Jeles-Secret` — cross-host, over plaintext, on the one lane that opts
    out of the destination check. requests does this in `rebuild_auth`;
    urllib has no equivalent."""
    handler = _egress.SchemeGuardedRedirects(_egress.HTTP_OR_HTTPS, allow_private=True)
    req = _req(
        "https://remote.operator.example/search",
        **{"X-Jeles-Secret": "SECRET", "X-Subscription-Token": "BRAVE", "User-Agent": "jeles"},
    )
    out = _redirect(handler, "http://attacker.example/collect", req)
    got = _lower(out)
    assert "x-jeles-secret" not in got
    assert "x-subscription-token" not in got
    assert got.get("user-agent") == "jeles", "only credentials go"


def test_a_credential_header_survives_a_same_host_redirect():
    """The other half. Stripping unconditionally would break every ordinary
    redirect an authenticated API performs on itself."""
    handler = _egress.SchemeGuardedRedirects(_egress.HTTP_OR_HTTPS, allow_private=True)
    req = _req("https://api.crossref.org/works", **{"X-Api-Key": "K", "User-Agent": "jeles"})
    out = _redirect(handler, "https://api.crossref.org/works/v2", req)
    assert _lower(out).get("x-api-key") == "K"


def test_a_name_that_breaks_the_idna_codec_is_refused_not_raised(unproxied):
    """`getaddrinfo` raises UnicodeError — not OSError — on an empty internal
    label or an over-long one, so `except OSError` did not catch it and a bare
    codec error escaped `redirect_request`, from a Location the far end
    controls. It must come back as a refusal or an allow, never as a raise."""
    for host in ("a..b", "x" * 64 + ".com"):
        assert _egress.private_destination(f"https://{host}/x") is None


def test_the_open_web_lane_only_opts_out_for_the_operators_own_backend(monkeypatch):
    """`_get_json` hardcoded `allow_private=True`, justified by the SearXNG
    default — but it is also the fetch path for Brave, Tavily and DuckDuckGo,
    three hardcoded public APIs with no claim to a private address. One
    operator-chosen backend was buying every other backend an exemption."""
    from jeles.reactions import search_adapter

    seen: list[bool] = []
    monkeypatch.setattr(
        _egress, "fetch", lambda url, **kw: seen.append(kw["allow_private"]) or b"{}"
    )

    monkeypatch.setenv("JELES_SEARXNG_URL", "http://127.0.0.1:8888")
    search_adapter._searxng("q")
    assert seen == [True], "the operator's own address stays reachable"

    seen.clear()
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    search_adapter._brave("q")
    search_adapter._ddg_fetch("q")
    assert seen == [False, False], "public APIs get the secure default"
