# Knowledge corpus seed

Structured research JSON files that feed the jeles verified-corpus store.
Each file contains pairs (claim + explanation), evidence (source URLs),
and edges (cross-domain relationships).

## Scale

- 74 source files across ~30 domains
- **1028** raw pair-shaped entries in the JSON (72 files carry a `pairs` list;
  2 are plain lists of adversarial *challenge* records, not pair sets),
  composing to **968** question/answer nuggets — 149 commons + 819 asserted.
  The remaining 36 entries share the `pairs` key without the question/answer
  shape (reasoning about the corpus rounds, not claims in it) and compose to
  nothing. Measured 2026-09-12 with `jeles-seed --dry-run`; see
  `tests/test_corpus_counts.py`.
- 2105 evidence entries with real source URLs
- 3 rounds of adversarial verification (fact-check, steel-man, contradiction challenge)

## Load into a jeles store

```bash
pip install -e .
python corpus/compose.py --origin-prefix=seed corpus/seed/*.json
```

This writes each pair as an `asserted` nugget (machine-proposed, not
human-verified). The compose script requires `jeles.corpus` to be
importable.

## Domains covered

| Category | Domains |
|----------|---------|
| Core science | Forces, heat/light/sound, electricity, materials, food/body, weather, measurement, money, transportation, digital |
| AI research | RAG, agent memory, KG grounding, distillation, multi-agent |
| Modern infrastructure | Internet, energy, food systems, healthcare, cities |
| People (contemporary) | AI pioneers, tech power, ethics/safety, scandals, open source, scientists, whistleblowers, infrastructure, media, rights |
| People (historical) | Scientists, political power, liberation, thinkers, artists |
| Economics, law, environment, education, war | R8 batch |
| Media, health, labor | R9 batch |
| Cross-domain wiring | Power-accountability, ethics-creation, credit-erasure, infrastructure-control, and more |
| Adversarial passes | Fact-check, steel-man, contradiction challenge (3 rounds) |

## Provenance

All entries are `asserted` (machine-proposed from public sources). None
are `human`-verified. The adversarial passes correct factual errors and
surface contradictions, but they are machine-generated corrections of
machine-generated claims.
