"""Structured JSONL run-trace logging. One file per run under traces/,
one flat JSON object per line — greppable, and free-form enough for later
phases to add new event_type values without a schema change."""

import json
import os
import uuid
from datetime import datetime, timezone

DEFAULT_TRACES_DIR = os.environ.get("SPONSORFIT_TRACES_DIR", "traces")


class RunTracer:
    def __init__(self, run_id: str | None = None, traces_dir: str | None = None):
        self.run_id = run_id or (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
        )
        self.traces_dir = traces_dir or DEFAULT_TRACES_DIR
        os.makedirs(self.traces_dir, exist_ok=True)
        self.path = os.path.join(self.traces_dir, f"{self.run_id}.jsonl")

    def log(self, event_type: str, payload: dict, agent: str = "manual") -> None:
        line = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "agent": agent,
            "event_type": event_type,
            "payload": payload,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
