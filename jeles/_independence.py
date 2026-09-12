"""_independence — the one definition of "when two pieces of evidence are two sources".

Two modules in this package need that answer and read different evidence to get
it. :mod:`jeles.reactions.conflict_scan` counts the distinct *sites* a web search
returned, to decide whether prior art is corroborated. :mod:`jeles.verify` counts
the distinct *institutions* behind a claim's citations, to decide whether the
claim is. Different evidence, different consequence — but the same underlying
question, and the same two-source bar.

`_egress` already recorded what happens when a question like that gets answered
once per call site: *a rule written out three times is a rule enforced nowhere*.
The site-identity rule here is the case in point. conflict_scan's copy had
accreted three corrections that a fresh second copy would have started without —
two-label public suffixes, address literals, and dotless hosts — each of which
had produced a wrong "corroborated" before it was fixed. Writing them out again
in `verify` would have re-opened all three.

So the identity function and the bar live here, once. Each caller supplies only
what genuinely differs: which evidence it reads, and what it does with the count.

Stdlib only, and nothing happens at import — no module here may cost anything to
load (`tests/test_import_purity.py`). `urllib.parse` is pure string work; note
that it is emphatically *not* `urllib.request`, which is the one this package
keeps behind :mod:`jeles._egress`.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

__all__ = ["MIN_INDEPENDENT_SOURCES", "registrable_domain"]

#: The bar: evidence is corroborated only when at least this many *distinct*
#: sources back it. Two pages on one site, or two records from one institution,
#: are one source, not two.
#:
#: Note what this is not. The constitution's *Independent Witness* requires
#: demonstrated failure-mode divergence — two distinct domains can still be one
#: actor who bought both, and two institutional labels can be one consortium.
#: This is a cheap heuristic, deliberately weaker and deliberately named apart,
#: so nothing built on it borrows authority it has not earned.
MIN_INDEPENDENT_SOURCES = 2

# Two-label public suffixes: without these, foo.co.uk and bar.co.uk both reduce
# to "co.uk" and read as one source. Small, common set — not a full PSL.
_TWO_LABEL_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "co.jp",
        "or.jp",
        "ne.jp",
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "com.br",
        "co.in",
        "co.za",
    }
)

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def registrable_domain(url: str) -> str:
    """Registrable-ish domain of a URL, for the independence test.

    Coarse on purpose: strip scheme, ``www.``, and path; keep the last two
    labels (``foo.github.io`` -> ``github.io``). Good enough to tell "two
    different sites" from "two pages on one site," which is all the two-source
    rule needs. It never raises — an unparseable URL yields ``""``.
    """
    try:
        host = urlparse(url if "://" in url else f"//{url}", scheme="https").netloc.lower()
    except Exception:
        return ""
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    labels = [x for x in host.split(".") if x]
    # A usable source has a dotted domain; a dotless/garbage host is no source.
    if len(labels) < 2:
        return ""
    # A bare IP is not a citable source, and taking its last two labels is
    # actively wrong: 93.184.216.34 and 93.184.216.99 became two "independent"
    # sources, while 1.2.3.4 and 9.9.3.4 both collapsed to "3.4". Neither
    # reading is defensible, so an address literal witnesses nothing.
    if _IPV4_RE.match(host):
        return ""
    last2 = ".".join(labels[-2:])
    # Keep three labels when the last two are a known two-label public suffix,
    # so foo.co.uk and bar.co.uk stay distinct sources.
    if last2 in _TWO_LABEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last2
