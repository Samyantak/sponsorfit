"""Phase 1.2: draw the extraction spot-check sample (roadmap: "human spot-check
of 50 random extractions", precision recorded in the dataset card).

Two strata, seeded so the sample is reproducible:
- N_SPONSORS random grounded sponsors (grounding re-checked with current
  rules) → precision. Each row records `passes_gate` (sponsorship wording at
  or near the evidence), so the same verdicts also measure the precision of
  the gated output and how many correct sponsors the gate would discard.
- N_EMPTY random issues where nothing was extracted → a rough recall check.

Writes data/sources/extraction_spotcheck.csv (verdicts get filled in there
and on the review page). Refuses to overwrite a sheet that has verdicts.

    uv run python scripts/sample_spotcheck.py [--version extraction_v2]
"""

import argparse
import csv
import os
import random
import sys

from sponsorfit.corpus.extract import prepare_issue, sponsorship_signal_nearby
from sponsorfit.corpus.runs import load_run
from sponsorfit.scrape.fetch import PoliteFetcher

OUT = os.path.join("data", "sources", "extraction_spotcheck.csv")
N_SPONSORS, N_EMPTY, SEED = 50, 15, 20260923

COLUMNS = ["sample_id", "stratum", "version", "publisher", "publisher_id", "vertical", "issue_id", "issue_url",
           "sponsor_name", "domain", "evidence", "grounding", "confidence", "passes_gate",
           "claude_verdict", "claude_note", "human_verdict", "human_note"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="extraction_v2")
    args = ap.parse_args()

    if os.path.exists(OUT):
        with open(OUT, newline="", encoding="utf-8") as f:
            if any((r.get("human_verdict") or r.get("claude_verdict") or "").strip() for r in csv.DictReader(f)):
                print(f"{OUT} already has verdicts — not overwriting.")
                return 1

    fetcher = PoliteFetcher()
    with open(os.path.join("data", "sources", "publishers.csv"), newline="", encoding="utf-8") as f:
        site_of = {p["id"]: p["url"] for p in csv.DictReader(f)}
    records = [r for r in load_run(args.version, fetcher=fetcher).values() if r["status"] == "ok"]
    records.sort(key=lambda r: r["issue_id"])  # stable order → reproducible sample
    sponsors = [(r, s) for r in records for s in r["sponsors"] if s["grounding"] != "none"]
    empty = [r for r in records if not r["sponsors"]]

    rng = random.Random(SEED)
    rows = []
    for i, (r, s) in enumerate(rng.sample(sponsors, min(N_SPONSORS, len(sponsors))), start=1):
        issue = prepare_issue(fetcher.get(r["issue_url"]).text, site_of[r["publisher_id"]])
        rows.append(_row(f"S{i:02d}", "extracted_sponsor", args.version, r) | {
            "sponsor_name": s["name"], "domain": s["domain"] or "", "evidence": s["evidence"],
            "grounding": s["grounding"], "confidence": s["confidence"],
            "passes_gate": "yes" if sponsorship_signal_nearby(s["evidence"], issue) else "no",
        })
    for i, r in enumerate(rng.sample(empty, min(N_EMPTY, len(empty))), start=1):
        rows.append(_row(f"E{i:02d}", "no_sponsor_extracted", args.version, r))

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    gated = sum(r.get("passes_gate") == "yes" for r in rows)
    print(f"{len(rows)} rows -> {OUT}: {N_SPONSORS} of {len(sponsors)} grounded sponsors ({gated} pass the gate), "
          f"{N_EMPTY} of {len(empty)} empty issues")
    return 0


def _row(sample_id: str, stratum: str, version: str, r: dict) -> dict:
    return {c: "" for c in COLUMNS} | {
        "sample_id": sample_id, "stratum": stratum, "version": version, "publisher": r["publisher"],
        "publisher_id": r["publisher_id"], "vertical": r["vertical"], "issue_id": r["issue_id"], "issue_url": r["issue_url"],
    }


if __name__ == "__main__":
    sys.exit(main())
