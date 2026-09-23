# Prompt changelog

Version → eval score → what changed. One entry per prompt version, append-only.

<!-- Example (Phase 4):
## placement_v3 — 2026-10-02
Restructured brand-safety into an explicit 4-criterion rubric.
MRR 0.74 → 0.82 | P@5 0.61 → 0.69 | cost/run $0.14 → $0.16
Hypothesis: v2 conflated audience mismatch with brand-safety risk.
Confirmed — v2's false positives were 70% audience-mismatch misfiles.
-->

## extraction_v1 — 2026-09-23
Sponsor extraction from one issue, `qwen3:8b` (local, $0), JSON-schema output with a verbatim `evidence` quote per sponsor.
1,360 issues in 106.5 min · bad_json 3.2% · 1.38 sponsors/issue · sponsorship wording near evidence 59.8% (LLM-independent proxy).
Failure found: news roundups read as sponsors — This Week in Fintech 2.02, Exec Sum 2.27 "sponsors"/issue ("Brex secured a $235m credit facility…"). All bad_json = lists cut off at exactly 1,500 output tokens.

## extraction_v2 — 2026-09-23
One prompt change: evidence must be the sponsorship label or the ad slot itself; companies in news, funding, deal lists, job listings or link roundups are always `editorial_mention`. (Separately, an engineering fix: output limit 1,500 → 3,000.)
bad_json 2.4% · 1.27 sponsors/issue · wording near evidence 69.0% · TWIF 2.08 → 1.40, Exec Sum 2.38 → 1.68 (after stitched re-grading).
Head-to-head on 1,290 issues: v2 dropped 266 probable-noise sponsors vs 55 possibly-real ones, and found 108 new.
Side effect: the label rule made the model **stitch** a heading on one line to a sponsor on another, so the verbatim check rejected real sponsors (Buildkite, Statsig, ParadeDB). Fixed in the checker (stitched grounding), not the prompt.
Remaining failure: bad_json now from runaway `excluded` lists ("Pump Club T-Shirts, Hoodies, Mugs…") → cap list lengths in the schema (candidate for v3).
Precision: pending human spot-check (extraction_spotcheck.csv). Claude's first pass: 33 correct / 10 not sponsors / 7 unsure of 50; with the wording gate 30 / 4 / 3 of 37.
