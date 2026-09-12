# Ideas — Jeles

This file is the repo's numbered idea pile. It is read by
`reconciler run --repo ./ --doc docs/ideas.md --validate` (willow-reconciler),
which classifies every item landed / partial / not started against this
repo's own git history, and by `reconciler verify`, which `.github/workflows/
trailers.yml` runs on every PR.

Legend: ✅ shipped · 🟡 partial · (untagged) proposed

**Numbers are permanent join keys.** `reconciler/ids.py` derives
`<corpus>-ideas-<num>` from the number written on the line, so a number is an
identity, not an ordinal. Never renumber; never write a markdown-auto-numbered
list (`1.` repeated) — retire a number instead and leave the gap.

A legend tag counts only when it leads the item text. An item is one line: the
reconciler reads the text after `N. ` on that line and nothing below it. Cite
the landing (a PR number written `PR #N`, or a commit) on a shipped item; cite
the source (a doc, a PR body, a design section) on an open one.

## A. Wiring Jeles into willow-mcp — carried from `docs/plans/2026-08-02-pypi-packaging-and-wiring.md`

1. ✅ **shipped**: `mcp` is an optional extra and base `jeles` declares zero runtime dependencies, so a host can depend on it without inheriting a version constraint (plan step 1). Landed in PR #6 (`f0fb73e`, the zero-dependency base package) and PR #7 (`b8486a5`, the SDK 2.0 port that made the pin co-installable with willow-mcp); `tests/test_import_purity.py` and CI's no-extras leg hold it.

2. ✅ **shipped**: release metadata and a tag-driven publish path — release-please, `release.yml`, and the PR-title guard (plan step 2). Landed in PR #6, PR #9 (`0e7fa76`, release automation) and PR #26 (`f5d7496`, the title guard); the package is tagged through v0.14.0 on PyPI.

3. The layering decision (plan step 3, register 9): one librarian instead of three — local verified corpus first, jeles-remote's institutional fan-out on a miss, the gap log when nothing answers. Routes the searcher through willow-mcp's gated egress and turns a silent `[]` into a recorded gap. Open; the plan's 2026-09-12 closure left it out of scope on purpose.

4. ✅ **shipped**: the corpus server reaches the web edge itself — `corpus_web_search`, the server's second hop (plan register 1, "Jeles' web edge is unreachable from its own MCP server"). Landed in `79342ed` via PR #15 (2026-08-03).

5. ✅ **shipped**: the zero-config web default is a real DuckDuckGo HTML-SERP scrape behind a circuit breaker, not the shallow Instant-Answer endpoint that failed soft and looked alive (plan register 2). Landed in `68fd2b5` via PR #61 (2026-08-10).

6. Three-key egress friction in local willow-mcp — manifest `web_net`, a standing `consent.internet`, a 30-minute CLI-minted lease, and an optional strict trust root (plan register 3). Lives in willow-mcp, not here; carried because the plan's register carried it and item 3 routes this repo's search through that gate.

7. ✅ **shipped**: Jeles' egress posture is decided — one scheme-and-destination guard in `jeles/_egress.py` for all three lanes, checked on every redirect hop (plan register 4, "raw urllib vs. gated"). Landed in `d36aa76` via PR #20; the reasoning is `docs/design/dependencies-and-egress.md`.

8. ✅ **shipped**: this repo has a `docs/` tree (plan register 5). Opened by the packaging plan itself, `a2e5ae7` via PR #6.

9. ✅ **shipped**: the trusted-source registry lives in this package — `jeles/sources.py`'s institutional collections with their declared hosts, and the per-host cards under `jeles/cards/` (plan register 6, "split between `willow_mcp/web_search.py` and `core/jeles_sources.py`"). Landed in `1d7d2bd` via PR #20 and `cddede6` (the 84-host catalog).

10. Consult kartikeya's publish workflow and make this repo's `release.yml` match it where the two should be identical (plan step 2, "two steps that require a human"). The kart repo was not in the workspace when the plan was written, so this repo publishes to the standard pattern rather than the fleet's.

## B. Design follow-ups — `docs/design/`

11. Add jeles' card schema to willow-mcp's `docs/design/fleet-versioning.md` Rule 2 surface table, beside "importable API" (host-cards.md §6.1, "Follow-up, not done here"). Lives in willow-mcp.

## C. Test hygiene — recorded by the 2026-09-12 survey waves

12. Factor the nine inline scans recorded in `tests/test_scans_fire.py`'s `INLINE_SCANS_STILL_TO_FACTOR` into helpers with a plant in the same file, deleting each ratchet line as it goes (from #82).

13. Retire the duplicate seed tree: `corpus/seed/` and `jeles/seed/` are byte-identical and pinned together by `tests/test_seed_dirs_in_step.py`; make `corpus/compose.py` read the packaged copy and remove the authoring one (from #81).

14. Verify `corpus/README.md`'s "2105 evidence entries" claim from the tree, the way `tests/test_corpus_counts.py` verifies the pair counts (from #81).

15. Run the live conventions-equality check on CI's matrix legs by adding `willow-reconciler>=0.6.0` to the `dev` extra; `tests/test_fleet_conventions.py` already skips it by name and needs no change (from #83).

## D. The evidence loop — fleet plan Wave 3

16. ✅ **shipped**: keep this numbered idea pile at `docs/ideas.md`, in the reconciler's form and validated by `reconciler run --validate` (E3-piles). Landed by the commit that added this file, which carries the trailer for this item.

17. adopt `Idea-Id` commit trailers (fleet CONVENTION, decision-2026-09-11): `.github/workflows/trailers.yml` runs `reconciler verify` on every PR and push to master, CONTRIBUTING.md documents how to generate a trailer rather than type one, and `tests/test_fleet_conventions.py` requires the gate wherever the pile exists (E3-trailers).
