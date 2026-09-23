"""Phase 1.1 final step: screening.csv + human review → data/sources/publishers.csv.

A publisher is in the corpus iff the screener found a sponsor signal, the
human spot-check (review.csv) says "keep", and its vertical still has at least
MIN_PER_VERTICAL kept publishers — hard negatives need same-vertical
publishers, so a 1–2 publisher vertical can't be evaluated.

    uv run python scripts/build_publisher_list.py
"""

import csv
import os
import sys
from collections import Counter

SOURCES_DIR = os.path.join("data", "sources")
MIN_PER_VERTICAL = 5
COLUMNS = ["id", "name", "vertical", "url", "platform", "feed_url", "screen_sponsor_rate", "evidence", "notes"]


def _read(name: str) -> list[dict]:
    with open(os.path.join(SOURCES_DIR, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> int:
    screening = {r["name"]: r for r in _read("screening.csv")}
    review = {r["name"]: r for r in _read("review.csv")}

    unreviewed = [n for n, r in screening.items() if r["status"] == "pass" and n not in review]
    if unreviewed:
        print(f"Screened-in but not yet reviewed — add verdicts to review.csv first: {unreviewed}")
        return 1

    kept = [screening[n] | {"review": review[n]} for n in review if review[n]["verdict"] == "keep"]
    per_vertical = Counter(r["vertical"] for r in kept)
    dropped = {v: c for v, c in per_vertical.items() if c < MIN_PER_VERTICAL}
    final = sorted((r for r in kept if r["vertical"] not in dropped), key=lambda r: (r["vertical"], r["name"].lower()))

    rows = []
    for i, r in enumerate(final, start=1):
        rows.append(
            {
                "id": f"pub_{i:03d}",
                "name": r["name"],
                "vertical": r["vertical"],
                "url": r["url"],
                "platform": r["platform"],
                "feed_url": r["feed_url"],
                "screen_sponsor_rate": r["sponsor_rate"],
                "evidence": r["review"]["evidence"],
                "notes": r["review"]["notes"],
            }
        )
    with open(os.path.join(SOURCES_DIR, "publishers.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    final_counts = Counter(r["vertical"] for r in rows)
    print(f"{len(rows)} publishers across {len(final_counts)} verticals -> data/sources/publishers.csv")
    for v in sorted(final_counts):
        print(f"  {v:<18} {final_counts[v]}")
    for v, c in sorted(dropped.items()):
        print(f"  dropped: {v} ({c} kept < {MIN_PER_VERTICAL})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
