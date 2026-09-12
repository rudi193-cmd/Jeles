"""The release chain is four files that must agree, and every disagreement is silent.

    release-please-config.json     decides the tag name and what cuts a release
    .release-please-manifest.json  is the version it bumps from
    .github/workflows/release-please.yml  opens and auto-merges the release PR
    .github/workflows/release.yml  fires on a tag pattern and publishes

Nothing joins them up at runtime. A mismatch does not raise — it means a
release quietly does not happen, which this fleet has now done in four
distinct ways: a tag pushed by a bot token that started no workflow, a
`workflow_call` whose attestation and authentication disagreed, a `ci:` commit
that published a version containing nothing installable, and a config that
would have tagged `willow-mcp-v2.2.0` while the publish workflow listened for
`v*`.

This file is ported from willow-mcp, where that last one was caught by having
auto-merge armed on the release PR. jeles happens to be configured correctly —
so these are guards against a future edit, not a live bug.

**Ported, not copied.** Two checks are deliberately inverted for this repo:
jeles is below 1.0 and carries the pre-major bump flags that willow-mcp must
not have, and jeles has no second version file to keep in step because nothing
here stores a version at all. Getting those backwards is exactly the
copy-paste failure the willow-mcp version was written after.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

import pytest

# tomllib is stdlib only from 3.11, and the matrix floor is 3.10 — where this
# file would have failed to *collect*, taking every check in it down with it.
# `tomli` is in the dev extra for that leg.
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 only
    import tomli as tomllib

yaml = pytest.importorskip("yaml", reason="PyYAML needed to read the workflows")

_REPO = Path(__file__).resolve().parents[1]
_CONFIG = _REPO / "release-please-config.json"
_MANIFEST = _REPO / ".release-please-manifest.json"
_RELEASE_WF = _REPO / ".github" / "workflows" / "release.yml"
_RP_WF = _REPO / ".github" / "workflows" / "release-please.yml"


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _package_config() -> dict:
    return _json(_CONFIG)["packages"]["."]



# A credential whose events actually trigger workflows. Either form is
# acceptable; what is NOT acceptable is GITHUB_TOKEN, whose events GitHub
# suppresses — the release PR merges, no tag workflow fires, nothing publishes.
#
# This originally pinned the literal RELEASE_PLEASE_TOKEN, which named the
# mechanism rather than the property. The willow-ci App satisfies the same
# property (an installation token is not GITHUB_TOKEN) and adds hourly expiry,
# so the check accepts either. The GITHUB_TOKEN prohibitions below are
# unchanged — that is the half that guards the failure jeles paid three
# releases for.
NON_SUPPRESSED_CREDENTIALS = (
    "RELEASE_PLEASE_TOKEN",              # fine-grained PAT (being retired)
    "steps.app-token.outputs.token",     # willow-ci App installation token
)


def _names_a_non_suppressed_credential(value: object) -> bool:
    text = str(value)
    return any(c in text for c in NON_SUPPRESSED_CREDENTIALS)


def test_the_credential_scan_catches_a_planted_token_and_not_github_token():
    """Planted, both ways. The scan must see either accepted credential inside
    the expression a workflow would actually write, and must *not* be satisfied
    by `GITHUB_TOKEN` — the one whose events GitHub suppresses, which is the
    failure this file guards against. A scan that matched on the word "token"
    would pass every check below it for the wrong reason."""
    assert _names_a_non_suppressed_credential("${{ secrets.RELEASE_PLEASE_TOKEN }}")
    assert _names_a_non_suppressed_credential("${{ steps.app-token.outputs.token }}")
    assert not _names_a_non_suppressed_credential("${{ secrets.GITHUB_TOKEN }}")
    assert not _names_a_non_suppressed_credential({"token": "${{ github.token }}"})


def test_the_tag_release_please_creates_matches_what_release_yml_listens_for():
    """With `include-component-in-tag` unset it defaults to *true*, and the tag
    becomes `<package-name>-vX.Y.Z` — which `v*` does not match, so the publish
    workflow never runs and nothing reports an error. Observed on
    willow-mcp#256, where the setting had been dropped when copying this repo's
    config."""
    cfg = _package_config()
    version = _json(_MANIFEST)["."]

    if cfg.get("include-component-in-tag", True):
        tag = f"{cfg['package-name']}-v{version}"
    else:
        tag = f"v{version}"

    # `on:` parses as the boolean True — PyYAML applies the YAML 1.1 rule.
    patterns = list(_yaml(_RELEASE_WF)[True]["push"]["tags"])
    assert any(fnmatch.fnmatch(tag, p) for p in patterns), (
        f"release-please would create the tag {tag!r}, which matches none of "
        f"release.yml's trigger patterns {patterns!r}. Nothing would publish, "
        f"and nothing would report an error."
    )


def test_the_version_has_exactly_one_source():
    """willow-mcp's equivalent check is that its manifest and
    `.claude-plugin/plugin.json` agree, because that file carries a version
    nothing derives — and it had already drifted.

    jeles has no such file, and that is the property worth pinning: the tag is
    the only place a version exists. `dynamic = ["version"]` plus hatch-vcs is
    what makes that true, and a literal `version =` in pyproject or a hardcoded
    `__version__` in the package would quietly become a second copy to drift.
    """
    pyproject = tomllib.loads((_REPO / "pyproject.toml").read_text())
    assert "version" in (pyproject["project"].get("dynamic") or []), \
        "project.version must stay dynamic — a literal is a second copy"
    assert "version" not in pyproject["project"], \
        "a literal project.version defeats the tag-derived scheme"
    assert pyproject["tool"]["hatch"]["version"]["source"] == "vcs"

    # No `extra-files` either: there is nothing for release-please to bump.
    assert not _package_config().get("extra-files"), \
        "nothing in this repo stores a version, so nothing needs bumping"

    hardcoded = [
        f"{p.relative_to(_REPO)}:{i}"
        for p in (_REPO / "jeles").rglob("*.py")
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if re.match(r"\s*__version__\s*=\s*[\"']", line)
    ]
    assert not hardcoded, f"hardcoded version string(s): {hardcoded}"


def test_release_automation_uses_the_pat_everywhere():
    """A bot token silently produces no workflow runs: the release PR merges,
    no tag workflow fires, nothing publishes. This repo lost three releases to
    that exact substitution — see the comment block in release-please.yml."""
    steps = _yaml(_RP_WF)["jobs"]["release-please"]["steps"]

    # Match `secrets.X` references only. A plain substring search would flag
    # prose that names GITHUB_TOKEN while explaining why it is wrong.
    used: set[str] = set()
    values: list[str] = []
    for step in steps:
        for value in list((step.get("env") or {}).values()) + \
                     list((step.get("with") or {}).values()):
            values.append(str(value))
            used.update(re.findall(r"secrets\.([A-Z_]+)", str(value)))

    assert any(_names_a_non_suppressed_credential(v) for v in values), used
    assert "GITHUB_TOKEN" not in used, (
        "release-please and the auto-merge arming must both use the PAT — "
        f"events generated with GITHUB_TOKEN start no workflow runs. Found: {used}"
    )


def test_auto_merge_waits_for_ci_rather_than_merging_directly():
    """`--auto` is the part that makes the merge wait for the required checks.
    A step that fell back to a plain `gh pr merge` on failure would publish off
    an unverified commit, and a PyPI version can never be reused."""
    steps = _yaml(_RP_WF)["jobs"]["release-please"]["steps"]
    arming = [s for s in steps if "gh pr merge" in str(s.get("run", ""))]
    assert arming, "no step arms auto-merge on the release PR"
    for step in arming:
        for line in step["run"].splitlines():
            if "gh pr merge" in line and not line.strip().startswith("#"):
                assert "--auto" in line, f"merge without --auto: {line.strip()}"
                assert "--squash" not in line, "this repo does not squash-merge"


def test_the_changelog_is_rebuilt_before_auto_merge_is_armed():
    """Order is the whole point. The correction must land on the release PR
    *before* auto-merge can take it, or the release ships with the wrong section
    and gets fixed afterwards — which is what it replaces.

    0.5.0 listed its single change twice, from `bbc8258` (the merge of #27) and
    `455be56` (the commit it merged), and was corrected by hand in #29.
    """
    steps = _yaml(_RP_WF)["jobs"]["release-please"]["steps"]
    names = [s.get("name") or str(s.get("uses", "")) for s in steps]

    def index_of(needle: str) -> int:
        hits = [i for i, n in enumerate(names) if needle in n]
        assert hits, f"no step matching {needle!r} in {names}"
        return hits[0]

    assert (index_of("actions/checkout") < index_of("release-please-action")
            < index_of("Rebuild the changelog") < index_of("Arm auto-merge")), names

    # The tool derives entries from `git log <previous tag>..<this release>`, so
    # a shallow clone or missing tags silently changes what it computes.
    checkout = next(s for s in steps
                    if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0, "needs full history for the range"
    assert checkout["with"]["fetch-tags"] is True, "needs tags to find the previous release"


def test_a_changelog_bail_does_not_block_the_release():
    """**The bug willow-mcp shipped, carried here as a guard rather than as a
    repeat.** There the step ran under `set -e`, so exit 2 — the tool correctly
    refusing a section it cannot model — skipped the auto-merge arming below it
    and stopped the release entirely. Its very first real run did exactly that.

    Failing closed is wrong here: the worst case this tool guards against is a
    wrong *changelog*, and trading that for "no release at all" is a bad deal."""
    steps = _yaml(_RP_WF)["jobs"]["release-please"]["steps"]
    step = next(s for s in steps if "Rebuild the changelog" in (s.get("name") or ""))
    run = step["run"]
    assert "::warning::" in run, "a bail must warn"
    assert 'status" = "2"' in run, "exit 2 must be handled explicitly, not by set -e"
    assert _names_a_non_suppressed_credential(step.get("env"))
    assert "GITHUB_TOKEN" not in str(step.get("env"))


def test_the_changelog_tool_exists_and_the_workflow_calls_it():
    """A workflow step invoking a script nobody ships is a silent no-op, on a
    path nobody watches."""
    assert (_REPO / "tools" / "changelog_dedup.py").exists()
    steps = _yaml(_RP_WF)["jobs"]["release-please"]["steps"]
    runs = " ".join(str(s.get("run", "")) for s in steps)
    assert "tools/changelog_dedup.py" in runs


def test_the_pr_title_check_guards_both_directions():
    """One direction stops a title inventing a release; the other stops a commit
    releasing something nobody installs. v0.4.1 shipped for a single `ci:`
    commit, and willow-mcp's 2.1.5 shipped for edits to tools/ and .github/.

    The packaged path must be **this** repo's. `src/willow_mcp/` is willow-mcp's
    layout; here the wheel packages `jeles`, so a copied constant would make the
    check pass on everything."""
    wf = _REPO / ".github" / "workflows" / "pr-title.yml"
    body = _yaml(wf)["jobs"]["title"]["steps"][-1]["run"]
    assert 'PACKAGED = ("jeles/", "pyproject.toml")' in body, \
        "packaged path is wrong or was copied from another repo"
    assert "src/willow_mcp" not in body, "willow-mcp's path leaked into this port"

    # And it really is what the wheel ships.
    pyproject = tomllib.loads((_REPO / "pyproject.toml").read_text())
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["jeles"]


def test_only_types_that_change_the_installed_package_cut_a_release():
    """Every un-hidden type releases on its own — not just feat and fix. v0.4.1
    was tagged and published for a single `ci:` commit that changed a workflow
    file, which was survivable when a human merged the release PR and is not
    now that auto-merge does."""
    sections = _package_config()["changelog-sections"]
    visible = {s["type"] for s in sections if not s.get("hidden")}
    assert visible == {"feat", "fix", "security", "perf", "refactor",
                       "build", "deps"}, visible
    for t in ("docs", "test", "ci", "chore"):
        assert next(s for s in sections if s["type"] == t).get("hidden") is True


def test_a_breaking_change_below_1_0_cuts_1_0_0_rather_than_a_minor():
    """`bump-minor-pre-major` was true here and is now false, which is a policy
    change rather than a tidy-up — so this test flipped with it.

    True meant a breaking change stayed at a minor, keeping 1.0 a decision
    someone makes rather than one a commit message makes. The price was paid
    downstream: a `<1.0.0` cap accepted every version this config could produce,
    so it was not a compatibility range. willow-mcp tried to fix that at its own
    end with `jeles>=0.5.1,<0.6.0` and withdrew it — a tight cap also blocks
    *this* package's security patches from reaching its users, and this package
    is where willow_institutional_search's SSRF defence lives.

    So the range means something now, and 1.0 arrives when compatibility breaks
    rather than when someone declares it. `bump-patch-for-minor-pre-major` stays
    false, which is what keeps `feat` at a minor and `fix` at a patch.
    """
    cfg = _package_config()
    assert cfg.get("bump-minor-pre-major") is False, (
        "true caps a breaking change at a minor, which makes a downstream "
        "`<1.0.0` cap meaningless. See willow-mcp docs/design/fleet-versioning.md")
    assert cfg.get("bump-patch-for-minor-pre-major") is False, \
        "with this true, a feat would bump the patch instead of the minor"
    version = _json(_MANIFEST)["."]
    assert version.startswith("0."), (
        f"manifest is {version} — past 1.0 both flags are dead weight, because "
        "`isPreMajor` gates them and it is false from 1.0.0 on. Remove them."
    )


def test_the_checkout_uses_the_pat_so_its_pushes_are_not_gated():
    """`actions/checkout` persists whatever credential it used, and the changelog
    step's `git push` then uses it. `env: GH_TOKEN` only reaches the `gh` CLI.

    With the default GITHUB_TOKEN the commit is pushed as github-actions[bot],
    and the release PR's CI run comes back `action_required` — created, but held
    awaiting manual approval — so auto-merge waits on a check that never
    reports. Observed on the 2.2.0 release PR, which needed CI started by hand;
    release-please's own commit on the same branch was not gated, because it
    pushes with the PAT.

    This is the fourth way this fleet has been bitten by token attribution, so
    it gets a test rather than a comment."""
    steps = _yaml(_RP_WF)["jobs"]["release-please"]["steps"]
    checkout = next(s for s in steps
                    if str(s.get("uses", "")).startswith("actions/checkout"))
    token = str((checkout.get("with") or {}).get("token", ""))
    assert _names_a_non_suppressed_credential(token), (
        "checkout must carry a credential whose events trigger workflows. "
        f"Got: {token!r}")
    assert "GITHUB_TOKEN" not in token
