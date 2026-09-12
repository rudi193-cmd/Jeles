# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries below 0.2.0 were written by hand. From 0.2.0 onward this file is
maintained by release-please, which builds each entry from the
conventional-commit prefixes on `master` — so it cannot go stale between
releases the way a hand-kept changelog can.

**Generated entries are sometimes corrected by hand, and this is why.** This
repo merges with merge commits rather than squashing, and GitHub writes the PR
title into the merge commit body. release-please parses that merge commit
*alongside* the commits it merges, so one change can produce two identical
entries — 0.5.0 listed its single change twice, once from merge commit
`bbc8258` and once from the commit it merged, `455be56`. The same thing hit
willow-mcp, in a worse form: there it also *dropped* a real commit in favour of
the merge commit that swallowed it, so a shipped fix went undocumented. The
tags and version numbers are unaffected either way — only the prose. Fixes go
in a `docs:` commit, which is hidden and cuts no release of its own.

## [0.14.1](https://github.com/hornbook-knowledge/Jeles/compare/v0.14.0...v0.14.1) (2026-09-12)


### Fixed

* **sources:** log a failed URL without its query string ([f01b63f](https://github.com/hornbook-knowledge/Jeles/commit/f01b63f48663371bd71729a7a934726d70bcaad1))

## [0.14.0](https://github.com/hornbook-knowledge/Jeles/compare/v0.13.0...v0.14.0) (2026-09-02)


### Added

* **commons:** auto-promote core seed nuggets to machine rung ([2fe5f9c](https://github.com/hornbook-knowledge/Jeles/commit/2fe5f9ce2c4e9210dc3c746ebda084999d758c1d))
* **intake:** add operator corpus load, probe, and gap resolve tooling ([273d262](https://github.com/hornbook-knowledge/Jeles/commit/273d26287ebec47d4a0562288abb721e83fc826e))

## [0.13.0](https://github.com/hornbook-knowledge/Jeles/compare/v0.12.0...v0.13.0) (2026-08-29)


### Added

* ship the seed corpus and load it with jeles-seed ([c10d50e](https://github.com/hornbook-knowledge/Jeles/commit/c10d50ed467caca29ad6ac23267ad0d7926ba749))


### Fixed

* **nestor-seal:** a seal covers the normalized source, not the raw question ([f3617ec](https://github.com/hornbook-knowledge/Jeles/commit/f3617ec6c84678a84e76a9688d3ae1590ef4bfc6))

## [0.12.0](https://github.com/hornbook-knowledge/Jeles/compare/v0.11.0...v0.12.0) (2026-08-29)


### Added

* **reactions:** nugget drafting, where the model may phrase but not source ([e61f2fc](https://github.com/hornbook-knowledge/Jeles/commit/e61f2fc5de3d1aa6d33f13d510b0c06f815ce1d2))
* **reactions:** gap triage, proposing groupings and writing nothing ([608d23f](https://github.com/hornbook-knowledge/Jeles/commit/608d23f2bba2eed37c55ab75da47b7e44f37c8c3))
* **source_trail:** a relevance judge that may only demote ([c90536a](https://github.com/hornbook-knowledge/Jeles/commit/c90536a950e17499b23d1163c70b68e18c48dad2))

## [0.11.0](https://github.com/hornbook-knowledge/Jeles/compare/v0.10.0...v0.11.0) (2026-08-28)


### Added

* **corpus:** expose claim verification and the host catalog ([4aaa057](https://github.com/hornbook-knowledge/Jeles/commit/4aaa057660b59ea873c06bd2734a92021b4cc1b5))


### Fixed

* **sources:** question_to_query stripped the wrong words ([4d1317a](https://github.com/hornbook-knowledge/Jeles/commit/4d1317ae1aa5291acf9e56200ef3db0b01959020))
* **sources:** zenodo called a user account an institution ([22ddeb5](https://github.com/hornbook-knowledge/Jeles/commit/22ddeb5529993cd431be96adc95fbe860eba9a4f))
* **source_trail:** confidence was the publisher's rank, not the match's ([6b4ae1a](https://github.com/hornbook-knowledge/Jeles/commit/6b4ae1a1801d6e6dfa319afb016475ef6246a7e5))

## [0.10.0](https://github.com/hornbook-knowledge/Jeles/compare/v0.9.0...v0.10.0) (2026-08-28)


### Added

* **corpus:** close the gap queue and open a window on both fleet edges ([4cbaaf1](https://github.com/hornbook-knowledge/Jeles/commit/4cbaaf15f2e9c0d7259b05484158c99d5da4a812))

## [0.9.0](https://github.com/rudi193-cmd/Jeles/compare/v0.8.0...v0.9.0) (2026-08-10)


### Added

* add courtlistener_verify_citations (legal_citations module) ([9e3ff17](https://github.com/rudi193-cmd/Jeles/commit/9e3ff171b2a1bf694e26c6cbac7ee4ef053f8550))
* **search_adapter:** rewrite ddg onto a real DuckDuckGo HTML SERP + circuit breaker ([68fd2b5](https://github.com/rudi193-cmd/Jeles/commit/68fd2b584e2da836df04e7e1e57c24e66628e080))
* **sources:** port question_to_intent, the prose gate, and the results cache ([444136c](https://github.com/rudi193-cmd/Jeles/commit/444136c95d9522eb16418f75dda501faca18fe5d))
* **source_trail:** port willow-2.0's core/source_trail.py claim verifier ([e3aad67](https://github.com/rudi193-cmd/Jeles/commit/e3aad675a56d6168c5c1edf3fa386b41616d5038))

## [0.8.0](https://github.com/rudi193-cmd/Jeles/compare/v0.7.2...v0.8.0) (2026-08-10)


### Added

* **corpus:** add optional evidence field so a verification can cross a repo boundary intact ([4a541eb](https://github.com/rudi193-cmd/Jeles/commit/4a541ebc6cda41d1f1f099d2ca13bdeb2f469a10))

## [0.7.2](https://github.com/rudi193-cmd/Jeles/compare/v0.7.1...v0.7.2) (2026-08-07)


### Fixed

* **willow_mcp_client:** treat an in-band {"error": ...} as a failed forward ([e6e1937](https://github.com/rudi193-cmd/Jeles/commit/e6e193720e0a6fdee8858225d5d2897722cf64de))

## [0.7.1](https://github.com/rudi193-cmd/Jeles/compare/v0.7.0...v0.7.1) (2026-08-07)


### Fixed

* **willow_mcp_client:** stop discarding failed gap forwards ([1dbd984](https://github.com/rudi193-cmd/Jeles/commit/1dbd98421f5f82960b7e95f10958e65728812d91))

## [0.7.0](https://github.com/rudi193-cmd/Jeles/compare/v0.6.1...v0.7.0) (2026-08-05)


### Added

* **verify:** per-claim corroboration — "six sources" is not "every sentence is backed" ([77e497e](https://github.com/rudi193-cmd/Jeles/commit/77e497e8c19c7248e5d96b94e145d192bf2e9937))
* **cards:** drop `observed` — reachability is a decision, not a stored measurement ([3f3415e](https://github.com/rudi193-cmd/Jeles/commit/3f3415ee094ddab05f4ea73ae2a4965efa7c6720))
* **cards:** the 84-host catalog, one file per host ([cddede6](https://github.com/rudi193-cmd/Jeles/commit/cddede60507d115d4d3c93d25ed1037a01785740))

## [0.6.1](https://github.com/rudi193-cmd/Jeles/compare/v0.6.0...v0.6.1) (2026-08-04)


### Fixed

* **sources:** coerce list-valued API fields, and move Chronicling America off its retired endpoint ([2e86f82](https://github.com/rudi193-cmd/Jeles/commit/2e86f82ba40e7d88e0289267ded1af5f8a5cce53))

## [0.6.0](https://github.com/rudi193-cmd/Jeles/compare/v0.5.2...v0.6.0) (2026-08-04)


### Added

* **sources:** recover the four sources stranded in the archived fork ([e72889c](https://github.com/rudi193-cmd/Jeles/commit/e72889c31f96e20e2ae4f968cb51e9d3617b692d))

## [0.5.2](https://github.com/rudi193-cmd/Jeles/compare/v0.5.1...v0.5.2) (2026-08-04)


### Fixed

* **egress:** the open-web lane exempted every backend, and leaked its keys ([91c2788](https://github.com/rudi193-cmd/Jeles/commit/91c2788c5fa7f016133725692562637456909d4f))

## [0.5.1](https://github.com/rudi193-cmd/Jeles/compare/v0.5.0...v0.5.1) (2026-08-04)


### Fixed

* **egress:** a percent-encoded host walked straight past the destination guard ([e0ed18f](https://github.com/rudi193-cmd/Jeles/commit/e0ed18f2f02c3d0419c565e1d0a2a097cf5aaa66))
* **egress:** a source redirect could reach any address, including localhost ([7d22acb](https://github.com/rudi193-cmd/Jeles/commit/7d22acbb49a69adf54bb2aaadf842b5fc7cf6567))

## [0.5.0](https://github.com/rudi193-cmd/Jeles/compare/v0.4.1...v0.5.0) (2026-08-04)


### Added

* **sources:** declare the hosts each source contacts ([455be56](https://github.com/rudi193-cmd/Jeles/commit/455be56673f62c42d097ca3bcf5819c64268bfe9))

## [0.4.1](https://github.com/rudi193-cmd/Jeles/compare/v0.4.0...v0.4.1) (2026-08-03)


### CI

* arm auto-merge on the release PR so releases ship without a reminder ([1ebe9d1](https://github.com/rudi193-cmd/Jeles/commit/1ebe9d1f976c8a5b48dd0596cd1da2ad67a91a72))

## [0.4.0](https://github.com/rudi193-cmd/Jeles/compare/v0.3.1...v0.4.0) (2026-08-03)


### Added

* bring the institutional collections into the package ([1d7d2bd](https://github.com/rudi193-cmd/Jeles/commit/1d7d2bd952451dc663242d44321fe837846fd696))


### Fixed

* **conflict_scan:** apply() validates proposals instead of splatting them ([98ee4b3](https://github.com/rudi193-cmd/Jeles/commit/98ee4b365b0cad4a0fca708decaed34c4b886d16))
* **corpus:** a short word can be the word that changes the answer ([1407e53](https://github.com/rudi193-cmd/Jeles/commit/1407e53768889ca2b107382b75d23d39084f3ea1))
* **corpus:** a tool call cannot mint a verified nugget ([c0f7941](https://github.com/rudi193-cmd/Jeles/commit/c0f79411aa4709dc51cdbea5d9c2b921fc413ee8))
* **corpus:** make non-Latin and short questions answerable, and stop merging opposites ([bb70dfd](https://github.com/rudi193-cmd/Jeles/commit/bb70dfdb32f7853cd67976348b89ae753563b7d2))
* **corpus:** stop clobbering the shared store on every write ([50e08e5](https://github.com/rudi193-cmd/Jeles/commit/50e08e559e5b7960665b3ffc94c968e645f7ee7e))
* **egress:** one scheme guard for all three lanes, checked on every hop ([d36aa76](https://github.com/rudi193-cmd/Jeles/commit/d36aa76d5279ffe9076f276a6fb3f277c7788877))
* **institutional:** decide "could not look" from what was actually looked at ([5b56d11](https://github.com/rudi193-cmd/Jeles/commit/5b56d11cd1c79d688c6392763660c82188ed89d4))
* **sources:** account for every source the fan-out dispatched ([e60f26d](https://github.com/rudi193-cmd/Jeles/commit/e60f26d92c79a1eef9e3a5d46c1af26d7fa2a3c8))
* **sources:** make the size cap and the scheme guard structural ([508d0a5](https://github.com/rudi193-cmd/Jeles/commit/508d0a5d1385ed51cdf2c7a4c0c359db24718d48))
* stop answering confidently with the wrong nugget ([4c5d8d1](https://github.com/rudi193-cmd/Jeles/commit/4c5d8d198ce01538af9bc95a794826811d2344a4))
* stop corroborating prior art from non-witnesses ([8cc6ae0](https://github.com/rudi193-cmd/Jeles/commit/8cc6ae0c73e1975d8f544adde3e243c91fea68cd))
* **willow_mcp_client:** recover from a session that died, and stop leaking loops ([2f4f6cd](https://github.com/rudi193-cmd/Jeles/commit/2f4f6cd1cc71c68703a0fda54da8b1251a6610d7))


### Docs

* **sources:** correct the last "~65 sources" claim ([7198dcd](https://github.com/rudi193-cmd/Jeles/commit/7198dcd38ff52011ab2cf087f126b6b0aa31a7dc))

## [0.3.1](https://github.com/rudi193-cmd/Jeles/compare/v0.3.0...v0.3.1) (2026-08-03)


### CI

* cut releases with a PAT so the tag push actually triggers publishing ([7399757](https://github.com/rudi193-cmd/Jeles/commit/7399757b7cb562b5876b732a363b1f8800a63d96))

## [0.3.0](https://github.com/rudi193-cmd/Jeles/compare/v0.2.1...v0.3.0) (2026-08-03)


### Added

* give the corpus server its second hop — the open web ([79342ed](https://github.com/rudi193-cmd/Jeles/commit/79342ed0c8905431b3819f440c3be9ffefa46013))

## [0.2.1](https://github.com/rudi193-cmd/Jeles/compare/v0.2.0...v0.2.1) (2026-08-03)


### Fixed

* extras went to the wrong jobs — no-extras is pure again ([3006ed9](https://github.com/rudi193-cmd/Jeles/commit/3006ed94268fc9150c68b00667d64e2b54088595))
* make the search edge say why it found nothing ([68b103b](https://github.com/rudi193-cmd/Jeles/commit/68b103be2c62098115c1e12509c6329a6600f8b8))


### CI

* add a workflow_dispatch recovery path to release.yml ([9569d2a](https://github.com/rudi193-cmd/Jeles/commit/9569d2a0d9203b8f2f6647e7d183cba87d4ae800))
* call release.yml from release-please instead of hoping to trigger it ([ecb40d9](https://github.com/rudi193-cmd/Jeles/commit/ecb40d96f2d9b65a217af8d6a2f3f31b81bfbd6a))
* coverage floor at 80 — measured 85.8%; tests run with the SDK ([e9328c9](https://github.com/rudi193-cmd/Jeles/commit/e9328c9ca88881e273764819b9cceea77cdbc86b))

## [0.2.0](https://github.com/rudi193-cmd/Jeles/compare/v0.1.0...v0.2.0) (2026-08-03)


### Added

* port corpus_server to MCP SDK 2.0, and give it tests ([b8486a5](https://github.com/rudi193-cmd/Jeles/commit/b8486a5a9161616072531ac355a02ccbd6401e6a))


### CI

* automate releases with release-please ([0e7fa76](https://github.com/rudi193-cmd/Jeles/commit/0e7fa768e1666ee89fd5540161c709adccf57efc))
* fleet hardening — concurrency, lint cache, automerge fix ([fe6f3d2](https://github.com/rudi193-cmd/Jeles/commit/fe6f3d2cf147817efdf389d5bfd2cc9c2cdaf0b8))

## [0.1.0] — 2026-08-02

First published release. The verified-corpus organ extracted from the Ask Jeles
app into its own installable package, plus the canonical Jeles persona.

### Added
- **`jeles.corpus`** — pure, stdlib-only storage and ranked lookup of verified
  nuggets over willow-mcp's SOIL `Store` SQLite schema, with gap logging.
- **`jeles.corpus_server`** — a standalone MCP server over the corpus
  (`corpus_ask`, `corpus_search`, `corpus_get`, `corpus_list`, `corpus_put`,
  `corpus_gaps`), depending on nothing from willow-mcp.
- **`jeles.willow_mcp_client`** — best-effort, non-blocking forwarding of gaps
  into willow-mcp's fleet-wide backlog, with a retry cooldown.
- **`jeles.reactions.conflict_scan`** — the prior-art reaction: search for what
  *supersedes or refutes* a claim rather than what resembles it, and promote a
  finding only when two independent registrable domains corroborate it.
- **`jeles.reactions.search_adapter`** — the web-search edge, with `searxng`,
  `brave`, `tavily` and `ddg` backends, fail-soft to `[]`.
- **The canonical Jeles persona** and a deterministic prompt compiler, so hosts
  stop hand-carrying their own prose copies.

### Build
- **Zero runtime dependencies.** The MCP SDK moved to a `[mcp]` extra so a host
  can depend on `jeles` without inheriting a version constraint — `mcp` had been
  a hard dependency pinned `<2`, which made `pip install willow-mcp jeles`
  unresolvable.
- **The version is derived from the git tag** (`hatch-vcs`); `jeles.__version__`
  reads it back from installed package metadata, resolved lazily so
  `importlib.metadata` does not pull `socket` into a package that promises not
  to import network modules.

[0.1.0]: https://pypi.org/project/jeles/0.1.0/
