"""Phase 1.2 step A: collect a date-spread sample of issues per publisher.

Reads data/sources/publishers.csv. For each publisher: enumerate the archive
(deepest source per platform), pick ISSUES_PER_PUBLISHER spread evenly over
[SINCE, now], fetch each page (cached raw under data/raw/http/), and take the
publish date from the page itself where it has one.

Writes data/sources/issues.csv — the manifest step B extracts sponsors from.

    uv run python scripts/collect_archive.py [--only NAME ...] [--workers N]
"""

import argparse
import csv
import os
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sponsorfit.scrape.archive import enumerate_issues, published_date_from_html, select_issues
from sponsorfit.scrape.fetch import CrawlDelayTooLong, PoliteFetcher, RobotsDisallowed

SOURCES_DIR = os.path.join("data", "sources")
PUBLISHERS = os.path.join(SOURCES_DIR, "publishers.csv")
OUT = os.path.join(SOURCES_DIR, "issues.csv")

ISSUES_PER_PUBLISHER = 40
MAX_REFILL_FETCHES = 150  # extra page reads allowed per publisher when sitemap dates mislead
SINCE = datetime(2025, 1, 1, tzinfo=timezone.utc)

COLUMNS = ["issue_id", "publisher_id", "publisher", "vertical", "issue_url", "published", "date_source", "audience", "discovered_via", "status"]


def collect(fetcher: PoliteFetcher, pub: dict) -> list[dict]:
    try:
        _, found = enumerate_issues(fetcher, pub["url"], SINCE)
    except (RobotsDisallowed, CrawlDelayTooLong) as e:
        return [_row(pub, 0, None, status=f"enumerate_failed:{type(e).__name__}")]
    chosen = select_issues(found, ISSUES_PER_PUBLISHER, SINCE)
    checked = [_check(fetcher, i) for i in chosen]

    # Sitemap <lastmod> can be wildly wrong (Health Tech Nerds, Wealth Daily),
    # so a sitemap-sourced sample can land mostly outside the window. Refill
    # from a seeded random draw of the rest, verifying each page's real date.
    ok = sum(c[0] == "ok" for c in checked)
    if ok < ISSUES_PER_PUBLISHER and any(i.source == "sitemap" for i in chosen):
        tried = {i.link for i in chosen}
        pool = [i for i in found if i.link not in tried]
        random.Random(pub["id"]).shuffle(pool)
        for issue in pool[:MAX_REFILL_FETCHES]:
            if ok >= ISSUES_PER_PUBLISHER:
                break
            result = _check(fetcher, issue)
            if result[0] == "ok":
                checked.append(result)
                ok += 1

    checked.sort(key=lambda c: c[1] or datetime.min.replace(tzinfo=timezone.utc))
    return [_row(pub, n, issue, published=published, date_source=src, status=status)
            for n, (status, published, src, issue) in enumerate(checked, start=1)]


def _check(fetcher: PoliteFetcher, issue) -> tuple:
    """(status, published, date_source, issue) after reading the issue page."""
    status, published, date_source = "ok", issue.date, issue.source if issue.date else "none"
    try:
        page = fetcher.get(issue.link)
        if page.status != 200:
            status = f"http_{page.status}"
        elif page_date := published_date_from_html(page.text):
            published, date_source = page_date, "page"
    except RobotsDisallowed:
        status = "robots_blocked"
    except Exception as e:
        status = f"error:{type(e).__name__}"
    if status == "ok" and published and published < SINCE:
        status = "out_of_window"  # enumerated date was a proxy (e.g. sitemap lastmod) and the page disagrees
    if status == "ok" and not published:
        status = "no_date"
    return status, published, date_source, issue


def _row(pub: dict, n: int, issue, published=None, date_source="", status="ok") -> dict:
    return {
        "issue_id": f"{pub['id']}_iss_{n:03d}",
        "publisher_id": pub["id"],
        "publisher": pub["name"],
        "vertical": pub["vertical"],
        "issue_url": issue.link if issue else "",
        "published": published.date().isoformat() if published else "",
        "date_source": date_source,
        "audience": issue.audience if issue else "",
        "discovered_via": issue.source if issue else "",
        "status": status,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    with open(PUBLISHERS, newline="", encoding="utf-8") as f:
        pubs = list(csv.DictReader(f))
    current_ids = {p["id"] for p in pubs}
    if args.only:
        pubs = [p for p in pubs if p["name"] in set(args.only)]

    fetcher = PoliteFetcher()
    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(collect, fetcher, p): p for p in pubs}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                got = fut.result()
            except Exception as e:  # one publisher's bug must not sink the run
                got = [_row(p, 0, None, status=f"error:{type(e).__name__}: {e}")]
            rows += got
            ok = [r for r in got if r["status"] == "ok"]
            span = f"{min(r['published'] for r in ok)} .. {max(r['published'] for r in ok)}" if ok else "-"
            print(f"  {len(ok):>3} ok / {len(got):<3} {p['vertical']:<17} {p['name']:<30} {span}", flush=True)

    previous = []
    if args.only and os.path.exists(OUT):
        with open(OUT, newline="", encoding="utf-8") as f:
            redone = {p["id"] for p in pubs}
            # keep other publishers' rows, but only for publishers still on the list
            previous = [r for r in csv.DictReader(f) if r["publisher_id"] not in redone and r["publisher_id"] in current_ids]
    rows = sorted(previous + rows, key=lambda r: r["issue_id"])
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    status = Counter(r["status"] for r in rows)
    ok_by_pub = Counter(r["publisher_id"] for r in rows if r["status"] == "ok")
    thin = sorted(r["publisher"] for r in {r["publisher_id"]: r for r in rows}.values() if ok_by_pub[r["publisher_id"]] < 20)
    print(f"\n{status['ok']} usable issues from {len(ok_by_pub)} publishers; statuses: {dict(status)}")
    print(f"under 20 usable issues: {thin or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
