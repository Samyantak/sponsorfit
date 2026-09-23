"""Phase 1.2 step B: run sponsor extraction over data/sources/issues.csv.

Local Ollama only ($0), every call metered through llm.py. Results append to
data/processed/extractions/<prompt_version>__<model>.jsonl, keyed by
(issue_id, input fingerprint): an interrupted run resumes where it stopped,
and re-running an unchanged prompt/model/input is free.

    uv run python scripts/extract_sponsors.py [--prompt-version extraction_v2] [--limit N] [--only NAME ...] [--sample N]
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

from sponsorfit.corpus.extract import extract_sponsors, prepare_issue
from sponsorfit.llm import MeteredLLMClient
from sponsorfit.scrape.fetch import PoliteFetcher

DEFAULT_PROMPT_VERSION = "extraction_v2"
MODEL = "qwen3:8b"
ISSUES = os.path.join("data", "sources", "issues.csv")
PUBLISHERS = os.path.join("data", "sources", "publishers.csv")
OUT_DIR = os.path.join("data", "processed", "extractions")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="stop after N new extractions")
    ap.add_argument("--only", nargs="*", help="publisher names")
    ap.add_argument("--sample", type=int, help="random N issues (seeded), for trial runs")
    ap.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION, help="prompts/<version>.md")
    args = ap.parse_args()
    prompt_version = args.prompt_version

    with open(prompt_path := os.path.join("prompts", f"{prompt_version}.md"), encoding="utf-8") as f:
        system_prompt = f.read()
    with open(PUBLISHERS, newline="", encoding="utf-8") as f:
        site_of = {p["id"]: p["url"] for p in csv.DictReader(f)}
    with open(ISSUES, newline="", encoding="utf-8") as f:
        issues = [r for r in csv.DictReader(f) if r["status"] == "ok"]
    if args.only:
        issues = [r for r in issues if r["publisher"] in set(args.only)]
    if args.sample:
        issues = random.Random(42).sample(issues, min(args.sample, len(issues)))

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"{prompt_version}__{MODEL.replace(':', '-')}.jsonl")
    done = set()
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            done = {(r["issue_id"], r["fingerprint"]) for r in map(json.loads, f)}

    fetcher = PoliteFetcher()
    run_id = f"extract_{prompt_version}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    client = MeteredLLMClient(run_id=run_id)
    print(f"{len(issues)} issues, {len(done)} already extracted; prompt={prompt_path} model={MODEL} run={run_id}")

    new = skipped = 0
    started = time.monotonic()
    with open(out_path, "a", encoding="utf-8") as out:
        for row in issues:
            page = fetcher.get(row["issue_url"])  # cache hit — collected in step A
            prepared = prepare_issue(page.text, site_of[row["publisher_id"]])
            if (row["issue_id"], prepared.fingerprint()) in done:
                skipped += 1
                continue
            try:
                result = extract_sponsors(
                    client, system_prompt=system_prompt, prompt_version=prompt_version, model=MODEL,
                    newsletter=row["publisher"], issue_url=row["issue_url"], issue=prepared,
                )
            except Exception as e:  # Ollama down / timeout: record and continue, don't lose the run
                result = {"status": f"error:{type(e).__name__}", "error": str(e), "sponsors": [], "excluded": []}
            record = {
                "issue_id": row["issue_id"], "publisher_id": row["publisher_id"], "publisher": row["publisher"],
                "vertical": row["vertical"], "issue_url": row["issue_url"], "published": row["published"],
                "prompt_version": prompt_version, "model": MODEL, "fingerprint": prepared.fingerprint(),
                "input_chars": len(prepared.text),
            } | result
            out.write(json.dumps(record) + "\n")
            out.flush()
            new += 1
            names = ", ".join(f"{s['name']}{'' if s['grounding'] != 'none' else '(!)'}" for s in record["sponsors"]) or "-"
            print(f"  [{new}] {record['status']:<8} {record.get('latency_ms', 0) / 1000:>5.1f}s {row['publisher'][:22]:<22} {names[:90]}", flush=True)
            if args.limit and new >= args.limit:
                break

    mins = (time.monotonic() - started) / 60
    print(f"\n{new} new extractions in {mins:.1f} min ({skipped} cached) -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
