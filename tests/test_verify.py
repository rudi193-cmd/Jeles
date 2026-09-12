"""Properties of the per-claim verifier.

The thing under test is not "does the parser parse" — it is whether a claim can
end up labelled `corroborated` without two institutions actually standing behind
it, and whether a claim that *is* backed can be reported as `unsupported`. Those
two are the only errors that matter here, so most of this file is about them.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from jeles import verify
from jeles._independence import MIN_INDEPENDENT_SOURCES
from jeles.reactions import conflict_scan
from jeles.verify import _parse_claim_lines, verify_claims


def _stub(text):
    """An `llm_respond` that always answers with `text`."""
    return lambda system, history, user: text


# ── the verdicts ─────────────────────────────────────────────────────────────


def test_two_distinct_institutions_corroborate_a_claim():
    citations = [{"n": 1, "source": "NASA"}, {"n": 2, "source": "arXiv"}]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 1,2"))
    claim = out["claims"][0]
    assert claim["verdict"] == "corroborated"
    assert claim["institutions"] == ["NASA", "arXiv"]
    assert out["summary"] == {"total": 1, "corroborated": 1, "single_source": 0, "unsupported": 0}


def test_two_citations_from_one_institution_do_not_corroborate():
    """The whole point of the module: source *count* is not source *diversity*."""
    citations = [{"n": 1, "source": "NASA"}, {"n": 2, "source": "NASA"}]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 1,2"))
    assert out["claims"][0]["verdict"] == "single_source"
    assert out["claims"][0]["institutions"] == ["NASA"]


def test_a_claim_no_cited_source_backs_is_unsupported():
    citations = [{"n": 1, "source": "NASA"}]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: NONE"))
    assert out["claims"][0]["verdict"] == "unsupported"
    assert out["claims"][0]["sources"] == []


def test_one_institution_spelled_two_ways_is_still_one_institution():
    """Casing and stray whitespace are what upstream APIs happened to write, not
    evidence of a second institution. Comparing them verbatim manufactured a
    corroboration out of one source."""
    citations = [{"n": 1, "source": "NASA"}, {"n": 2, "source": " nasa "}]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 1,2"))
    assert out["claims"][0]["verdict"] == "single_source"


def test_a_name_wrapped_across_lines_is_still_one_institution():
    """Institution labels arrive out of XML and JSON text nodes, which is where
    a name picks up a newline or a doubled space in the middle. Comparing the
    raw strings makes that punctuation into a second institution."""
    citations = [
        {"n": 1, "source": "Library of Congress"},
        {"n": 2, "source": "Library\n   of  Congress"},
    ]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 1,2"))
    assert out["claims"][0]["verdict"] == "single_source"


def test_an_invented_source_number_is_dropped_rather_than_counted():
    citations = [{"n": 1, "source": "NASA"}]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 1, 9"))
    claim = out["claims"][0]
    assert claim["sources"] == [1]
    assert claim["verdict"] == "single_source"


def test_an_invented_number_cannot_manufacture_corroboration():
    citations = [{"n": 1, "source": "NASA"}]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 2, 3"))
    assert out["claims"][0]["verdict"] == "unsupported"


def test_the_bar_is_configurable_without_touching_the_shared_default():
    citations = [{"n": 1, "source": "NASA"}, {"n": 2, "source": "arXiv"}]
    out = verify_claims(
        "ans", "block", citations, _stub("CLAIM: x || SOURCES: 1,2"), min_institutions=3
    )
    assert out["claims"][0]["verdict"] == "single_source"


# ── the unnamed citation ─────────────────────────────────────────────────────


def test_a_backed_claim_is_never_reported_unsupported_for_want_of_a_name():
    """An absent `institution` is real — `tests/test_sources.py` pins that jeles
    leaves it empty rather than inventing one. Dropping such citations turned
    "two records back this" into the strongest available denial."""
    citations = [
        {"n": 1, "source": "", "url": "https://www.loc.gov/item/1"},
        {"n": 2, "source": "", "url": "https://arxiv.org/abs/2"},
    ]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 1,2"))
    claim = out["claims"][0]
    assert claim["verdict"] == "corroborated"
    assert claim["institutions"] == ["arxiv.org", "loc.gov"]


def test_two_unnamed_citations_from_one_site_are_one_source():
    citations = [
        {"n": 1, "url": "https://www.loc.gov/item/1"},
        {"n": 2, "url": "https://catalog.loc.gov/item/2"},
    ]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 1,2"))
    assert out["claims"][0]["verdict"] == "single_source"
    assert out["claims"][0]["institutions"] == ["loc.gov"]


def test_a_citation_with_neither_name_nor_site_backs_without_corroborating():
    """Nothing distinguishes two anonymous records, so they must not clear the
    bar — but they did back the claim, and calling that `unsupported` would be
    the report denying evidence it was handed."""
    citations = [{"n": 1}, {"n": 2}]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 1,2"))
    claim = out["claims"][0]
    assert claim["verdict"] == "single_source"
    assert claim["institutions"] == []


def test_source_over_institution_is_a_contract_a_registry_caller_must_honor():
    """``jeles.sources._result`` puts the *registry key* ("openalex", "arxiv",
    …) in ``source`` and the per-record institution in ``institution`` — the
    opposite of what `_identity` reads first. Feeding those records into
    `verify_claims` unchanged corroborates over adapters, not institutions:
    two real institutions routed through one adapter collapse into a single
    source here. This is not a bug in `_identity` — it is the documented
    precedence every other test in this file pins, and the one
    `institutional.py` citations actually need. It locks the current, correct
    behaviour for un-relabelled registry citations, and shows the fix belongs
    on the caller: re-label `source` to the institution before verifying.
    """
    citations = [
        {"n": 1, "source": "openalex", "institution": "MIT"},
        {"n": 2, "source": "openalex", "institution": "Stanford"},
    ]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 1,2"))
    claim = out["claims"][0]
    assert claim["verdict"] == "single_source"
    assert claim["institutions"] == ["openalex"]

    # What a host must do before calling verify_claims with sources.py output.
    relabelled = [{**c, "source": c["institution"] or c["source"]} for c in citations]
    out2 = verify_claims("ans", "block", relabelled, _stub("CLAIM: x || SOURCES: 1,2"))
    claim2 = out2["claims"][0]
    assert claim2["verdict"] == "corroborated"
    assert claim2["institutions"] == ["MIT", "Stanford"]


def test_the_label_outranks_the_site_so_one_resolver_is_not_one_institution():
    """18 source adapters build a doi.org citation URL. Leading with the domain
    would fold every publisher behind the resolver into a single source."""
    citations = [
        {"n": 1, "source": "Nature", "url": "https://doi.org/10.1000/a"},
        {"n": 2, "source": "Science", "url": "https://doi.org/10.1000/b"},
    ]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 1,2"))
    assert out["claims"][0]["verdict"] == "corroborated"


# ── reading what the model wrote ─────────────────────────────────────────────


def test_well_formed_lines_parse_to_claims_and_numbers():
    raw = "CLAIM: The sky is blue || SOURCES: 1, 3\nCLAIM: Water is wet || SOURCES: NONE"
    assert _parse_claim_lines(raw) == [("The sky is blue", [1, 3]), ("Water is wet", [])]


def test_bracketed_and_repeated_numbers_survive_as_one_source_each():
    raw = "preamble\nCLAIM: X happened || SOURCES: [2] and [2]\nnot a claim line"
    assert _parse_claim_lines(raw) == [("X happened", [2])]


def test_a_dropped_separator_does_not_delete_the_claim():
    """The `||` is the most droppable token in the format and the label survives
    it. Losing the claim outright would remove it from the report — the one
    outcome worse than a mangled line."""
    assert _parse_claim_lines("CLAIM: X happened SOURCES: 4") == [("X happened", [4])]


def test_a_claim_that_mentions_its_own_sources_is_not_cut_at_its_own_word():
    raw = "CLAIM: the paper lists its sources: three || SOURCES: 1"
    assert _parse_claim_lines(raw) == [("the paper lists its sources: three", [1])]


def test_a_word_merely_ending_in_claim_is_prose_not_a_claim_line():
    assert _parse_claim_lines("Reclaim: the land was returned || SOURCES: 1") == []
    assert _parse_claim_lines("disclaimer: none of this is advice") == []


def test_a_claim_with_no_sources_clause_is_read_as_backed_by_nothing():
    assert _parse_claim_lines("CLAIM: standalone assertion") == [("standalone assertion", [])]


def test_an_empty_claim_body_is_skipped_rather_than_reported():
    assert _parse_claim_lines("CLAIM:  || SOURCES: 1") == []


# ── degraded inputs ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "answer,citations",
    [
        ("", []),
        ("", [{"n": 1, "source": "NASA"}]),
        ("an answer", []),
    ],
)
def test_nothing_to_attribute_to_short_circuits_before_the_model(answer, citations):
    def never(*args):
        raise AssertionError("the model must not be called with nothing to attribute")

    out = verify_claims(answer, "block", citations, never)
    assert out == {
        "claims": [],
        "summary": {"total": 0, "corroborated": 0, "single_source": 0, "unsupported": 0},
    }


def test_a_model_failure_is_reported_not_raised():
    """A fact-check that could not run is a missing verdict. Letting it raise
    would trade the caller's degraded answer for no answer at all."""

    def boom(system, history, user):
        raise RuntimeError("llm down")

    out = verify_claims("a", "s", [{"n": 1, "source": "NASA"}], boom)
    assert out["claims"] == []
    assert out["summary"]["total"] == 0
    assert out["summary"]["error"] == "llm down"


def test_a_citation_with_no_usable_number_is_ignored_not_fatal():
    """`n` is whatever the host put there. A string cannot match a parsed source
    number, and an unhashable one cannot even be looked up — neither may take
    down the verification of the citations that are well-formed."""
    citations = [
        {"n": "one", "source": "NASA"},
        {"n": ["also not a number"], "source": "Reuters"},
        {"n": 2, "source": "arXiv"},
    ]
    out = verify_claims("ans", "block", citations, _stub("CLAIM: x || SOURCES: 1,2"))
    assert out["claims"][0]["institutions"] == ["arXiv"]
    assert out["claims"][0]["verdict"] == "single_source"


def test_the_summary_counts_every_verdict_it_reports():
    citations = [
        {"n": 1, "source": "NASA"},
        {"n": 2, "source": "arXiv"},
        {"n": 3, "source": "NASA"},
    ]
    out = verify_claims(
        "ans",
        "block",
        citations,
        _stub(
            "CLAIM: corroborated claim || SOURCES: 1,2\n"
            "CLAIM: single claim || SOURCES: 1,3\n"
            "CLAIM: unsupported claim || SOURCES: NONE\n"
        ),
    )
    assert out["summary"] == {"total": 3, "corroborated": 1, "single_source": 1, "unsupported": 1}


# ── the seams this module promises to keep ───────────────────────────────────


def test_the_corroboration_bar_is_the_one_the_conflict_reaction_applies():
    """Two modules, one rule. Stated in `_independence`, and pinned here so a
    change to either vocabulary cannot leave them quietly disagreeing about what
    "corroborated" means."""
    assert verify.DEFAULT_MIN_INSTITUTIONS == MIN_INDEPENDENT_SOURCES
    assert conflict_scan.DEFAULT_MIN_SOURCES == MIN_INDEPENDENT_SOURCES


def test_the_verifier_has_no_egress_of_its_own():
    """All egress in this package goes through `_egress`. This module's answer is
    to have none: the model is injected, so there is no URL to open. Asserted
    against the source rather than left to the docstring, in the same spirit as
    `tests/test_sources.py`'s no-source-opens-a-response check."""
    tree = ast.parse(Path(verify.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"urllib", "socket", "ssl", "http", "requests"}
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } | {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & {"urlopen", "fetch", "open", "read"}


def test_importing_the_verifier_pulls_in_no_network_stack():
    """Design principle 2, for the newest module. Run in a subprocess so an
    import elsewhere in this session cannot mask a regression."""
    probe = textwrap.dedent(
        """
        import sys
        import jeles.verify  # noqa: F401

        forbidden = {"urllib.request", "socket", "ssl", "http.client",
                     "asyncio", "requests", "mcp"}
        loaded = forbidden & set(sys.modules)
        if loaded:
            print(",".join(sorted(loaded)))
            sys.exit(1)
        """
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"jeles.verify imported network modules at import time: {result.stdout.strip()!r} "
        f"(stderr: {result.stderr.strip()!r})"
    )
