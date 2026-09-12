"""corpus.py must stay pure: importing it loads no MCP and no network stack.

Design principle 2 — `corpus.py` has no MCP, no network, no side effects
beyond SQLite. This test imports it in a *fresh* subprocess and asserts that
none of the MCP/HTTP/socket machinery got dragged in as an import side
effect. Run in a subprocess so a prior import in this session's interpreter
(e.g. from corpus_server tests) can't mask a real regression.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_importing_corpus_pulls_in_no_mcp_or_network_modules():
    probe = textwrap.dedent(
        """
        import sys
        import jeles.corpus  # noqa: F401

        forbidden = {
            "mcp",
            "mcp.server",
            "mcp.server.fastmcp",
            "mcp.client",
            "anyio",
            "httpx",
            "httpcore",
            "requests",
            "aiohttp",
            "urllib.request",
            "socket",
            "ssl",
            "asyncio",
        }
        loaded = forbidden & set(sys.modules)
        if loaded:
            print(",".join(sorted(loaded)))
            sys.exit(1)
        sys.exit(0)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "jeles.corpus imported network/MCP modules at import time: "
        f"{result.stdout.strip()!r} (stderr: {result.stderr.strip()!r})"
    )


def test_base_package_declares_no_runtime_dependencies():
    """The packaging-level half of the same promise.

    Module purity keeps `import jeles.corpus` cheap; *dependency* purity is what
    lets a host depend on this package at all. `mcp` was once a hard runtime
    dependency pinned <2.0.0, which made `pip install willow-mcp jeles`
    unresolvable — willow-mcp requires mcp>=2. Nothing caught that, because no
    test looked at the metadata.

    Anything the package genuinely needs belongs in an extra, not here.
    """
    from importlib.metadata import PackageNotFoundError, requires

    try:
        declared = requires("jeles") or []
    except PackageNotFoundError:  # source tree with no install; nothing to check
        return

    # Extras are declared as `name; extra == "mcp"` — those are opt-in and fine.
    unconditional = [r for r in declared if "extra ==" not in r]
    assert not unconditional, (
        "base `jeles` must declare zero runtime dependencies so that hosts "
        "inherit no version constraints from it; found: "
        f"{unconditional!r}. Put it in [project.optional-dependencies] instead."
    )
