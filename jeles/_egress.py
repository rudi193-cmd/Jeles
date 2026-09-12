"""_egress — the one definition of "how this package opens a URL".

Three modules do raw egress: `sources` (the institutional fan-out),
`reactions.search_adapter` (the open-web hop) and `institutional` (the remote
delegate). Each grew its own scheme check, and each got the same two things
wrong, because a rule written out three times is a rule enforced nowhere:

* The check ran once, on the URL the caller built. urllib follows 3xx *inside*
  `urlopen`, so a redirect was never inspected. Reproduced against a listening
  socket with `build_opener`'s default handler set: a 302 to `ftp://` landed a
  TCP connection on the target.
* The docstring beside it described a policy the code did not implement.

So the guard lives here, once, and the difference between call sites is one
argument: which schemes that site allows. Everything else — the redirect hop,
the handler set, the body cap — is identical by construction.

**Stdlib only, and no state at import.** Openers are built on first use, because
`ProxyHandler()` snapshots the proxy environment when it is constructed and
this package promises nothing happens at load. `jeles.corpus` must never import
this (see `tests/test_import_purity.py`); nothing here is needed for storage.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any

#: What `sources` allows: TLS or nothing.
HTTPS_ONLY = frozenset({"https"})
#: What the two operator-pointed lanes allow. Plain http stays reachable there
#: because both are aimed at an address the operator chose — a SearXNG instance
#: on `http://127.0.0.1:8888` is the documented zero-config default, and a
#: `JELES_REMOTE_URL` on a private network is a legitimate deployment. Refusing
#: it would break the sovereign, self-hosted case this package is built around.
#: It is a real trade: on those two lanes a hostile redirect can still downgrade
#: https -> http. Narrow the set at the call site if a deployment can afford to.
HTTP_OR_HTTPS = frozenset({"http", "https"})


def scheme_ok(url: str, allowed: Iterable[str]) -> bool:
    return urllib.parse.urlsplit(url).scheme.lower() in set(allowed)


#: Hostnames that name the local machine without being IP literals.
_LOCAL_NAMES = frozenset({"localhost", "localhost.", "localhost.localdomain", "ip6-localhost"})


def _without_port(host: str) -> str:
    if host.startswith("["):
        return host.partition("]")[0][1:]
    head, sep, tail = host.rpartition(":")
    return head if sep and tail.isdigit() else host


def _as_address(host: str) -> str | None:
    """The host read as a literal address, or None if it is a name.

    `inet_aton` is here because `ip_address` is stricter than every resolver:
    it rejects `2130706433`, `0177.0.0.1`, `0x7f.0.0.1` and `127.1`, all of
    which `getaddrinfo` — and therefore the socket — happily reads as
    `127.0.0.1`. Doing that arithmetic locally rather than leaning on the
    resolver is what keeps those forms refused on the two paths where no DNS
    lookup happens: behind a proxy, and offline.
    """
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except OSError:
        return None


def _dialled_hosts(url: str) -> list[str] | None:
    """Every host string this URL could end up dialling, lowercased.

    Two parsers in the stdlib disagree, and the gap between them was a working
    bypass. This guard reads `urlsplit(url).hostname`, which does **not**
    percent-decode. The dialler reads `Request(url).host`, which
    `urllib.request.Request._parse` runs through `unquote` before handing to
    `HTTPSConnection` — so `https://127.0.0%2e1:8888/` was the opaque *name*
    `127.0.0%2e1` to the check and the address `127.0.0.1` to the socket.
    Eleven variants got through that way, including
    `https://169.254.169%2e254/latest/meta-data/`, from a `Location:` header a
    hostile upstream fully controls. A single character was enough:
    `12%37.0.0.1`.

    So both views are collected and every one of them has to be acceptable.
    Checking only the dialler's view is not sufficient either — it keeps the
    userinfo, so `https://arxiv.org@127.0.0.1/` reads there as one long
    hostname rather than as loopback. It is the union that holds.

    `None` means neither parser could say — a different answer from `[]`, which
    means both agree there is no host.
    """
    seen: list[str] = []
    parsed = False

    def add(host: str | None) -> None:
        if not host:
            return
        # `.rstrip(".")` — a trailing dot is a fully-qualified name and every
        # resolver strips it, but `inet_aton` rejects `127.0.0.1.`, so the
        # literal took the resolver path and was waved through wherever no
        # resolution happens. Behind a proxy that meant urllib emitted
        # `CONNECT 169.254.169.254.:443` and the proxy resolved it — breaking
        # this module's own stated invariant that a literal is refused either
        # way. willow-mcp's copy had the rstrip; this one did not.
        h = host.strip().strip("[]").rstrip(".").lower()
        if h and h not in seen:
            seen.append(h)

    for view in (_split_host, _request_host):
        try:
            add(view(url))
        except ValueError:
            continue
        parsed = True
    return seen if parsed else None


def _split_host(url: str) -> str | None:
    return urllib.parse.urlsplit(url).hostname


def _request_host(url: str) -> str:
    # Userinfo stripped as `urlsplit` strips it, port removed, percent-escapes
    # already decoded by `Request._parse`.
    return _without_port((urllib.request.Request(url).host or "").rpartition("@")[2])


def _proxy_dials_for(url: str) -> bool:
    """Whether urllib hands this URL to a proxy rather than dialling it itself.

    It matters because a proxied request never resolves the destination here:
    the TCP peer is the proxy, and the hostname travels to it in a CONNECT
    line. Measured on a real request through this environment's HTTPS_PROXY,
    `getaddrinfo` was called for the proxy and *never* for `api.crossref.org`.

    Resolving anyway is wrong in both directions. Under split-horizon DNS a
    legitimate source resolves privately for us and would be refused — in this
    very environment `api.crossref.org` answers `10.4.2.9`, so the guard would
    have broken the sources lane outright.
    """
    try:
        split = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if not urllib.request.getproxies().get(split.scheme.lower()):
        return False
    try:
        return not urllib.request.proxy_bypass(split.hostname or "")
    except (OSError, ValueError):
        return True


def private_destination(url: str) -> str | None:
    """Why this URL points somewhere only the host itself can reach, or None.

    A scheme guard says *how* a request travels, never *where*. Nothing here
    used to say where, so a redirect from a source could reach any https
    address — including `169.254.169.254`, the cloud metadata endpoint, and
    `127.0.0.1:8888`, the SearXNG instance this package documents as its
    zero-config search backend. (Not `corpus_server`, which an earlier draft of
    this paragraph claimed: that one is stdio-only and listens on no port.)
    The initial URLs are hardcoded public APIs, so reaching those needs a
    hostile or
    compromised upstream, an open redirect, or DNS pointing a public name at a
    private address. Several sources are third-party conveniences rather than
    hardened institutions, which is enough to make that worth closing.

    Hostnames are resolved, not just pattern-matched: `evil.example` with an A
    record of `127.0.0.1` is the obvious way past a name-only check, and
    willow-mcp's own fetch guard — which inspects the literal host and stops —
    has exactly that hole. Alternate literal encodings (`2130706433`,
    `0177.0.0.1`, `127.1`) need no special case: `ip_address` rejects them, so
    they take the resolver path, and the resolver returns `127.0.0.1`.

    The classifier is the explicit list *plus* `not is_global`, and both halves
    earn their place. `is_global` alone would allow IPv4 and IPv6 multicast and
    the NAT64 well-known prefix — and `64:ff9b::a9fe:a9fe` reaches
    `169.254.169.254` on a NAT64 network. The list alone allowed all of
    100.64.0.0/10, RFC6598 shared address space, which is exactly what cloud
    and ISP internal networks are numbered from.

    **Two residuals, stated rather than papered over.**

    Resolving here and connecting afterwards is two lookups, so a name that
    answers public now and private a moment later still gets through. Closing
    that needs the connection pinned to the address checked, which urllib does
    not expose. It raises the cost from "set a DNS record" to "win a race".

    Behind a proxy the name is not resolved here at all (see
    `_proxy_dials_for`), so a name only the proxy can resolve to a private
    address is not caught. That is the proxy's ACL to enforce — it is the one
    party that knows where the request actually goes. Literal addresses are
    still refused, proxy or not, because the proxy will CONNECT to whatever it
    is named.

    A name that does not resolve is allowed through — the connection is about to
    fail on its own, and refusing here would report the wrong reason.
    """
    hosts = _dialled_hosts(url)
    if hosts is None:
        # `https://[%3a%3a1]/x`: urlsplit rejects the bracketed netloc and
        # Request raises on the same string. Neither view can say where this
        # goes, and "nobody could tell" is not permission. Refusing here also
        # makes it a stated decision rather than a bare ValueError escaping
        # from inside a parser, which is not what callers catch.
        return "the host cannot be parsed, so where it goes cannot be checked"
    for host in hosts:
        if host in _LOCAL_NAMES:
            return f"{host!r} names the local machine"

    resolve = not _proxy_dials_for(url)
    for host in hosts:
        literal = _as_address(host)
        if literal is not None:
            candidates, how = [literal], "written as"
        elif resolve:
            how = "resolved from"
            try:
                candidates = [info[4][0] for info in socket.getaddrinfo(host, None)]
            except (OSError, UnicodeError):
                # UnicodeError, not just OSError: `a..b`, an over-long label, or
                # a non-IDNA-encodable name makes getaddrinfo raise from the
                # idna codec, which is not an OSError. Uncaught it escaped
                # `redirect_request` as a bare codec error rather than a
                # refusal — from a `Location:` header the far end controls.
                continue
        else:
            continue
        for raw in candidates:
            try:
                addr = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_reserved
                or addr.is_multicast
                or addr.is_unspecified
                or not addr.is_global
            ):
                via = "" if raw == host else f" ({how} {host!r})"
                return f"{raw} is not a public address{via}"
    return None


class SchemeGuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-applies the scheme *and* destination checks on every redirect hop.

    stdlib's own filter lives in `http_error_302` and permits http, https *and
    ftp*; `file:` and `data:` it already rejects. So ftp is the hop it lets
    through, and — for an https-only caller — so is a silent https -> http
    downgrade. Both are closed here.

    The destination check is the one that was missing entirely. A first URL is
    chosen by this package; a redirect target is chosen by whatever answered.
    """

    def __init__(self, allowed: frozenset[str], allow_private: bool = False) -> None:
        super().__init__()
        self.allowed = allowed
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # `newurl` is resolved against the request URL upstream, so a relative
        # Location arrives here absolute and carries its scheme.
        if not scheme_ok(newurl, self.allowed):
            raise urllib.error.HTTPError(
                newurl,
                code,
                f"refusing redirect to a scheme outside {sorted(self.allowed)}: {newurl[:60]!r}",
                headers,
                fp,
            )
        if not self.allow_private:
            reason = private_destination(newurl)
            if reason is not None:
                raise urllib.error.HTTPError(
                    newurl,
                    code,
                    f"refusing redirect to a private destination — {reason}: {newurl[:60]!r}",
                    headers,
                    fp,
                )
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        return self._strip_credentials_across_hosts(req, new)

    #: Headers that authenticate the caller rather than describe the body.
    #: stdlib strips only `content-length`/`content-type`, so everything here
    #: rode along to whatever answered the redirect.
    CREDENTIAL_HEADERS = frozenset(
        {
            "authorization",
            "proxy-authorization",
            "cookie",
            "x-jeles-secret",  # institutional._post_remote's shared secret
            "x-subscription-token",  # Brave
            "x-api-key",
            "api-key",
        }
    )

    @classmethod
    def _strip_credentials_across_hosts(cls, old, new):
        """Drop authenticating headers when a redirect changes host.

        urllib carries every non-content header to the redirect target. Measured
        before this: a 302 from a `JELES_REMOTE_URL` to `http://attacker.example`
        produced a request still holding `X-Jeles-Secret: <the shared secret>` —
        cross-host, over plaintext, and on the one lane that opts out of the
        destination check. The Brave key leaks the same way.

        requests does this in `Session.rebuild_auth`; urllib has no equivalent,
        so it has to happen here. Same host, same credential — only a change of
        host drops it.
        """
        if new is None:
            return new

        def host_of(u):
            try:
                return (urllib.parse.urlsplit(u).hostname or "").rstrip(".").lower()
            except ValueError:
                return None

        old_host, new_host = host_of(old.full_url), host_of(new.full_url)
        # `None` means unparseable on either side: treat as a change, not as a
        # match, so an unreadable URL does not inherit the credential.
        if old_host is not None and old_host == new_host:
            return new
        for name in list(new.headers):
            if name.lower() in cls.CREDENTIAL_HEADERS:
                del new.headers[name]
        for name in list(getattr(new, "unredirected_hdrs", {})):
            if name.lower() in cls.CREDENTIAL_HEADERS:
                del new.unredirected_hdrs[name]
        return new


_OPENERS: dict[tuple[frozenset[str], bool], urllib.request.OpenerDirector] = {}
_OPENER_LOCK = threading.Lock()


def opener(
    allowed: frozenset[str], *, allow_private: bool = False
) -> urllib.request.OpenerDirector:
    """The shared opener for one scheme policy, built on first use.

    Assembled by hand rather than with `build_opener`, which installs handlers
    for file:, ftp: and data:. With no handler for a scheme, a URL that somehow
    got past both checks has nothing able to open it — `UnknownHandler` raises
    `unknown url type: ...`. `HTTPHandler` is included only when plain http is
    allowed, so an https-only caller gets that structural backstop too.
    (Verified that omitting it leaves https-through-a-proxy unchanged: that
    path tunnels via `HTTPSHandler` and `ProxyHandler`.)
    """
    # The destination policy is part of the cache key, not just the scheme set.
    # Sharing one opener between a lane that may reach localhost and one that
    # may not would hand the stricter lane the looser lane's redirect handler.
    key = (allowed, allow_private)
    with _OPENER_LOCK:
        if key not in _OPENERS:
            handlers: list[Any] = [urllib.request.ProxyHandler()]  # honors HTTPS_PROXY
            if "http" in allowed:
                handlers.append(urllib.request.HTTPHandler())
            handlers += [
                urllib.request.HTTPSHandler(),
                SchemeGuardedRedirects(allowed, allow_private),
                urllib.request.HTTPDefaultErrorHandler(),
                urllib.request.HTTPErrorProcessor(),
                urllib.request.UnknownHandler(),
            ]
            o = urllib.request.OpenerDirector()
            for h in handlers:
                o.add_handler(h)
            _OPENERS[key] = o
        return _OPENERS[key]


def check_url(url: str, allowed: frozenset[str], *, allow_private: bool = False) -> None:
    """Raise unless this URL may be opened. The pre-flight half of `urlopen`.

    Factored out because `sources` composes its own opener rather than calling
    `urlopen` (it needs `_opener` to stay a substitutable name on that module,
    and it adds a breadcrumb on transport failure). It had therefore
    re-implemented the scheme check and simply not had the destination one —
    so on the single lane this whole guard exists for, the first-URL half of it
    did not run. Both callers now share these lines rather than agreeing to.
    """
    if not scheme_ok(url, allowed):
        raise ValueError(f"refusing URL scheme outside {sorted(allowed)}: {url[:60]!r}")
    if allow_private:
        return
    reason = private_destination(url)
    if reason is not None:
        raise ValueError(f"refusing a private destination — {reason}: {url[:60]!r}")


def urlopen(
    req: urllib.request.Request,
    *,
    allowed: frozenset[str],
    timeout: float,
    allow_private: bool = False,
):
    """Open a request, refusing a disallowed scheme on the first URL and on
    every redirect hop.

    Known gap, not fixed: stdlib drains the *redirect* response with an
    uncapped `fp.read()` before following it, so a body cap bounds the final
    response only. A 3xx with an endless body is still an endless body.
    """
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    check_url(url, allowed, allow_private=allow_private)
    return opener(allowed, allow_private=allow_private).open(req, timeout=timeout)


def read_capped(resp: Any, max_bytes: int) -> bytes:
    """Read a bounded body. Every response is untrusted input, and an endpoint
    that starts streaming without end should fail, not exhaust memory."""
    raw = resp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"response exceeds {max_bytes} bytes — refusing")
    return raw


def fetch(
    url: str,
    *,
    allowed: frozenset[str],
    timeout: float,
    max_bytes: int,
    headers: dict | None = None,
    data: bytes | None = None,
    allow_private: bool = False,
) -> bytes:
    """Open and read in one call, so no caller ever holds a response it could
    read unbounded. This is the only shape that makes the cap structural rather
    than remembered — six of eight egress sites in `sources` had skipped it."""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urlopen(req, allowed=allowed, timeout=timeout, allow_private=allow_private) as resp:
        return read_capped(resp, max_bytes)
