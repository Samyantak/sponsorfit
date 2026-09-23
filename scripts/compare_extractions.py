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
from sponsorfit.corpus.runs import load_run
from sponsorfit.scrape.fetch import PoliteFetcher

WATCH = ["This Week in Fintech", "Exec Sum", "Fintech Is Femme", "Tim Ferriss Blog", "Out-of-Pocket"]


def summarize(version: str, site_of: dict, fetcher: PoliteFetcher) -> dict:
    records = list(load_run(version, fetcher=fetcher).values())
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
    if len(versions) == 2:
        head_to_head(*versions, site_of, fetcher)
    return 0


def head_to_head(old: str, new: str, site_of: dict, fetcher: PoliteFetcher) -> None:
    """Sponsors `old` found that `new` dropped, split by whether sponsorship
    wording sits near the evidence: dropped *with* a signal ≈ probable recall
    loss; dropped *without* ≈ probable precision gain. Only issues both runs
    parsed successfully count, so a bad_json isn't mistaken for a drop."""
    a, b = load_run(old, fetcher=fetcher), load_run(new, fetcher=fetcher)
    shared = [i for i in a if i in b and a[i]["status"] == "ok" and b[i]["status"] == "ok"]
    lost_signal, lost_plain, gained = Counter(), Counter(), 0
    examples = []
    for i in shared:
        names_b = {s["name"].lower() for s in b[i]["sponsors"]}
        dropped = [s for s in a[i]["sponsors"] if s["grounding"] != "none" and s["name"].lower() not in names_b]
        gained += sum(1 for s in b[i]["sponsors"] if s["name"].lower() not in {x["name"].lower() for x in a[i]["sponsors"]})
        if not dropped:
            continue
        issue = prepare_issue(fetcher.get(a[i]["issue_url"]).text, site_of[a[i]["publisher_id"]])
        for s in dropped:
            if sponsorship_signal_nearby(s["evidence"], issue):
                lost_signal[a[i]["publisher"]] += 1
                examples.append((a[i]["publisher"], s["name"], s["evidence"][:80]))
            else:
                lost_plain[a[i]["publisher"]] += 1
    print(f"\nhead-to-head on {len(shared)} issues both runs parsed:")
    print(f"  dropped by {new}, signal nearby (probable recall loss):     {sum(lost_signal.values())}")
    print(f"  dropped by {new}, no signal nearby (probable precision gain): {sum(lost_plain.values())}")
    print(f"  newly found by {new}:                                        {gained}")
    print(f"  recall-loss hotspots: {lost_signal.most_common(6)}")
    for p, name, ev in examples[:8]:
        print(f"    {p[:22]:<22} {name[:20]:<20} {ev}")


if __name__ == "__main__":
    sys.exit(main())
