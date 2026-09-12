"""Jeles reactions — pure-script handlers of the shape ``(event, ctx) -> [proposed actions]``.

A *reaction* is the enforced form of what used to be prose: instead of a skill
telling an agent "you should go look at prior art," a reaction is a deterministic
script that *does* it, on an event, and hands back proposed actions for a driver
to execute. This is the Jeles-resident half of the reaction-engine design
(``willow/design/reaction-engine.md``): the network-touching, corpus-writing
reactions live here, wrapping :mod:`jeles.corpus` — never inside it — so the
corpus core stays stdlib-only and network-free.

The first reaction is :mod:`jeles.reactions.conflict_scan`: on a design claim,
search the web *for the conflict* — the superseding or refuting prior art — and
promote a finding to a nugget only when two independent sources corroborate it,
holding it as a contested gap until then.
"""

from __future__ import annotations

from . import conflict_scan

__all__ = ["conflict_scan"]
