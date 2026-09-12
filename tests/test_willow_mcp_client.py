"""willow_mcp_client: launch resolution and the fire-and-forget gap forward.

No real willow-mcp process is spun up here and nothing touches the network —
these tests exercise the pure resolution logic, confirm forward_gap() is safe
(non-blocking, never raises) when willow-mcp isn't installed, and drive a fake
stdio session through its whole life (start, die, reconnect) by substituting
the two `mcp` modules _lifecycle imports lazily.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import threading
import time
import types

import pytest

from jeles import willow_mcp_client as wmc


def _wait_until(predicate, timeout=3.0):
    """Poll a background-thread condition. Returns whether it came true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def _reset_client_state():
    """ensure_started/forward_gap mutate module-level session state — reset
    it around every test so retry/cooldown behavior isn't order-dependent."""

    def _clear():
        # Retire any loop a test left running before dropping the reference,
        # otherwise the next test inherits a live thread holding an fd.
        loop = wmc._mcp_loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):  # already closing
                loop.call_soon_threadsafe(loop.stop)
            _wait_until(lambda: loop.is_closed(), timeout=2.0)
        wmc._mcp_session = None
        wmc._mcp_loop = None
        wmc._mcp_stop_event = None
        wmc._mcp_ready = False
        wmc._mcp_error = None
        wmc._last_attempt_at = None
        wmc._last_forward_error = None
        wmc._forward_ok = 0
        wmc._forward_failed = 0

    _clear()
    yield
    _clear()


@pytest.fixture
def no_willow_mcp(monkeypatch):
    """Make every _launch() probe report "willow-mcp is not installed".

    _launch() resolves a launcher from three independent probes — the
    WILLOW_MCP_CMD override, a `willow-mcp` console script on PATH, and an
    importable `willow_mcp` package — and ANY ONE of them succeeding produces
    a launcher. So a test that stubs only `shutil.which` is not asserting
    "willow-mcp is unavailable"; it is asserting "the console script is
    absent" and leaving the third probe to answer from whatever the venv
    happens to contain.

    That gap is not hypothetical, and it is not an unlucky venv either: the
    dependency edge runs willow-mcp -> jeles, so every environment where
    willow-mcp is installed necessarily has jeles installed beside it. An
    environment with both is the *normal* deployment of this module, and
    there `import willow_mcp` succeeds, `_launch()` returns
    (sys.executable, ["-m", "willow_mcp"]), and these tests spawn a real
    willow-mcp subprocess instead of exercising the unavailable path.

    CI never notices because it installs `jeles[dev,mcp]` and nothing else —
    willow-mcp is deliberately not a dependency of this package — so the
    third probe fails there by accident of the install set rather than by
    anything the tests do. This fixture makes it fail on purpose.

    `None` in sys.modules is the import system's own "this import must fail"
    sentinel: it raises ImportError before consulting any finder, so no
    willow_mcp code is imported and nothing on disk is touched.
    """
    monkeypatch.delenv("WILLOW_MCP_CMD", raising=False)
    monkeypatch.setattr(wmc.shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "willow_mcp", None)


class _FakeSession:
    """A willow-mcp session that initializes, serves calls, and can be killed.

    `kill()` emulates what actually happens when the child process dies: the
    task group running the session cancels the body of the `async with`, and
    the cancellation surfaces at its exit as an ordinary exception.
    """

    def __init__(self, read, write, control):
        self.control = control
        self.alive = True
        self.calls = []
        self.task = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is asyncio.CancelledError and not self.alive:
            raise RuntimeError("willow-mcp stdio child exited") from None
        return False

    async def initialize(self):
        self.task = asyncio.current_task()
        self.control.sessions.append(self)

    async def call_tool(self, name, payload):
        if not self.alive:
            raise BrokenPipeError("write to a closed willow-mcp pipe")
        self.calls.append((name, payload))
        return types.SimpleNamespace(isError=False, content=[])


class _FakeWillow:
    def __init__(self):
        self.sessions = []

    def kill(self):
        """Drop the live session the way a crashed/upgraded willow-mcp would."""
        loop, session = wmc._mcp_loop, self.sessions[-1]
        session.alive = False
        loop.call_soon_threadsafe(session.task.cancel)
        assert _wait_until(lambda: not wmc._mcp_ready), "session never came down"


@pytest.fixture
def fake_willow(monkeypatch):
    """Stand in for the `mcp` SDK so _lifecycle runs for real, offline."""
    control = _FakeWillow()

    class _FakeStdio:
        def __init__(self, params):
            self.params = params

        async def __aenter__(self):
            return ("read", "write")

        async def __aexit__(self, *exc):
            return False

    mcp_mod = types.ModuleType("mcp")
    mcp_mod.ClientSession = lambda read, write: _FakeSession(read, write, control)
    stdio_mod = types.ModuleType("mcp.client.stdio")
    stdio_mod.stdio_client = _FakeStdio
    stdio_mod.StdioServerParameters = lambda command, args, env: (command, args, env)
    client_mod = types.ModuleType("mcp.client")
    client_mod.stdio = stdio_mod

    monkeypatch.setitem(sys.modules, "mcp", mcp_mod)
    monkeypatch.setitem(sys.modules, "mcp.client", client_mod)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio_mod)
    monkeypatch.setattr(wmc, "_launch", lambda: ("/nonexistent/willow-mcp", []))
    monkeypatch.delenv("ASK_JELES_USE_WILLOW_MCP", raising=False)
    return control


def test_use_willow_mcp_defaults_on(monkeypatch):
    monkeypatch.delenv("ASK_JELES_USE_WILLOW_MCP", raising=False)
    assert wmc._use_willow_mcp() is True


def test_use_willow_mcp_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ASK_JELES_USE_WILLOW_MCP", "0")
    assert wmc._use_willow_mcp() is False


def test_launch_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("WILLOW_MCP_CMD", "/custom/venv/bin/python3 -m willow_mcp --serve")
    assert wmc._launch() == ("/custom/venv/bin/python3", ["-m", "willow_mcp", "--serve"])


def test_launch_falls_back_to_path_binary(monkeypatch):
    monkeypatch.delenv("WILLOW_MCP_CMD", raising=False)
    monkeypatch.setattr(
        wmc.shutil,
        "which",
        lambda name: "/usr/local/bin/willow-mcp" if name == "willow-mcp" else None,
    )
    assert wmc._launch() == ("/usr/local/bin/willow-mcp", [])


def test_launch_falls_back_to_python_m_when_only_the_package_is_importable(monkeypatch):
    """The third probe, pinned down rather than left to the ambient venv.

    No console script on PATH but `willow_mcp` importable is the shape a
    `pip install willow-mcp` into a venv whose bin/ is not on PATH produces —
    and, because willow-mcp depends on jeles, it is the shape a willow-mcp
    host itself runs in. Running it as `-m` against *this* interpreter is the
    point: it is the same environment the package was resolved from.
    """
    monkeypatch.delenv("WILLOW_MCP_CMD", raising=False)
    monkeypatch.setattr(wmc.shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "willow_mcp", types.ModuleType("willow_mcp"))
    assert wmc._launch() == (sys.executable, ["-m", "willow_mcp"])


def test_launch_returns_none_when_nothing_available(no_willow_mcp):
    # All three probes are stubbed to "absent" by the fixture — including the
    # `import willow_mcp` fallback, which this test used to leave to chance.
    assert wmc._launch() is None


def test_forward_gap_disabled_is_a_true_noop(monkeypatch):
    monkeypatch.setenv("ASK_JELES_USE_WILLOW_MCP", "0")
    before = time.monotonic()
    wmc.forward_gap("What is the accent color in Nord?")
    assert time.monotonic() - before < 0.05  # no thread spawned at all


def test_forward_gap_does_not_raise_or_block_when_unavailable(monkeypatch, no_willow_mcp):
    monkeypatch.delenv("ASK_JELES_USE_WILLOW_MCP", raising=False)

    before = time.monotonic()
    wmc.forward_gap("What is the accent color in Nord?")
    elapsed = time.monotonic() - before

    # Fire-and-forget: returns near-instantly regardless of whether the
    # background thread has finished resolving "willow-mcp isn't installed".
    assert elapsed < 0.5


@pytest.fixture
def attempts(monkeypatch):
    """Record every session attempt ensure_started() actually spawns.

    Attempts are counted here rather than by watching _mcp_loop: a finished
    attempt now clears that global (and closes its loop), so loop identity is
    no longer a stable witness of "did we try again".
    """
    started = []
    real = wmc._run_session

    def _spy(loop, ready):
        started.append(loop)
        real(loop, ready)

    monkeypatch.setattr(wmc, "_run_session", _spy)
    return started


def test_ensure_started_retries_after_cooldown(monkeypatch, attempts, no_willow_mcp):
    monkeypatch.setattr(wmc, "RETRY_COOLDOWN", 0.05)

    assert wmc.ensure_started(timeout=1) is False
    assert len(attempts) == 1

    # A second call inside the cooldown window must NOT spawn a fresh
    # attempt — a failed connect shouldn't cost a new subprocess/thread on
    # every single forward_gap() call while willow-mcp is still down.
    assert wmc.ensure_started(timeout=1) is False
    assert len(attempts) == 1

    time.sleep(0.1)

    # Past the cooldown, a stale failure must be retried, not cached forever
    # — this is the actual bug being guarded against: "best effort" must not
    # silently become "one effort" for the rest of a long-running session.
    assert wmc.ensure_started(timeout=1) is False
    assert len(attempts) == 2


def test_healthy_session_starts_and_forwards(fake_willow):
    assert wmc.ensure_started(timeout=3) is True
    wmc.call_tool("gap_log", {"topic": "t", "question": "q"})
    assert fake_willow.sessions[-1].calls == [
        ("gap_log", {"app_id": wmc.APP_ID, "topic": "t", "question": "q"})
    ]


def test_dead_session_is_not_reported_ready(fake_willow):
    """The F1 regression: _mcp_ready was only ever set True, so a willow-mcp
    that crashed mid-session left ensure_started() answering True forever off
    its fast path, with _mcp_session bound to a session that could not work."""
    assert wmc.ensure_started(timeout=3) is True
    fake_willow.kill()

    assert wmc._mcp_ready is False
    assert wmc._mcp_session is None
    # Still inside the cooldown, so this reports "not available" rather than
    # immediately respawning — but it must not claim the dead session is fine.
    assert wmc.ensure_started(timeout=1) is False


def test_ensure_started_reconnects_after_a_session_dies(fake_willow, monkeypatch):
    monkeypatch.setattr(wmc, "RETRY_COOLDOWN", 0.0)
    assert wmc.ensure_started(timeout=3) is True
    dead = fake_willow.sessions[-1]
    fake_willow.kill()

    assert wmc.ensure_started(timeout=3) is True
    assert len(fake_willow.sessions) == 2
    assert wmc._mcp_session is not dead
    wmc.call_tool("gap_log", {"topic": "t", "question": "q"})
    assert len(fake_willow.sessions[-1].calls) == 1


def test_forward_gap_after_a_death_never_raises_into_the_caller(fake_willow, monkeypatch):
    monkeypatch.setattr(wmc, "RETRY_COOLDOWN", 1000.0)  # no reconnect available
    assert wmc.ensure_started(timeout=3) is True
    fake_willow.kill()

    # call_tool() must raise (there is no session) and forward_gap() must
    # swallow it: the local gap write is the source of truth, this is a bonus.
    with pytest.raises(RuntimeError):
        wmc.call_tool("gap_log", {"topic": "t", "question": "q"})

    before = time.monotonic()
    wmc.forward_gap("What is the accent color in Nord?")
    assert time.monotonic() - before < 0.5


def test_retries_do_not_accumulate_event_loops(monkeypatch, attempts, no_willow_mcp):
    """The F2 regression: each retry allocated a fresh event loop and dropped
    the previous one without close(), holding its epoll fd and self-pipe pair
    (measured at ~3 fds per retry, on a 30s cooldown)."""
    monkeypatch.setattr(wmc, "RETRY_COOLDOWN", 0.0)

    fd_dir = "/proc/self/fd"
    countable = os.path.isdir(fd_dir)
    wmc.ensure_started(timeout=1)  # warm up thread/import machinery first
    before = len(os.listdir(fd_dir)) if countable else 0

    for _ in range(25):
        wmc._last_attempt_at = None
        assert wmc.ensure_started(timeout=1) is False

    assert len(attempts) == 26
    assert _wait_until(lambda: all(loop.is_closed() for loop in attempts)), (
        "a retry left its event loop open"
    )
    if countable:
        # Allow a little slack for unrelated churn; the bug was +3/retry.
        assert len(os.listdir(fd_dir)) - before < 10


def test_shutdown_is_safe_after_the_session_ended(fake_willow):
    assert wmc.ensure_started(timeout=3) is True
    fake_willow.kill()
    wmc.shutdown()  # must not raise on a loop that is already closed


# ── forward_status: failures are recorded, not discarded ─────────────────────
#
# forward_gap() swallowed every failure into a DEBUG line, so the most common
# one in practice — willow-mcp's gate denying the call because `ask-jeles` has
# no manifest, or the seat has no gap_write — was indistinguishable from a
# successful forward at every surface a caller could reach. It stays
# non-raising; it no longer stays silent.


def test_forward_status_records_a_failed_forward(no_willow_mcp):
    wmc.forward_gap("Which seat is this host forwarding as?")
    assert _wait_until(lambda: wmc.last_forward_error() is not None), (
        "a failed forward left no trace"
    )

    status = wmc.forward_status()
    assert status["failed"] == 1
    assert status["forwarded"] == 0
    assert status["last_error"]
    assert status["app_id"] == wmc.APP_ID


def test_forward_status_records_a_successful_forward(monkeypatch):
    calls = []
    monkeypatch.setattr(wmc, "call_tool", lambda name, inputs, **kw: calls.append((name, inputs)))

    wmc.forward_gap("Did this one land?", topic="t")
    assert _wait_until(lambda: wmc.forward_status()["forwarded"] == 1)
    assert wmc.last_forward_error() is None
    assert calls == [("gap_log", {"topic": "t", "question": "Did this one land?"})]


def test_a_recovered_forward_clears_the_previous_error(monkeypatch):
    outcomes = iter([RuntimeError("gate denied: no manifest for 'ask-jeles'"), None])

    def flaky(name, inputs, **kw):
        exc = next(outcomes)
        if exc:
            raise exc

    monkeypatch.setattr(wmc, "call_tool", flaky)

    wmc.forward_gap("first")
    assert _wait_until(lambda: wmc.last_forward_error() is not None)
    assert "no manifest" in wmc.last_forward_error()

    wmc.forward_gap("second")
    assert _wait_until(lambda: wmc.forward_status()["forwarded"] == 1)
    assert wmc.last_forward_error() is None, (
        "a later success must not leave a stale error on the status"
    )


def test_forward_gap_still_never_raises_into_its_caller(no_willow_mcp):
    """The whole point of the module: a host asking a question must not fail
    because a fleet backlog is unreachable. Recording the failure must not
    have changed that."""
    before = time.monotonic()
    wmc.forward_gap("Does recording a failure make this blocking?")
    assert time.monotonic() - before < 0.5
    assert _wait_until(lambda: wmc.forward_status()["failed"] == 1)


def test_repeated_identical_failures_warn_once(no_willow_mcp, caplog):
    """A willow-mcp that is down for an hour must not write one WARNING per
    question asked — but a *changed* failure is news and warns again."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="jeles.willow_mcp"):
        wmc._record_forward("gate denied")
        wmc._record_forward("gate denied")
        wmc._record_forward("connection refused")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "gate denied" in warnings[0].message
    assert "connection refused" in warnings[1].message


# ── an in-band {"error": ...} is a failure, not a payload ───────────────────
#
# willow-mcp reports a gate denial as a SUCCESSFUL MCP call whose payload is
# {"error": "gate denied: ..."} — isError is not set. Checking isError alone
# therefore counted the single most common forwarding failure as a forward that
# landed. Found by a cross-repo seam check reporting "no error reported" beside
# a gate denial in the server's own log.


class _Result:
    """Minimal stand-in for an MCP CallToolResult."""

    def __init__(self, text, is_error=False):
        self.isError = is_error
        self.content = [types.SimpleNamespace(text=text)]


def test_gate_denial_in_the_payload_raises():
    denial = (
        "gate denied: 'jeles' not permitted for 'gap_log'. Ensure a manifest "
        "exists at $WILLOW_HOME/mcp_apps/jeles/manifest.json"
    )
    with pytest.raises(RuntimeError, match="not permitted for 'gap_log'"):
        wmc._parse_tool_payload(_Result(json.dumps({"error": denial})))


def test_transport_level_is_error_still_raises():
    with pytest.raises(RuntimeError, match="boom"):
        wmc._parse_tool_payload(_Result("boom", is_error=True))


def test_a_normal_payload_is_returned_unchanged():
    ok = {"id": "abc123", "status": "open", "asked_count": 1}
    assert wmc._parse_tool_payload(_Result(json.dumps(ok))) == ok


def test_an_empty_error_is_not_a_failure():
    """Only a truthy `error` means refused. `{"error": ""}` is a tool that
    answered, and a payload is not a failure for carrying the key."""
    payload = {"error": "", "id": "abc123"}
    assert wmc._parse_tool_payload(_Result(json.dumps(payload))) == payload


def test_a_string_payload_mentioning_error_is_not_a_failure():
    assert wmc._parse_tool_payload(_Result("no error occurred")) == "no error occurred"


def test_a_denied_forward_reaches_forward_status(monkeypatch):
    """The whole point: the denial has to arrive where an operator can see it,
    through the real parse path rather than a stubbed call_tool."""
    denial = "gate denied: 'ask-jeles' not permitted for 'gap_log'"

    class _Session:
        async def call_tool(self, name, payload):
            return _Result(json.dumps({"error": denial}))

    monkeypatch.setattr(wmc, "ensure_started", lambda *a, **k: True)
    monkeypatch.setattr(wmc, "_mcp_session", _Session())
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    monkeypatch.setattr(wmc, "_mcp_loop", loop)
    try:
        wmc.forward_gap("what does the fleet not know?")
        assert _wait_until(lambda: wmc.last_forward_error() is not None), (
            "a gate denial was recorded as a successful forward"
        )
        status = wmc.forward_status()
        assert status["failed"] == 1
        assert status["forwarded"] == 0
        assert "not permitted" in status["last_error"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
