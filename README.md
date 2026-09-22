# SponsorFit

AI sponsorship-placement portfolio project: a multi-agent system that matches
advertiser campaign briefs to newsletter/podcast sponsorship inventory.

See [ROADMAP.md](ROADMAP.md) for the full multi-phase plan (Phase 0–9) and
[PROGRESS.md](PROGRESS.md) for the chronological build log of what's actually
been done against it.

## Hypothesis

Given an advertiser profile, an LLM reranker that reasons over an
embedding-retrieved shortlist of publishers ranks the publishers that actually
ran that sponsor higher than embedding cosine similarity alone.

- **Ground truth:** real advertiser–publisher sponsorship pairings extracted
  from public newsletter archives.
- **Test set:** pairings after a fixed cutoff date (time-based split, never
  random), with sponsor names stripped from every publisher profile.
- **Primary metric:** MRR and Precision@5 on **hard negatives** (same-vertical
  publishers that did not run the sponsor). Easy-negative numbers are reported
  but are not the claim.
- **Comparison:** random → vertical match → BM25 → embedding similarity →
  LLM rerank, all on the same frozen split.
- **Secondary:** cost per ranked campaign, and whether the per-candidate
  reasoning is inspectable enough to explain a ranking.

If the reranker does not beat the embedding baseline on hard negatives, that is
the result and it gets reported as such.

## Status

Phase 0 (foundation: metered LLM wrapper, cost tracking, structured tracing)
is complete. Next: Phase 1, sourcing the newsletter corpus. No scraping or
agent logic yet.

## Budget

Total API budget across the whole project: **$30–80**. Most LLM calls run
locally via **Ollama** ($0 cost) — Anthropic/OpenAI remain available as a
pluggable option for calls where a stronger cloud model is worth the spend.
Every LLM call, on any provider, is logged with exact token/cost/latency to
`data/costs.db` — see `scripts/smoke_test.py`.

Default model routing (see `.env.example` for the full breakdown):

| Task | Provider / model |
|---|---|
| Embeddings and retrieval | Ollama, local |
| Sponsor extraction (Phase 1) | Ollama — `qwen3:8b` |
| Smoke tests, pipeline debugging | Ollama — `qwen3:8b` |
| Prompt iteration (Phase 4) | Ollama — `qwen3:14b`, batched overnight |
| Full test runs, headline numbers | Ollama — `qwen3:14b` |
| Strategy agent (Phase 5) | Ollama |

## Repo layout

- `data/` — raw scraped HTML, processed profiles/pairings, frozen eval splits, cost DB
- `prompts/` — versioned prompt files + `CHANGELOG.md` (version → eval score → what changed)
- `src/sponsorfit/` — application code (`llm.py` metered wrapper; `agents/`,
  `tools/`, `eval/`, `scrape/` are empty stub packages for later phases)
- `traces/` — JSONL agent run traces (gitignored)
- `notebooks/` — exploration only, nothing load-bearing
- `tests/` — pytest suite
- `scripts/` — manual verification scripts

## Setup

```bash
# one-time: install uv, then pin Python 3.12
uv python install 3.12
uv venv --python 3.12

uv sync
cp .env.example .env   # fill in ANTHROPIC_API_KEY / OPENAI_API_KEY if using them

# Ollama (local, default provider) — install, then pull the two models used across phases
winget install Ollama.Ollama
ollama pull qwen3:8b
ollama pull qwen3:14b
```

## Running tests

```bash
uv run pytest
```

## Running the smoke test (makes one real, cheap LLM call)

```bash
uv run python scripts/smoke_test.py
```
