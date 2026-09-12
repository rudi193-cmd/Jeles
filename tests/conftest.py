"""Shared test seam for the egress guard.

Every network-touching test in this suite stubs `urllib.request.urlopen`. Since
the scheme guard moved onto a shared `OpenerDirector` — which is what lets it
run on redirect hops, not just the first URL — those stubs are no longer on the
path: `opener.open()` dispatches to its own handler chain and never calls
`urlopen`. Without this fixture the stubs would be silently bypassed and the
tests would reach the real network.

So: point `_egress.opener` at a delegate that forwards to whatever `urlopen`
currently is. Autouse, because "this test stubs urlopen" is true of most of the
file and forgetting it fails as a live request rather than as an assertion.

`_egress.real_opener` is captured here before anything can patch it, for the
handful of tests that need to introspect the genuine handler chain.
"""

from __future__ import annotations

import pytest

from jeles import _egress

#: The unpatched builder, for tests that assert on the real handler chain.
real_opener = _egress.opener


@pytest.fixture(autouse=True)
def _egress_opener_delegates_to_urlopen(monkeypatch):
    import urllib.request

    class _Delegating:
        @staticmethod
        def open(req, timeout=None):
            return urllib.request.urlopen(req, timeout=timeout)

    # Accepts `allow_private` because the real `opener` takes it — a shim with
    # a narrower signature turns a real call into a TypeError that the caller
    # swallows, and every hit disappears with only a warning to say why.
    monkeypatch.setattr(_egress, "opener", lambda allowed, *, allow_private=False: _Delegating)
