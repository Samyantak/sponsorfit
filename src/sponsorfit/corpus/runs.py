"""Load a saved extraction run, re-checking evidence with the current rules.

Grounding is deterministic code, not model output, so it can improve after a
run without re-running the model: loading re-grades every sponsor's
`grounding` against the cached issue page. Later lines in a run file win, so
a retried error supersedes the failed attempt.
"""

import csv
import json
import os

from sponsorfit.corpus.extract import evidence_grounding, prepare_issue
from sponsorfit.scrape.fetch import PoliteFetcher

RUNS_DIR = os.path.join("data", "processed", "extractions")
MODEL_SLUG = "qwen3-8b"


def load_run(version: str, regrade: bool = True, fetcher: PoliteFetcher | None = None) -> dict[str, dict]:
    path = os.path.join(RUNS_DIR, f"{version}__{MODEL_SLUG}.jsonl")
    with open(path, encoding="utf-8") as f:
        records = {r["issue_id"]: r for r in map(json.loads, f)}
    if not regrade:
        return records
    with open(os.path.join("data", "sources", "publishers.csv"), newline="", encoding="utf-8") as f:
        site_of = {p["id"]: p["url"] for p in csv.DictReader(f)}
    fetcher = fetcher or PoliteFetcher()
    for r in records.values():
        if not r.get("sponsors"):
            continue
        issue = prepare_issue(fetcher.get(r["issue_url"]).text, site_of[r["publisher_id"]])  # cache hit
        for s in r["sponsors"]:
            s["grounding"] = evidence_grounding(s.get("evidence", ""), issue, s.get("name", ""))
    return records
