"""Jeles — the verified-corpus organ and its canonical persona.

A small, dependency-light package extracted from the Ask Jeles app so the
verified-nugget corpus, its standalone MCP server, the best-effort fleet
gap-forwarder, and the canonical Jeles persona can be consumed by any host.

Public surface:
    corpus              — pure storage/ranking of verified nuggets + gaps
    corpus_server       — standalone MCP server over the corpus (SDK 2.x)
    verify              — per-claim cross-source corroboration of an answer
    source_trail        — per-claim verification of raw, uncited prose
    legal_citations     — verify prose citations against CourtListener
    willow_mcp_client   — best-effort gap forwarding to willow-mcp
    load_persona()      — load the canonical Jeles persona JSON

Submodules are not imported here. Importing `jeles` must stay free of I/O and of
the network stack (`tests/test_import_purity.py`), so a consumer imports the one
it wants.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


def __getattr__(name: str) -> Any:
    """Lazily resolve ``jeles.__version__`` from installed package metadata.

    The version is no longer a literal here — it duplicated pyproject.toml's,
    which is the pattern that had already drifted three releases apart in
    kartikeya and one apart in willow-mcp's plugin manifest. Metadata is written
    at build time from the git tag and cannot drift.

    Resolved lazily (PEP 562) rather than at import, because
    ``importlib.metadata`` pulls in ``socket`` — which would break design
    principle 2 and fail ``tests/test_import_purity.py``. Reading a version
    should not be what puts a network module in ``sys.modules``.
    """
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        try:
            return _pkg_version("jeles")
        except PackageNotFoundError:  # source tree with no install
            return "0.0.0+unknown"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_PERSONA_PATH = Path(__file__).resolve().parent / "persona" / "jeles_persona.json"


def persona_path() -> Path:
    """Absolute path to the canonical Jeles persona JSON shipped in this
    package. This package is the persona's canonical home."""
    return _PERSONA_PATH


@lru_cache(maxsize=1)
def load_persona() -> dict[str, Any]:
    """Load and return the canonical Jeles persona as a dict.

    Stdlib-only, no I/O beyond a single file read. Cached after the first
    call. Mutating the returned dict mutates the cached copy — treat it as
    read-only, or copy it if you need to edit.
    """
    return json.loads(_PERSONA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def persona_prompt() -> str:
    """The canonical Jeles persona rendered to a system-prompt string.

    The combine (#18): one JSON source of truth here, compiled deterministically
    into the labeled prompt every host used to hand-carry as its own prose copy.
    Consumers call this instead of pasting a persona string. Cached.
    """
    from jeles.persona.compiler import compile_persona

    return compile_persona(load_persona())


__all__ = ["__version__", "load_persona", "persona_path", "persona_prompt"]
