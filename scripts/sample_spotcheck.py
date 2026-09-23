"""Phase 1.2: draw the extraction spot-check sample (roadmap: "human spot-check
of 50 random extractions", precision recorded in the dataset card).

Two strata, both seeded so the sample is reproducible:
- N_SPONSORS random extracted sponsors (grounded ones only — ungrounded
  never become pairings) → estimates **precision**
- N_EMPTY random issues where nothing was extracted → a rough check on
  **recall** (sponsors the extractor missed)

Writes data/sources/extraction_spotcheck.csv with blank `verdict` / `notes`
columns to fill in. Refuses to overwrite a sheet that already has verdicts.

    uv run python scripts/sample_spotcheck.py
"""

import csv
import json
import os
import random
import sys

EXTRACTIONS = os.path.join("data", "processed", "extractions", "extraction_v1__qwen3-8b.jsonl")
OUT = os.path.join("data", "sources", "extraction_spotcheck.csv")
N_SPONSORS, N_EMPTY, SEED = 50, 15, 20260923

COLUMNS = ["sample_id", "stratum", "publisher", "vertical", "issue_url", "sponsor_name", "domain", "evidence",
           "grounding", "confidence", "verdict", "notes"]


def main() -> int:
    if os.path.exists(OUT):
        with open(OUT, newline="", encoding="utf-8") as f:
            if any(r["verdict"].strip() for r in csv.DictReader(f)):
                print(f"{OUT} already has verdicts — not overwriting.")
                return 1

    with open(EXTRACTIONS, encoding="utf-8") as f:
        records = [json.loads(line) for line in f]
    records = [r for r in records if r["status"] == "ok"]
    sponsors = [(r, s) for r in records for s in r["sponsors"] if s["grounding"] != "none"]
    empty = [r for r in records if not r["sponsors"]]

    rng = random.Random(SEED)
    rows = []
    for i, (r, s) in enumerate(rng.sample(sponsors, min(N_SPONSORS, len(sponsors))), start=1):
        rows.append({
            "sample_id": f"S{i:02d}", "stratum": "extracted_sponsor", "publisher": r["publisher"], "vertical": r["vertical"],
            "issue_url": r["issue_url"], "sponsor_name": s["name"], "domain": s["domain"] or "", "evidence": s["evidence"],
            "grounding": s["grounding"], "confidence": s["confidence"], "verdict": "", "notes": "",
        })
    for i, r in enumerate(rng.sample(empty, min(N_EMPTY, len(empty))), start=1):
        rows.append({
            "sample_id": f"E{i:02d}", "stratum": "no_sponsor_extracted", "publisher": r["publisher"], "vertical": r["vertical"],
            "issue_url": r["issue_url"], "sponsor_name": "", "domain": "", "evidence": "", "grounding": "", "confidence": "",
            "verdict": "", "notes": "",
        })

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} rows ({N_SPONSORS} sponsors from {len(sponsors)} grounded, {N_EMPTY} empty issues from {len(empty)}) -> {OUT}")
    print("verdicts: extracted_sponsor -> correct | wrong_not_sponsor | wrong_name ; no_sponsor_extracted -> correct_empty | missed:<sponsor>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
