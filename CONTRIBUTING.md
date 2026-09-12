# Contributing

## Receipts, not claims

Run the suite exactly as CI's no-extras job does, and quote the command and its
result in the PR:

```sh
python -m pytest tests/ -q
```

CI's matrix job runs the same command under `coverage` with an 80% floor, and
the lint job runs the same two tools every push must pass:

```sh
ruff check jeles tests
bandit -r jeles -ll -q
```

Both are pinned in `.github/workflows/tests.yml` (`ruff==0.15.0`); a clean
local run of that command is the receipt. The suite is network-free by
construction and passes on a bare `pip install -e . pytest` as well as on
`pip install -e ".[dev]"` — see README's *Tests* section for what each install
shape skips.

## Commits

Conventional commits. `release-please-config.json` decides what a type does:
`feat`, `fix`, `perf`, `refactor`, `build`, `deps` and `security` each cut a
release on their own; `docs`, `test`, `ci` and `chore` are hidden and cut
nothing. The reasoning lives beside the setting, in that file's
`$comment-hidden-rule` and `$comment-what-cuts-a-release`. `pr-title.yml` holds
a PR whose title would cut a release its commits would not.

These rules are the fleet's, published by `willow-reconciler` as
`reconciler conventions --json` and held to in `tests/test_fleet_conventions.py`,
which reads them from `tests/fleet_conventions.json` rather than restating them.
