"""Best-effort forwarder from Jeles' local corpus gaps to willow-mcp's
fleet-wide gap backlog (the gap_log/gap_list/gap_resolve/gap_promote tools).

A Jeles host stays fully functional offline: jeles/corpus.py's local gap log
(WILLOW_STORE_ROOT/<gaps collection>) is always written first and is the
source of truth for the host itself — that write is synchronous and never
depends on this module. forward_gap() only ever ADDS a copy into willow-mcp's
shared backlog when willow-mcp is installed, reachable, and this app_id is
authorized for gap_write there. It never blocks the caller and never raises
into it — a stalled or missing willow-mcp should be invisible to a user
asking Jeles a question.

willow-mcp is a separate, standalone, agent-neutral package — see
https://github.com/rudi193-cmd/willow-mcp — invoked here as an ordinary
external MCP server, the same way any generic MCP drawer would talk to any
other discovered server. This module has no hard dependency on it.

APP_ID / DEFAULT_TOPIC default to Ask Jeles' original values for back-compat
(the fleet backlog already keys gaps under `ask-jeles-corpus`) and can be
overridden via env for a differently-scoped host.

**The app_id has to exist on the other side.** willow-mcp authorizes every
tool call against `$WILLOW_HOME/mcp_apps/<app_id>/manifest.json`, and the
default `ask-jeles` is not one of the seats it seeds — so out of the box a
forward is denied with `no manifest for 'ask-jeles'`. On a fleet whose hub is
willow-mcp, set `JELES_CORPUS_APP_ID=jeles` to call as the librarian seat it
does seed (which carries `gap_write` as of willow-mcp 2.4). The topic is
independent of the seat: gaps still land under `ask-jeles-corpus` unless
`JELES_CORPUS_TOPIC` says otherwise. `forward_status()` reports which seat is
in use and why the last forward failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import sys
import threading
import time
from typing import Any

log = logging.getLogger("jeles.willow_mcp")

APP_ID = os.environ.get("JELES_CORPUS_APP_ID", "ask-jeles")
DEFAULT_TOPIC = os.environ.get("JELES_CORPUS_TOPIC", "ask-jeles-corpus")
RETRY_COOLDOWN = 30.0  # seconds before retrying a failed connection attempt

_mcp_session = None
_mcp_loop: asyncio.AbstractEventLoop | None = None
_mcp_stop_event: asyncio.Event | None = None
_last_attempt_at: float | None = None
_mcp_ready = False
_mcp_error: str | None = None
_start_lock = threading.Lock()

# Outcome of the most recent forward attempt. `_mcp_error` is about the
# *session* (willow-mcp missing, spawn failed, child died); these are about the
# *call* — the far more common failure being a gate denial, which is a live
# session refusing one tool. Kept separate because they need different fixes:
# one is "start willow-mcp", the other is "grant this app_id gap_write".
_forward_lock = threading.Lock()
_last_forward_error: str | None = None
_forward_ok = 0
_forward_failed = 0


def _use_willow_mcp() -> bool:
    return os.environ.get("ASK_JELES_USE_WILLOW_MCP", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _subprocess_env() -> dict[str, str]:
    """A minimal environment for the spawned willow-mcp subprocess.

    Forwarding the full parent environment leaks unrelated secrets (e.g. the
    conflict-scan search adapter's ``BRAVE_API_KEY`` / ``TAVILY_API_KEY``) into a
    ``PATH``/``WILLOW_MCP_CMD``-resolved binary this package does not control.
    Pass only what willow-mcp needs: PATH/HOME, locale, and ``WILLOW_*`` config.
    """
    keep = {"PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER", "LOGNAME"}
    return {k: v for k, v in os.environ.items() if k in keep or k.startswith(("LC_", "WILLOW_"))}


def _launch() -> tuple[str, list[str]] | None:
    """Resolve how to start willow-mcp, or None if it isn't available.

    Precedence: WILLOW_MCP_CMD (explicit override, shell-split) > a
    `willow-mcp` console script on PATH (the normal pip-installed case) >
    `python -m willow_mcp` against the current interpreter (installed into
    this same venv). No hardcoded personal paths — willow-mcp is a separate
    package that may or may not be installed anywhere in particular.
    """
    override = os.environ.get("WILLOW_MCP_CMD", "").strip()
    if override:
        parts = shlex.split(override)
        if parts:
            return parts[0], parts[1:]

    exe = shutil.which("willow-mcp")
    if exe:
        return exe, []

    try:
        import willow_mcp  # noqa: F401
    except ImportError:
        return None
    return sys.executable, ["-m", "willow_mcp"]


async def _lifecycle(ready: threading.Event) -> None:
    global _mcp_session, _mcp_stop_event, _mcp_ready, _mcp_error
    launch = _launch()
    if launch is None:
        _mcp_error = "willow-mcp not installed (set WILLOW_MCP_CMD, or `pip install willow-mcp`)"
        ready.set()
        return

    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as exc:
        _mcp_error = f"mcp package missing: {exc}"
        ready.set()
        return

    command, args = launch
    params = StdioServerParameters(command=command, args=args, env=_subprocess_env())
    stop = asyncio.Event()
    _mcp_stop_event = stop

    try:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            _mcp_session = session
            _mcp_ready = True
            ready.set()
            await stop.wait()
    except Exception as exc:
        _mcp_error = str(exc)
        log.debug("willow-mcp session failed: %s", exc)
        ready.set()


def _close_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Close a finished attempt's loop, cancelling anything still pending.

    Retries used to drop the previous loop on the floor without close(). An
    unclosed loop keeps its epoll fd and its self-pipe pair — measured at 2.97
    fds per retry (100 forced retries: +297 open fds, 100/100 loops still
    open). Against a willow-mcp that stays down that is ~360 fds/hour at the
    30s cooldown, i.e. a soft-limit breach within a day on a host whose only
    symptom is a module that is supposed to fail invisibly.

    Not a bare close(): a stalled attempt is retired with loop.stop() (see
    _abandon), which leaves its lifecycle task suspended, and a loop closed
    with pending tasks prints "Task was destroyed but it is pending!" to
    stderr. The wait is bounded because a task wedged in a non-cancellable
    await must not strand this thread — the fd is worth less than the thread.
    """
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=1.0))
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception as exc:
        log.debug("willow-mcp loop teardown: %s", exc)
    finally:
        loop.close()


def _abandon(loop: asyncio.AbstractEventLoop) -> None:
    """Retire a stale in-flight attempt so its own thread closes its loop.

    A retry displaces the previous attempt. Without this, an attempt still
    wedged before initialize (willow-mcp spawned but never answering) would go
    on running with nothing left to hand a session to, and its loop would never
    be closed — the fd leak in _close_loop, on exactly the attempts that never
    finish on their own. stop() makes run_until_complete return so _run_session
    reaches its teardown.
    """
    # RuntimeError == already closed: the attempt's own thread got there first.
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(loop.stop)


def _run_session(loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
    """Own one attempt end to end: run its loop, then tear the attempt down.

    Teardown lives here rather than in _lifecycle because it has to run on
    every exit path, including the ones _lifecycle cannot see: a BaseException
    (the CancelledError delivered when the willow-mcp child dies mid-await
    never reaches an `except Exception`), and a loop retired by _abandon.
    """
    global _mcp_session, _mcp_loop, _mcp_stop_event, _mcp_ready, _mcp_error
    try:
        loop.run_until_complete(_lifecycle(ready))
    except BaseException as exc:
        _mcp_error = str(exc) or exc.__class__.__name__
        log.debug("willow-mcp session thread ended: %r", exc)
    finally:
        # Unblock ensure_started() *before* taking _start_lock: it holds that
        # lock while waiting on `ready`, so grabbing the lock first would stall
        # this thread for the whole ensure_started timeout.
        ready.set()
        with _start_lock:
            # Only disown the globals if a later retry has not already claimed
            # them — a slow-dying attempt must not blank out its successor.
            if _mcp_loop is loop:
                _mcp_session = None
                _mcp_ready = False
                _mcp_stop_event = None
                _mcp_loop = None
        _close_loop(loop)


def ensure_started(timeout: float = 5) -> bool:
    """Lazy-start a background willow-mcp session. Short default timeout —
    this is a best-effort forward, not a feature anything blocks on.

    Retries after RETRY_COOLDOWN if there is no usable session. A session
    that never started (willow-mcp not running yet at host boot, a
    transient spawn failure, ...) must not permanently disable forwarding
    for the rest of a long-running session — that would make "best
    effort" mean "one effort."

    That covers a session that started and then *died* too, which is the
    case this retry was written for and long did not reach: _mcp_ready was
    only ever set True, so a willow-mcp that crashed or was upgraded
    mid-session left _mcp_session bound to a dead session, ensure_started
    returning True off the fast path, and every later forward timing out for
    the life of the process. _run_session now clears that state on every exit
    path, which is what makes the retry below reachable.
    """
    global _mcp_loop, _last_attempt_at
    if not _use_willow_mcp():
        return False
    if _mcp_ready and _mcp_session is not None:
        return True

    with _start_lock:
        if _mcp_ready and _mcp_session is not None:
            return True  # re-check: a concurrent attempt may have just landed
        now = time.monotonic()
        # _last_attempt_at, not _mcp_loop, is the "we have tried" marker.
        # _mcp_loop is cleared when an attempt ends, so keying the cooldown off
        # it would respawn willow-mcp on every single forward while it is down.
        cooled = _last_attempt_at is None or now - _last_attempt_at >= RETRY_COOLDOWN
        if not cooled:
            return False
        if _mcp_loop is not None:
            _abandon(_mcp_loop)  # a stale attempt still in flight
        loop = asyncio.new_event_loop()
        _mcp_loop = loop
        _last_attempt_at = now
        ready = threading.Event()
        threading.Thread(
            target=_run_session,
            args=(loop, ready),
            daemon=True,
            name="jeles-willow-mcp",
        ).start()
        if not ready.wait(timeout=timeout):
            return False
        return _mcp_ready


def _parse_tool_payload(result: Any) -> Any:
    """The tool's return value, or a raise if the call did not succeed.

    Two different things can mean failure, and only one of them looks like one:

    ``isError``          transport/protocol level — the tool blew up.
    ``{"error": ...}``   in-band — the call completed and the tool is telling
                         you it refused.

    willow-mcp reports a **gate denial** the second way. It is a successful MCP
    call carrying ``{"error": "gate denied: ... not permitted for 'gap_log'"}``,
    so checking ``isError`` alone treats the single most common forwarding
    failure as a forward that landed: ``forward_status()`` counted it under
    ``forwarded``, ``last_error`` stayed ``None``, and the gap was never
    written. That is the exact silence this module's status API exists to end,
    reappearing one layer down — found by a cross-repo seam check reporting
    "no error reported" beside a gate denial in the server's own log.
    """
    if getattr(result, "isError", False):
        parts = [getattr(c, "text", str(c)) for c in (getattr(result, "content", None) or [])]
        raise RuntimeError("; ".join(parts) or "willow-mcp tool error")
    payload = _decode_content(result)
    # Only a dict, and only a truthy `error`: willow-mcp's convention is that
    # this key is present exactly when the call did not do what was asked. A
    # string payload that happens to contain the word is not a failure, and
    # `{"error": ""}` is not one either.
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload


def _decode_content(result: Any) -> Any:
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        text = text.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        return text
    return {}


def call_tool(name: str, inputs: dict[str, Any], timeout: float = 10) -> Any:
    if not ensure_started():
        raise RuntimeError(_mcp_error or "willow-mcp unavailable")
    # Snapshot both globals together: the session thread clears them the moment
    # the willow-mcp child dies, which can land between ensure_started()
    # returning and the submit below. A raise here is the correct outcome —
    # forward_gap() catches it — but it must be a real exception, not the bare
    # `assert` that used to sit here (gone under `python -O`, and it read as a
    # guarantee that the state could not change).
    loop, session = _mcp_loop, _mcp_session
    if loop is None or session is None:
        raise RuntimeError(_mcp_error or "willow-mcp session ended")
    payload = {"app_id": APP_ID, **inputs}
    coro = session.call_tool(name, payload)
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError as exc:  # loop closed in that same window
        coro.close()  # or Python warns about a coroutine that was never awaited
        raise RuntimeError(f"willow-mcp session ended: {exc}") from exc
    return _parse_tool_payload(future.result(timeout=timeout))


def _record_forward(error: str | None) -> None:
    """Remember how the last forward went, and say so at a level an operator
    will actually see the *first* time a new failure appears.

    Repeats stay at DEBUG so a willow-mcp that is down for an hour does not
    fill a host's log with one identical line per question asked; a *changed*
    message is news and gets WARNING again.
    """
    global _last_forward_error, _forward_ok, _forward_failed
    with _forward_lock:
        if error is None:
            _forward_ok += 1
            _last_forward_error = None
            return
        first = error != _last_forward_error
        _last_forward_error = error
        _forward_failed += 1
    if first:
        log.warning("gap forward to willow-mcp failed: %s", error)
    else:
        log.debug("gap forward to willow-mcp failed again: %s", error)


def forward_status() -> dict[str, Any]:
    """What has actually happened to forwarded gaps, for a host that wants to
    show it (or a stand-up check that wants to assert it).

    Forwarding is best-effort by design, and for a long time that meant its
    failures were unobservable: a gate denial — `no manifest for 'ask-jeles'`,
    or an app_id without `gap_write` — was caught, logged at DEBUG, and
    dropped, so a misconfigured fleet looked exactly like a working one from
    every surface a caller could reach. `session_error` is why no session
    exists; `last_error` is why the last call failed on a session that does.
    """
    with _forward_lock:
        return {
            "enabled": _use_willow_mcp(),
            "app_id": APP_ID,
            "session_ready": _mcp_ready,
            "session_error": _mcp_error,
            "forwarded": _forward_ok,
            "failed": _forward_failed,
            "last_error": _last_forward_error,
        }


def last_forward_error() -> str | None:
    """The most recent forward failure, or None if the last one succeeded."""
    with _forward_lock:
        return _last_forward_error


def forward_gap(question: str, topic: str = DEFAULT_TOPIC) -> None:
    """Fire-and-forget: runs in a daemon thread, never blocks the caller,
    never raises. This is the one function the rest of a host should call
    — everything above is plumbing for it.

    Failures are recorded rather than discarded — see :func:`forward_status`.
    Still never raised: a host asking a question must not fail because a
    fleet backlog is unreachable.
    """
    if not _use_willow_mcp():
        return

    def _run() -> None:
        try:
            call_tool("gap_log", {"topic": topic, "question": question})
        except Exception as exc:
            _record_forward(str(exc) or exc.__class__.__name__)
        else:
            _record_forward(None)

    threading.Thread(target=_run, daemon=True, name="jeles-gap-forward").start()


def shutdown() -> None:
    # Snapshot, and tolerate a closed loop: loops are now closed when their
    # attempt ends, so an unguarded call_soon_threadsafe here would raise
    # RuntimeError into a host that is only trying to shut down tidily.
    loop, stop = _mcp_loop, _mcp_stop_event
    if loop is None or stop is None:
        return
    try:
        loop.call_soon_threadsafe(stop.set)
    except RuntimeError as exc:
        log.debug("willow-mcp already stopped: %s", exc)
