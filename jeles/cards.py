"""cards — the preloaded catalog of hosts jeles touches.

One JSON file per host under `cards/`, shipped in the wheel. See
`docs/design/host-cards.md` for why this exists and what each field means; the
short version is that `SOURCES[*].hosts` was one field answering three different
questions — *we request this*, *we emit links to this*, and *this is an XML
namespace URI* — and consumers downstream were forced to publish a citability
verdict on all 84 hosts when only 36 can ever be a result URL.

**A card holds facts about a host. It does not hold anyone's trust verdict.**
Whether `custody: community` disqualifies a link is the caller's policy, and
stays with the caller — willow-mcp is not jeles' only consumer, and a second one
should not inherit willow-mcp's opinions along with its data.

One file per host rather than one file of 84, so a bot proposing a change to one
card does not rewrite a file holding the other 83.

**There is no reachability field here, and that is deliberate.** An earlier draft
carried an `observed` block for a prober to fill. `almanac-template` already runs
this job — `link-check.yml`, daily — and its discipline is stricter: the probe is
read-only, its report becomes an *issue*, and only a decision reaches the record
through a pull request. So the only reachability state a card carries is
`status`, and `status` is set by a human merging that PR. A field a machine
silently overwrites would make every card's history unreadable and turn a
transient 403 behind CDN bot protection into a permanent claim.

Stdlib only, and no I/O at import: cards are read on first use and cached. The
package is `jeles`, whose whole point is that importing it costs nothing.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

__all__ = ["CUSTODY", "ROLES", "STATUS", "CardError", "card", "cards", "hosts", "hosts_with_role"]

#: A host's relationship to jeles. `namespace` is not a network relationship at
#: all — it is an XML namespace URI that happens to be spelled like a URL, which
#: is how `www.w3.org` was once trusted as an institution because arXiv's Atom
#: feed named it. Recording it as a role makes that class a value rather than an
#: accident.
ROLES = frozenset({"query", "citation", "namespace"})

#: Who holds editorial responsibility for the record. A judgement, but a
#: judgement about the *site* — it does not vary by consumer, which is why it
#: belongs here and a trust verdict does not.
CUSTODY = frozenset({"institutional", "community", "commercial", "aggregator"})

#: Reachability, as a *decision* rather than a measurement. Set by a human
#: merging a PR — never by a probe writing to the file. See the module docstring.
STATUS = frozenset({"live", "degraded", "retired"})

_DIR = Path(__file__).parent / "cards"


class CardError(ValueError):
    """A card that does not satisfy the schema. Raised on read, not on import."""


def _validate(data: dict, source: str) -> dict:
    def fail(msg: str):
        raise CardError(f"{source}: {msg}")

    for key in ("host", "roles", "publisher", "custody", "status"):
        if key not in data:
            fail(f"missing required field {key!r}")

    if not isinstance(data["host"], str) or not data["host"]:
        fail("host must be a non-empty string")
    if data["host"] != data["host"].lower().rstrip("."):
        fail(f"host must be lowercase with no trailing dot: {data['host']!r}")

    roles = data["roles"]
    if not isinstance(roles, list) or not roles:
        fail("roles must be a non-empty list")
    bad = sorted(set(roles) - ROLES)
    if bad:
        fail(f"unknown role(s) {bad}; expected a subset of {sorted(ROLES)}")
    if len(set(roles)) != len(roles):
        fail(f"duplicate roles: {roles}")

    if data["custody"] not in CUSTODY:
        fail(f"unknown custody {data['custody']!r}; expected one of {sorted(CUSTODY)}")
    if data["status"] not in STATUS:
        fail(f"unknown status {data['status']!r}; expected one of {sorted(STATUS)}")

    j = data.get("jurisdiction")
    if j is not None:
        if not isinstance(j, dict) or "scope" not in j:
            fail("jurisdiction must be an object with a scope")
        if j["scope"] == "national" and not j.get("country"):
            fail("national jurisdiction needs a country")
        if j["scope"] != "national" and j.get("country"):
            fail(f"{j['scope']} jurisdiction must not name a country")
    return data


@lru_cache(maxsize=1)
def cards() -> dict[str, dict]:
    """Every card, keyed by host. Read once, cached.

    The filename is checked against the `host` field rather than trusted: they
    are two statements of the same fact, and a rename that edits one and not the
    other would leave a card reachable under a name it does not claim.
    """
    out: dict[str, dict] = {}
    for path in sorted(_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise CardError(f"{path.name}: unreadable ({e})") from e
        _validate(data, path.name)
        if path.stem != data["host"]:
            raise CardError(f"{path.name}: filename says {path.stem!r}, card says {data['host']!r}")
        out[data["host"]] = data
    return out


def card(host: str) -> dict | None:
    """One card, or None. Case- and trailing-dot-insensitive, because that is
    how hostnames arrive from a parsed URL."""
    return cards().get((host or "").strip().lower().rstrip("."))


def hosts() -> list[str]:
    return sorted(cards())


def hosts_with_role(role: str) -> list[str]:
    """Hosts holding `role`.

    `hosts_with_role("citation")` is the set a trust policy has to decide, and
    it is the point of the whole file: 36 of 84, not 84 of 84.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(ROLES)}")
    return sorted(h for h, c in cards().items() if role in c["roles"])
