"""Phase 1.1: screen candidate newsletters for use as sources.

Reads data/sources/candidates.csv, writes:
- data/sources/screening.csv            one row per candidate (tracked in git)
- data/sources/screening_samples.jsonl  per-issue marker evidence, for spot-checking

Re-runs are cheap: every successful fetch is cached under data/raw/http/.

    uv run python scripts/screen_publishers.py [--only NAME ...]
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

from sponsorfit.scrape.fetch import PoliteFetcher
from sponsorfit.scrape.screen import ScreenResult, screen_publisher

SOURCES_DIR = os.path.join("data", "sources")
CANDIDATES = os.path.join(SOURCES_DIR, "candidates.csv")
OUT_CSV = os.path.join(SOURCES_DIR, "screening.csv")
OUT_SAMPLES = os.path.join(SOURCES_DIR, "screening_samples.jsonl")

COLUMNS = [
    "name", "vertical", "status", "sponsor_rate", "sponsored", "sampled", "feed_items",
    "platform", "newest_issue", "oldest_issue_in_feed", "url", "feed_url", "example", "error",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="screen only these candidate names")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    with open(CANDIDATES, newline="", encoding="utf-8") as f:
        candidates = list(csv.DictReader(f))
    if args.only:
        candidates = [c for c in candidates if c["name"] in set(args.only)]

    fetcher = PoliteFetcher()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(screen_publisher, fetcher, c["name"], c["vertical"], c["url"]): c for c in candidates}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                r = fut.result()
            except Exception as e:  # a bug in screening one site must not sink the run
                r = ScreenResult(name=c["name"], vertical=c["vertical"], url=c["url"], status="error", error=f"{type(e).__name__}: {e}")
            results.append(r)
            print(f"  {r.status:<18} {r.sponsor_rate:>4}  {r.vertical:<16} {r.name}", flush=True)

    # --only runs merge into the existing screening.csv instead of clobbering it
    previous = {}
    if args.only and os.path.exists(OUT_CSV):
        with open(OUT_CSV, newline="", encoding="utf-8") as f:
            previous = {row["name"]: row for row in csv.DictReader(f)}
    rows = dict(previous)
    for r in results:
        d = asdict(r)
        rows[r.name] = {k: d[k] for k in COLUMNS}
    ordered = sorted(rows.values(), key=lambda row: (row["vertical"], row["status"] != "pass", row["name"].lower()))

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(ordered)
    mode = "a" if args.only else "w"
    with open(OUT_SAMPLES, mode, encoding="utf-8") as f:
        for r in sorted(results, key=lambda r: r.name):
            f.write(json.dumps({"name": r.name, "status": r.status, "samples": r.samples}) + "\n")

    by_vertical = defaultdict(Counter)
    for row in ordered:
        by_vertical[row["vertical"]][row["status"]] += 1
    print(f"\n{'vertical':<16} pass / total")
    for v in sorted(by_vertical):
        c = by_vertical[v]
        print(f"{v:<16} {c['pass']:>4} / {sum(c.values())}")
    total_pass = sum(c["pass"] for c in by_vertical.values())
    print(f"{'ALL':<16} {total_pass:>4} / {len(ordered)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
