"""SQLite cost-tracking ledger. One row per LLM call, across the whole
project's lifetime — this is the running ledger against the $30-80 total
API budget, so it lives in data/ (persistent) rather than traces/
(gitignored, ephemeral per-run output)."""

import os
import sqlite3

DEFAULT_DB_PATH = os.environ.get("SPONSORFIT_COSTS_DB_PATH", "data/costs.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id        TEXT NOT NULL UNIQUE,
    run_id         TEXT,
    timestamp      TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT,
    tokens_in      INTEGER NOT NULL,
    tokens_out     INTEGER NOT NULL,
    cost_usd       REAL NOT NULL,
    latency_ms     INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'ok',
    error_message  TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_timestamp ON llm_calls(timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run_id ON llm_calls(run_id);
"""


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    db_path = db_path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def record_call(
    conn: sqlite3.Connection,
    *,
    call_id: str,
    run_id: str | None,
    provider: str,
    model: str,
    prompt_version: str | None,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_ms: int,
    status: str = "ok",
    error_message: str | None = None,
) -> None:
    from datetime import datetime, timezone

    conn.execute(
        """
        INSERT INTO llm_calls
            (call_id, run_id, timestamp, provider, model, prompt_version,
             tokens_in, tokens_out, cost_usd, latency_ms, status, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            call_id,
            run_id,
            datetime.now(timezone.utc).isoformat(),
            provider,
            model,
            prompt_version,
            tokens_in,
            tokens_out,
            cost_usd,
            latency_ms,
            status,
            error_message,
        ),
    )
    conn.commit()


def get_call(conn: sqlite3.Connection, call_id: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM llm_calls WHERE call_id = ?", (call_id,))
    return cur.fetchone()
