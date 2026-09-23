"""Compare extraction runs on LLM-independent proxies, before any human review.

For each run (data/processed/extractions/<version>__qwen3-8b.jsonl):
failure rate, sponsors per issue, evidence grounding, and the share of
grounded sponsors with sponsorship wording at or near their evidence
(`sponsorship_signal_nearby`). Also per-publisher sponsors/issue for the
news-roundup newsletters where v1 over-extracted.

These are proxies, not precision — precision comes from the human
spot-check — but they're cheap enough to run on every prompt version.

    uv run python scripts/compare_extractions.py extraction_v1 extraction_v2
"""

import csv
import json
import os
import sys
from collections import Counter, defaultdict

from sponsorfit.corpus.extract import prepare_issue, sponsorship_signal_nearby
from sponsorfit.scrape.fetch import PoliteFetcher

MODEL_SLUG = "qwen3-8b"
WATCH = ["This Week in Fintech", "Exec Sum", "Fintech Is Femme", "Tim Ferriss Blog", "Out-of-Pocket"]


def summarize(version: str, site_of: dict, fetcher: PoliteFetcher) -> dict:
    path = os.path.join("data", "processed", "extractions", f"{version}__{MODEL_SLUG}.jsonl")
    with open(path, encoding="utf-8") as f:
        records = [json.loads(line) for line in f]
    status = Counter(r["status"] for r in records)
    grounded, signal = 0, 0
    per_pub = defaultdict(lambda: [0, 0])  # publisher -> [issues, sponsors]
    for r in records:
        per_pub[r["publisher"]][0] += 1
        good = [s for s in r["sponsors"] if s["grounding"] != "none"]
        per_pub[r["publisher"]][1] += len(good)
        if not good:
            continue
        issue = prepare_issue(fetcher.get(r["issue_url"]).text, site_of[r["publisher_id"]])  # cache hit
        for s in good:
            grounded += 1
            signal += sponsorship_signal_nearby(s["evidence"], issue)
    sponsors = sum(len(r["sponsors"]) for r in records)
    return {
        "issues": len(records),
        "bad_json": status.get("bad_json", 0),
        "with_sponsor": sum(1 for r in records if r["sponsors"]),
        "sponsors": sponsors,
        "ungrounded": sponsors - grounded,
        "grounded": grounded,
        "signal_nearby": signal,
        "per_pub": {p: round(s / n, 2) for p, (n, s) in per_pub.items()},
    }


def main() -> int:
    versions = sys.argv[1:] or ["extraction_v1", "extraction_v2"]
    with open(os.path.join("data", "sources", "publishers.csv"), newline="", encoding="utf-8") as f:
        site_of = {p["id"]: p["url"] for p in csv.DictReader(f)}
    fetcher = PoliteFetcher()
    runs = {v: summarize(v, site_of, fetcher) for v in versions}

    pct = lambda a, b: f"{100 * a / b:.1f}%" if b else "-"
    rows = [
        ("issues", lambda s: s["issues"]),
        ("bad_json", lambda s: f"{s['bad_json']} ({pct(s['bad_json'], s['issues'])})"),
        ("issues with >=1 sponsor", lambda s: f"{s['with_sponsor']} ({pct(s['with_sponsor'], s['issues'])})"),
        ("sponsors / issue", lambda s: f"{s['sponsors'] / s['issues']:.2f}"),
        ("ungrounded evidence", lambda s: f"{s['ungrounded']} ({pct(s['ungrounded'], s['sponsors'])})"),
        ("signal near evidence", lambda s: f"{s['signal_nearby']}/{s['grounded']} ({pct(s['signal_nearby'], s['grounded'])})"),
    ]
    print(f"{'':<26}" + "".join(f"{v:>24}" for v in versions))
    for label, fn in rows:
        print(f"{label:<26}" + "".join(f"{fn(runs[v])!s:>24}" for v in versions))
    print("\nsponsors / issue, news-heavy publishers:")
    for p in WATCH:
        print(f"  {p:<24}" + "".join(f"{runs[v]['per_pub'].get(p, '-')!s:>24}" for v in versions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
