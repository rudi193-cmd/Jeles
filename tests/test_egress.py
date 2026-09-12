"""_egress — the one definition of how this package opens a URL.

Three modules did raw egress and each had written its own scheme check. All
three had the same two bugs, which is the argument for this module existing:
the check ran once on the first URL while urllib follows 3xx *inside*
`urlopen`, and the comment beside it described a policy the code did not
implement.

These tests use the real opener (`conftest.real_opener`), not the delegating
seam every other test file gets, because the handler chain *is* the subject.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest
from conftest import real_opener

from jeles import _egress

# ── The scheme check ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url, https_only, http_or_https",
    [
        ("https://example.org/x", True, True),
        ("HTTPS://example.org/x", True, True),  # scheme is case-insensitive
        ("http://example.org/x", False, True),  # the one difference between lanes
        ("ftp://example.org/x", False, False),
        ("file:///etc/passwd", False, False),
        ("data:text/plain,hello", False, False),
        ("gopher://example.org/x", False, False),
        ("", False, False),
    ],
)
def test_the_scheme_check_matches_each_lanes_policy(url, https_only, http_or_https):
    assert _egress.scheme_ok(url, _egress.HTTPS_ONLY) is https_only
    assert _egress.scheme_ok(url, _egress.HTTP_OR_HTTPS) is http_or_https


def test_urlopen_refuses_a_disallowed_scheme_before_opening():
    """Fail-closed, and name the policy in the message — a bare "refused" sends
    the reader to the source to find out which set was in force."""
    with pytest.raises(ValueError, match=r"scheme outside \['https'\]"):
        _egress.urlopen(
            urllib.request.Request("http://example.org/x"), allowed=_egress.HTTPS_ONLY, timeout=1
        )


@pytest.mark.parametrize(
    "url, kept",
    [
        (
            "https://api.example.org/v2/search?api_key=planted-key&q=x",
            "https://api.example.org/v2/search",
        ),
        (
            "https://api.example.org/search.json?wskey=planted-key#frag",
            "https://api.example.org/search.json",
        ),
        (
            "https://user:planted-pw@api.example.org:8443/p?k=planted-key",
            "https://api.example.org:8443/p",
        ),
        ("http://127.0.0.1:8888/search", "http://127.0.0.1:8888/search"),
        ("file:///etc/passwd", "file:///etc/passwd"),
    ],
)
def test_loggable_keeps_the_destination_and_drops_the_secrets(url, kept):
    """What the failure paths quote: scheme, host and path. Query, fragment and
    userinfo are where sources and operators put credentials, so none of them
    survive — every string above named `planted` is the proof."""
    out = _egress.loggable(url)
    assert out == kept
    assert "planted" not in out


def test_a_refusal_message_quotes_the_url_without_its_query_string():
    """The refusal used to quote `url[:60]`, which for a key at offset 53 is a
    prefix of the key. The message still names the destination, so the reader
    knows what was refused, and nothing after the `?`."""
    url = "http://api.example.org/v2/search.json?api_key=planted-key&q=x"
    with pytest.raises(ValueError, match=r"scheme outside \['https'\]") as info:
        _egress.check_url(url, _egress.HTTPS_ONLY)
    assert "http://api.example.org/v2/search.json" in str(info.value)
    assert "planted" not in str(info.value)
    assert "api_key" not in str(info.value)


# ── The redirect hop, which is what all three modules were missing ──────────


@pytest.mark.parametrize(
    "allowed, newurl, refused",
    [
        (_egress.HTTPS_ONLY, "https://example.org/b", False),
        # stdlib's own filter permits http, https *and* ftp. ftp is the hop it lets
        # through; for an https-only caller so is a silent downgrade to http.
        (_egress.HTTPS_ONLY, "http://example.org/b", True),
        (_egress.HTTPS_ONLY, "ftp://evil.example/x", True),
        (_egress.HTTPS_ONLY, "file:///etc/passwd", True),
        (_egress.HTTP_OR_HTTPS, "http://example.org/b", False),
        (_egress.HTTP_OR_HTTPS, "ftp://evil.example/x", True),
    ],
)
def test_the_scheme_is_rechecked_on_every_redirect_hop(allowed, newurl, refused):
    handler = _egress.SchemeGuardedRedirects(allowed)
    args = (
        urllib.request.Request("https://example.org/a"),
        io.BytesIO(b""),
        302,
        "Found",
        {},
        newurl,
    )
    if refused:
        with pytest.raises(urllib.error.HTTPError, match="refusing redirect"):
            handler.redirect_request(*args)
    else:
        assert handler.redirect_request(*args).full_url == newurl


def test_a_live_redirect_to_ftp_does_not_reach_the_target():
    """The end-to-end version, because the unit test above only proves the
    handler refuses when called — not that urllib actually calls it.

    Before the guard, with `build_opener`'s default handler set, this landed a
    TCP connection on the target socket. That is the whole finding.
    """
    import http.server
    import socket
    import threading
    import time

    target = socket.socket()
    target.bind(("127.0.0.1", 0))
    target.listen(1)
    arrived = []
    threading.Thread(target=lambda: (target.accept(), arrived.append(True)), daemon=True).start()
    dest = f"ftp://127.0.0.1:{target.getsockname()[1]}/x"

    class _Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", dest)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # http_or_https, so the local redirector is reachable and only the
        # *hop* is under test — the https-only lane would stop this one URL
        # earlier and prove nothing about redirects.
        opener = real_opener(_egress.HTTP_OR_HTTPS)
        url = f"http://127.0.0.1:{srv.server_port}/a"
        with pytest.raises(urllib.error.HTTPError, match="refusing redirect"):
            opener.open(urllib.request.Request(url), timeout=5)
        time.sleep(0.4)
        assert not arrived, "the guard let a connection through to an ftp target"
    finally:
        srv.shutdown()
        target.close()


# ── The handler chain ───────────────────────────────────────────────────────


def test_a_scheme_with_no_transport_cannot_be_opened_at_all():
    """Belt and braces: even bypassing both checks, nothing in the chain can
    open file:, ftp: or data:, and on the https-only lane not http: either."""
    opener = real_opener(_egress.HTTPS_ONLY)
    for url in ("file:///etc/passwd", "ftp://example.org/x", "http://example.org/x"):
        with pytest.raises(urllib.error.URLError, match="unknown url type"):
            opener.open(urllib.request.Request(url), timeout=2)


def test_the_http_lane_installs_an_http_transport_and_the_https_lane_does_not():
    https_only = {type(h).__name__ for h in real_opener(_egress.HTTPS_ONLY).handlers}
    both = {type(h).__name__ for h in real_opener(_egress.HTTP_OR_HTTPS).handlers}

    assert "HTTPHandler" not in https_only
    assert "HTTPHandler" in both
    for names in (https_only, both):
        assert "SchemeGuardedRedirects" in names
        assert "HTTPRedirectHandler" not in names, (
            "the unguarded default must not also be installed"
        )
        assert not (names & {"FileHandler", "FTPHandler", "DataHandler"})


def test_each_policy_gets_its_own_cached_opener():
    assert real_opener(_egress.HTTPS_ONLY) is real_opener(_egress.HTTPS_ONLY)
    assert real_opener(_egress.HTTPS_ONLY) is not real_opener(_egress.HTTP_OR_HTTPS)


def test_no_opener_is_built_at_import():
    """`ProxyHandler()` snapshots the proxy environment when constructed, so
    building one at import would pin HTTPS_PROXY to whatever was set when
    `jeles` was first imported."""
    import subprocess
    import sys

    probe = "from jeles import _egress\nassert not _egress._OPENERS, 'opener built at import'\n"
    assert subprocess.run([sys.executable, "-c", probe]).returncode == 0


# ── The body cap ────────────────────────────────────────────────────────────


def test_an_oversized_body_is_refused():
    resp = io.BytesIO(b"z" * 2048)
    assert len(_egress.read_capped(io.BytesIO(b"z" * 100), 2048)) == 100
    with pytest.raises(ValueError, match="exceeds 1024 bytes"):
        _egress.read_capped(resp, 1024)


def test_fetch_opens_and_reads_in_one_call(monkeypatch):
    """The shape is the point: a caller that never holds the response cannot
    forget to cap it. Six of eight egress sites in `sources` had forgotten."""

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    seen = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        seen["data"] = req.data
        return _R(b"z" * 5000)

    monkeypatch.setattr(urllib.request, "urlopen", fake)

    assert (
        _egress.fetch(
            "https://example.org/x",
            allowed=_egress.HTTPS_ONLY,
            timeout=1,
            max_bytes=10_000,
            headers={"User-Agent": "ua"},
            data=b"body",
        )
        == b"z" * 5000
    )
    assert seen["url"] == "https://example.org/x"
    assert seen["data"] == b"body"
    assert {k.lower(): v for k, v in seen["headers"].items()}["user-agent"] == "ua"

    with pytest.raises(ValueError, match="exceeds"):
        _egress.fetch("https://example.org/x", allowed=_egress.HTTPS_ONLY, timeout=1, max_bytes=100)


# ── Every egress lane goes through here ─────────────────────────────────────


def test_no_module_opens_a_url_outside_this_one():
    """The reason this module exists. A fourth copy of the guard would be a
    fourth chance to get it wrong, so nothing outside `_egress` may call
    `urlopen` or `build_opener` — the test that would have caught the original
    three."""
    import ast
    import pathlib

    root = pathlib.Path(_egress.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "_egress.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in {"urlopen", "build_opener"}:
                offenders.append(f"{path.name}:{node.lineno} calls {name}()")
    assert not offenders, offenders
