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

## The Idea-Id commit-trailer convention

A commit that lands an idea recorded in docs/ideas.md carries an
`Idea-Id: <corpus>-ideas-<num>` git trailer (add `Idea-Status: partial` when a
commit only partly lands it). It is the durable join key willow-reconciler
reads; a wrong id is worse than no id, so never type one by hand:

    reconciler id --repo ./ --doc docs/ideas.md --grep "words from the item"
    reconciler install-hook --repo ./       # derives it from a branch named idea-NN

(`./`, not `.`: a `--repo` with no slash is read as a bare repo name, not a
path, and `.` has none.)

`.github/workflows/trailers.yml` runs `reconciler verify` on every PR and fails
on a trailer that names an item the doc does not contain.
