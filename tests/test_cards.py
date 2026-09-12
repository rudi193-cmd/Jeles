"""The host catalog, kept answerable to the code it describes.

`docs/design/host-cards.md` is the why. This file is the part that does not rot:
a card set curated by hand drifts from the registry the moment anyone edits
`SOURCES`, which is the exact failure `_TRUSTED_SUFFIXES` had downstream — it
carried a comment claiming to cover a file that was not even in that repository.

The load-bearing test here is `test_every_role_the_code_proves_is_on_the_card`.
It re-derives roles from the AST of `sources.py` and asserts the card carries at
least what the code proves. **At least, not exactly**: the derivation reads
string literals, so it sees `https://doi.org/{doi}` and is blind to
`item.get("url")`. A curator may therefore add a `citation` role the AST cannot
see, and must never be able to remove one it can.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from jeles import cards as C
from jeles import sources

_DIR = Path(C.__file__).parent / "cards"
_NSPAT = re.compile(r"/zing/|xmlns|/Atom|/ns/")


# ── The set matches the registry ─────────────────────────────────────────────

def test_every_registered_host_has_a_card():
    missing = sorted(set(sources.registered_hosts()) - set(C.cards()))
    assert not missing, (
        f"hosts in SOURCES with no card: {missing}. Add jeles/cards/<host>.json "
        "— a host with no card is a host nothing downstream can reason about.")


def test_no_card_describes_a_host_jeles_no_longer_queries():
    """A leftover card is a stale opinion that reads as a live one — the same
    failure the exclusion-list check catches downstream."""
    stale = sorted(set(C.cards()) - set(sources.registered_hosts()))
    assert not stale, f"cards for hosts no longer in SOURCES: {stale}"


def test_the_catalog_is_not_accidentally_empty():
    """Every assertion above is vacuously true against an empty directory, and
    `cards()` globs a directory — a packaging mistake that ships no JSON would
    make this whole file green while the feature is entirely absent."""
    assert len(C.cards()) > 50, f"only {len(C.cards())} cards loaded from {_DIR}"


# ── The cards agree with the code ────────────────────────────────────────────

def _strings(node) -> list[str]:
    out = []
    for x in ast.walk(node):
        if isinstance(x, ast.Constant) and isinstance(x.value, str):
            out.append(x.value)
        elif isinstance(x, ast.JoinedStr):
            out.append("".join(p.value for p in x.values
                               if isinstance(p, ast.Constant) and isinstance(p.value, str)))
    return out


def _roles_from_source() -> dict[str, set[str]]:
    """Roles the code *proves*, by AST. A lower bound — see the module docstring."""
    tree = ast.parse(Path(sources.__file__).read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    roles: dict[str, set[str]] = {}
    for sid, cfg in sources.SOURCES.items():
        fn = funcs.get(cfg.get("fn_name") or f"search_{sid}")
        if not fn:
            continue
        emitted, names = [], set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_result":
                for kw in node.keywords:
                    if kw.arg in ("url", "rid"):
                        emitted += _strings(kw.value)
                        if isinstance(kw.value, ast.Name):
                            names.add(kw.value.id)
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and {t.id for t in node.targets
                                                 if isinstance(t, ast.Name)} & names:
                emitted += _strings(node.value)
        emitted = set(emitted)
        for host in (cfg.get("hosts") or []):
            bucket = roles.setdefault(host, set())
            for text in _strings(fn):
                if host not in text:
                    continue
                if _NSPAT.search(text):
                    bucket.add("namespace")
                elif text in emitted:
                    bucket.add("citation")
                else:
                    bucket.add("query")
    return roles


def test_every_role_the_code_proves_is_on_the_card():
    """Subset, in the one direction that is safe.

    A curator adding `citation` to a host whose URL is built at runtime is
    correct and this test must allow it. A curator *dropping* `citation` from a
    host the code demonstrably emits is how a trust policy stops being asked
    about a real destination, and this test must refuse it.
    """
    derived = _roles_from_source()
    lost = {
        host: sorted(want - set(C.cards()[host]["roles"]))
        for host, want in derived.items()
        if host in C.cards() and want - set(C.cards()[host]["roles"])
    }
    assert not lost, (
        "the code proves these roles and the cards do not carry them: "
        f"{lost}. Roles may be added by a curator (runtime-built URLs are "
        "invisible to the AST) but never removed below what sources.py shows.")


def test_the_derivation_still_finds_something():
    """Guards the test above, which passes trivially if the AST walk silently
    stops matching — a rename of `_result` would do it."""
    derived = _roles_from_source()
    assert len(derived) > 50, f"AST derivation found only {len(derived)} hosts"
    assert any("citation" in v for v in derived.values())
    assert any("query" in v for v in derived.values())


def test_the_string_walk_catches_plain_and_f_string_literals():
    """Planted: `_strings` is the primitive every role below is derived
    through. A plain literal, the constant prefix of an f-string, and a literal
    nested inside a call must all come back; a walk that lost f-strings would
    lose every `https://host/{id}` citation URL in `sources.py`."""
    tree = ast.parse(
        "def f(x):\n"
        "    a = 'https://plain.example.org/item'\n"
        "    b = f'https://fmt.example.org/{x}'\n"
        "    return g('nested.example.org', a, b)\n"
    )
    # Set equality, not `"https://…" in found`: CodeQL reads a URL literal on
    # the left of `in` as an incomplete URL sanitization (py/incomplete-url-
    # substring-sanitization, high) and turned the PR red for it. Equality is
    # the stronger assertion anyway — nothing else may be in the walk.
    assert set(_strings(tree)) == {
        "https://plain.example.org/item",
        "https://fmt.example.org/",      # the f-string's constant prefix
        "nested.example.org",
    }


def test_the_role_derivation_catches_each_planted_role(tmp_path, monkeypatch):
    """Planted: a synthetic `sources.py` whose one function proves all three
    roles at once — a URL it emits through `_result(url=...)` (citation), a
    host it only queries (query), and an XML namespace URI (namespace). The
    derivation must find exactly those, so a change that stopped following
    `_result` keywords or assignments to their names would fail here, not
    silently shrink the lower bound the card test relies on."""
    fake = tmp_path / "sources.py"
    fake.write_text(
        "def search_planted(q, n):\n"
        "    ns = 'http://www.loc.gov/zing/srw/'\n"
        "    _get(f'https://query.example.net/search?q={q}')\n"
        "    link = 'https://cite.example.org/item/1'\n"
        "    return [_result(title='t', url=link)]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sources, "__file__", str(fake))
    monkeypatch.setattr(sources, "SOURCES", {
        "planted": {"hosts": ["cite.example.org", "query.example.net", "www.loc.gov"]},
    })
    # Hosts are matched as substrings of each literal, so the three are chosen
    # not to contain one another — or the query host would also be "cited".
    assert _roles_from_source() == {
        "cite.example.org": {"citation"},
        "query.example.net": {"query"},
        "www.loc.gov": {"namespace"},
    }


def test_the_namespace_role_is_carried_where_the_code_shows_one():
    """`www.loc.gov` is declared by `gallica` and `ndl` purely as the SRW/Zing
    XML namespace URI. That is a schema identifier, not a server, and it is the
    same mechanism that once got `www.w3.org` trusted as an institution off
    arXiv's Atom feed. The role exists so the class is a value, not an accident.
    """
    assert "namespace" in C.card("www.loc.gov")["roles"]
    assert C.hosts_with_role("namespace"), "no namespace role survived the migration"


def test_a_namespace_only_host_owes_no_citability_verdict(tmp_path, monkeypatch):
    """The operative half of the §7.1 decision, and the reason `namespace` is a
    role rather than a deleted row.

    Keeping the host is only useful if the value *does* something. A card whose
    roles are `["namespace"]` alone describes a schema identifier: nothing
    contacts it and nothing links to it, so it must not appear in the set a
    consumer's trust policy has to decide. If it leaked into `citation`, keeping
    these rows would be strictly worse than dropping them — it would manufacture
    the exact obligation this catalog exists to remove.

    Checked against a synthetic card because no real host is namespace-only
    today (`www.loc.gov` is `namespace` *and* `query`). Untested-until-it-
    happens is how the `w3.org` class survived its first fix.
    """
    monkeypatch.setattr(C, "_DIR", tmp_path)
    C.cards.cache_clear()
    (tmp_path / "schema.example.org.json").write_text(json.dumps({
        "host": "schema.example.org", "roles": ["namespace"],
        "publisher": "Example Standards Body", "custody": "institutional",
        "status": "live"}))
    try:
        assert C.hosts_with_role("namespace") == ["schema.example.org"]
        assert C.hosts_with_role("citation") == []
        assert C.hosts_with_role("query") == []
    finally:
        C.cards.cache_clear()


# ── Schema ───────────────────────────────────────────────────────────────────

def test_every_card_validates_and_the_filename_matches_its_host():
    # `cards()` validates on read, so simply loading is the assertion. Kept
    # explicit because a future lazy/partial loader would make that implicit.
    loaded = C.cards()
    for host, data in loaded.items():
        assert (_DIR / f"{host}.json").exists()
        assert data["host"] == host


@pytest.mark.parametrize("field,allowed", [
    ("custody", C.CUSTODY), ("status", C.STATUS)])
def test_enum_fields_stay_inside_their_enum(field, allowed):
    bad = {h: c[field] for h, c in C.cards().items() if c[field] not in allowed}
    assert not bad, f"{field} outside {sorted(allowed)}: {bad}"


def test_no_card_is_missing_a_publisher():
    """`publisher` is the reference piece. Blank is a skipped question wearing
    the shape of an answered one — the same reasoning as the Rule 2 table check
    in willow-mcp's fleet-versioning tests."""
    blank = sorted(h for h, c in C.cards().items() if not (c.get("publisher") or "").strip())
    assert not blank, f"cards with no publisher: {blank}"


def test_no_card_carries_a_measured_reachability_field():
    """Reachability is a decision on a card, not a measurement in one.

    An earlier draft gave every card an `observed` block for a prober to fill.
    `almanac-template` already runs that job (`link-check.yml`, daily) under a
    stricter rule: the probe is read-only, its report becomes an issue, and only
    a decision reaches the record through a PR. Two reasons that rule wins here
    too — a field a machine silently overwrites makes a card's git history
    unreadable, and `check_links.py`'s hardest-won lesson is that *blocked is not
    dead*, so a transient 403 behind CDN bot protection must never become a
    stored claim.

    `status` carries the decision. This test keeps the measurement out.
    """
    banned = {"observed", "reachable", "http_status", "last_checked", "checked",
              "etag", "fingerprint"}
    for host, c in C.cards().items():
        leaked = banned & set(c)
        assert not leaked, (
            f"{host}: {sorted(leaked)} is a probe measurement. Reachability "
            "belongs in a report and an issue; only `status` lands on the card, "
            "set by a human merging a PR. See docs/design/host-cards.md §6.2.")


def test_a_bad_card_is_refused_rather_than_half_read(tmp_path, monkeypatch):
    """Fail-closed on read. A card with an unknown custody value must raise, not
    load with a field a consumer will branch on and get wrong."""
    monkeypatch.setattr(C, "_DIR", tmp_path)
    C.cards.cache_clear()
    (tmp_path / "example.org.json").write_text(json.dumps({
        "host": "example.org", "roles": ["query"], "publisher": "Example",
        "custody": "totally-made-up", "status": "live"}))
    with pytest.raises(C.CardError, match="unknown custody"):
        C.cards()
    C.cards.cache_clear()


def test_a_filename_that_disagrees_with_its_host_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "_DIR", tmp_path)
    C.cards.cache_clear()
    (tmp_path / "wrong-name.json").write_text(json.dumps({
        "host": "example.org", "roles": ["query"], "publisher": "Example",
        "custody": "commercial", "status": "live"}))
    with pytest.raises(C.CardError, match="filename says"):
        C.cards()
    C.cards.cache_clear()


# ── The point of the exercise ────────────────────────────────────────────────

def test_the_citation_set_is_much_smaller_than_the_host_set():
    """The measurable win, pinned. A consumer's trust policy owes a verdict on
    citation hosts only; today that is 36 of 84. If a change makes those numbers
    equal, the roles have stopped discriminating and the catalog has quietly
    become the flat host list it replaced."""
    citation = C.hosts_with_role("citation")
    assert 0 < len(citation) < len(C.cards()) * 0.75, (
        f"{len(citation)} of {len(C.cards())} hosts are citation-capable — if "
        "that ratio approaches 1 the role field has stopped doing any work")


def test_no_host_reaches_a_trust_verdict_from_this_package():
    """The boundary, asserted rather than described.

    A card holds facts about a host; the verdict is the consumer's. If a
    `trusted`/`citable`/`believable` field ever appears here, jeles has taken a
    position it cannot hold for every consumer — and willow-mcp's policy would
    silently become jeles' policy for everyone downstream.
    """
    banned = {"trusted", "citable", "believable", "trust", "trusted_only"}
    for host, c in C.cards().items():
        leaked = banned & set(c)
        assert not leaked, (
            f"{host}: {sorted(leaked)} is a verdict, not a fact about the host. "
            "See docs/design/host-cards.md §2 — consumers keep their own policy.")
