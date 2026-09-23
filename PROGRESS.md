# SponsorFit — Build Log

A running record of what was actually done, in order, and why. Not a plan —
a log. The full multi-phase roadmap this follows lives at
[ROADMAP.md](ROADMAP.md) in this same repo; this file tracks execution
against it, one entry per work session.

---

## 2026-09-17 — Phase 0: Foundation

**Goal:** scaffolding that makes everything after it measurable, per the
roadmap's own warning that evaluation-last agent projects end up with no
defensible numbers.

- Chose project name **SponsorFit** / package `sponsorfit`, matching the folder name.
- Decided the metered LLM wrapper must support **both Anthropic and OpenAI**, pluggable behind one interface, rather than locking to a single provider.
- Installed `uv` (via `winget install astral-sh.uv`) and pinned a **Python 3.12** interpreter (`uv python install 3.12`) — system Python was 3.13.7, which the roadmap doesn't target.
- Created `.venv` scoped to Python 3.12 (`uv venv --python 3.12`).
- Scaffolded the full directory tree: `data/{raw,processed,eval}`, `prompts/`, `src/sponsorfit/` (with empty stub subpackages `agents/`, `tools/`, `eval/`, `scrape/` for later phases to plug into), `tests/`, `scripts/`, `traces/`, `notebooks/`.
- Wrote `pyproject.toml` (uv-managed, `hatchling` build, deps: `anthropic`, `openai`, `python-dotenv`; dev dep `pytest`).
- Wrote `.gitignore` — notably: `data/eval/*` is **intentionally tracked** (frozen eval splits should be reviewable in git) while `data/raw/*`, `data/processed/*`, `traces/*`, and `data/costs.db*` are ignored.
- Wrote `.env.example` (provider keys + default provider/model env vars).
- Built the cost-tracking core, in dependency order:
  - `src/sponsorfit/pricing.py` — hardcoded `$/MTok` table per provider/model; unknown model raises `KeyError` on purpose (fail loud, never silently log `$0`).
  - `src/sponsorfit/costs.py` — SQLite `llm_calls` table at `data/costs.db` (chosen over `traces/` because the cost ledger must survive a trace cleanup — it tracks spend against the whole project's $30–80 budget, not one run).
  - `src/sponsorfit/tracing.py` — `RunTracer`, one JSONL file per run under `traces/`.
  - `src/sponsorfit/llm.py` — `MeteredLLMClient` / `call_llm()`, the single call-through path for both providers. Every call (success or failure) writes one SQLite row and one JSONL trace line. No retry/backoff yet — deferred to the prompt-engineering phase.
- Wrote `tests/test_smoke.py` (trivial) and `tests/test_llm_metering.py` (mocks the Anthropic SDK, proves the full cost + trace pipeline with **zero API spend**). Both pass: `uv run pytest` → 2 passed.
- Wrote `scripts/smoke_test.py` — the one real LLM call that satisfies Phase 0's exit criteria (make a call, then query SQLite for its exact cost).
- Wrote the `README.md` stub (hypothesis placeholder still TODO).
- Ran into an API key issue on the first real smoke-test call: the provided Anthropic key wasn't scoped to a workspace (`anthropic-workspace-id` error). Fix in progress — generating a workspace-scoped key from the Anthropic console rather than adding a header-based workaround, to keep `llm.py` simple.

**Status at end of session:** all code and tests in place; `uv run python scripts/smoke_test.py` blocked only on swapping in a workspace-scoped API key. Nothing else outstanding for Phase 0.

---

## 2026-09-22 — Housekeeping

- Started this build log (`PROGRESS.md`) at the user's request, to track steps taken across sessions.

## 2026-09-22 — Shift default execution to local Ollama models

**Goal:** keep the project's whole-lifetime $30–80 API budget mostly untouched by running the bulk of LLM calls locally instead of against paid cloud APIs.

- Decided the per-task routing:
  - Embeddings/retrieval, sponsor extraction (Phase 1), smoke tests/debugging → Ollama `qwen3:8b`
  - Prompt iteration (Phase 4, batched overnight) and full eval runs/headline numbers → Ollama `qwen3:14b`
  - Strategy agent (Phase 5) → Ollama
- Kept Anthropic and OpenAI wired into `llm.py` as explicit `provider=` overrides for later phases, rather than ripping them out — the wrapper was already designed pluggable, so this was additive, not a rewrite.
- Installed **Ollama** (`winget install Ollama.Ollama`) — service starts automatically and listens on `localhost:11434`.
- Kicked off background pulls of `qwen3:8b` (~5.2GB) and `qwen3:14b` (~9GB).
- Added `src/sponsorfit/llm.py::_call_ollama` — talks to Ollama's native `/api/chat` REST endpoint via stdlib `urllib` (no new dependency), reading `prompt_eval_count`/`eval_count` for token counts.
- Updated `pricing.py::compute_cost_usd` to special-case `provider == "ollama"` → always `$0.00`, rather than requiring a pricing entry per local model (local models change often; cost is $0 regardless of which one).
- Switched `.env` / `.env.example` defaults to `SPONSORFIT_DEFAULT_PROVIDER=ollama`, `SPONSORFIT_DEFAULT_MODEL=qwen3:8b`; documented the full per-phase routing table as comments (routing itself happens via explicit `model=` args per call, not env vars).
- Repointed `scripts/smoke_test.py` at Ollama/`qwen3:8b` instead of Anthropic — sidesteps the still-unresolved Anthropic workspace-scoping issue and matches the "smoke tests → Ollama" routing decision anyway.
- Added `tests/test_llm_metering.py::test_metered_call_ollama_is_free` (mocks `urllib.request.urlopen`, zero real calls) — `uv run pytest` → 3 passed.
- Updated `README.md` with the routing table and Ollama install/pull steps.

**Status at end of session:** code changes done and tests passing; `qwen3:8b`/`qwen3:14b` downloads running in the background. Once they finish, `scripts/smoke_test.py` becomes runnable end-to-end against Ollama at $0 cost, and the Anthropic key issue becomes a non-blocker for Phase 0 completion (still worth fixing eventually for the cloud-fallback path).

**Update, same session — found and fixed a real bug:** `qwen3` is a hybrid *thinking* model. Ollama returns its chain-of-thought in a separate `message.thinking` field and the visible `message.content` was coming back **empty** because `max_tokens=16` was entirely consumed by hidden reasoning before any answer token was produced (confirmed via a raw `curl` to `/api/chat` — `done_reason: "length"`, `content: ""`, non-empty `thinking`). Fixed by sending `"think": false` in the Ollama request payload in `_call_ollama` — later phases want reasoning captured as an explicit structured-output field anyway (per the roadmap's Phase 3 design), not a hidden pre-answer trace eating the token budget.

- Re-verified with a raw `curl` request (`think: false` → `content: "pong"`) before touching code.
- Re-ran `uv run pytest` → 3 passed.
- Re-ran `uv run python scripts/smoke_test.py` against `qwen3:8b` → response text `"pong"`, SQLite row confirms `tokens_in=23, tokens_out=2, cost_usd=0.0, latency_ms=2250`. **Phase 0 exit criteria fully met, end to end, on Ollama.**
- Confirmed both `qwen3:8b` (5.2GB) and `qwen3:14b` (9.3GB, 14.8B params) finished downloading and show up in `ollama list`.
- Saved persistent cross-session memory (`project_sponsorfit` in the assistant's memory store) summarizing the roadmap and architecture decisions, since the user is about to switch from this chat interface to a separate Claude Code session in VS Code. This file (`PROGRESS.md`) remains the authoritative, guaranteed-to-carry-over record — read it first in any new session.

**Phase 0 status: complete.** All exit criteria met. Next up (not started): Phase 1 — sourcing the newsletter corpus.

## 2026-09-22 — Anthropic key fixed, one more real bug caught

- User generated a workspace-scoped Anthropic API key (console.anthropic.com → Settings → API Keys → Create Key, workspace set explicitly) and swapped it into `.env`, resolving the original "not scoped to a workspace" error.
- While verifying it, found and fixed a second real bug in `_call_anthropic`: it was passing `system=None` explicitly to `anthropic.Client.messages.create()`. This SDK version rejects that (`system: Input should be a valid array`) rather than silently ignoring it — only omitting the `system` kwarg entirely works, same pattern `_call_openai` already used. Fixed in `src/sponsorfit/llm.py` by building the kwargs dict conditionally.
- Chased a red herring for a while: an ad-hoc debug script placed *outside* the project directory (in the OS temp scratch folder) reproducibly hit `TypeError: Could not resolve authentication method`. Root cause was `python-dotenv`'s `load_dotenv()` — with no explicit path, it searches from the **calling script's own file location** upward, not the current working directory, so a script outside `F:\SponsorFit` never found `.env` and silently loaded nothing (`load_dotenv()` returned `False`). Not a bug in any shipped code — `scripts/smoke_test.py` and everything else lives inside the project, so this can't happen there. Lesson: always verify ad-hoc debug scripts from inside the project root, or pass `load_dotenv(dotenv_path=...)` explicitly.
- Verified end-to-end with a temporary in-project script: real Anthropic call succeeded, returned `"pong"` at `$0.00004`. Script deleted after confirming.
- `uv run pytest` → 3 passed, unaffected (mocked tests don't touch real credentials).

Both cloud providers and Ollama are now confirmed working through the same wrapper. Nothing outstanding for Phase 0.

## 2026-09-22 — Phase 0 loose ends + git

**Goal:** close the Phase 0 items that were done in substance but not on paper, and put the project under version control before Phase 1 (frozen eval splits are meant to be tracked in git, so the repo has to exist first).

- **README hypothesis written.** Replaced the TODO with a falsifiable claim: an LLM reranker over an embedding-retrieved shortlist ranks the real sponsor's publishers higher than embedding cosine similarity alone, measured by MRR and P@5 on **hard negatives** over a **time-based** held-out split, with the full baseline ladder (random → vertical → BM25 → embeddings → LLM rerank) on the same frozen split. Stated up front that a null result gets reported as the result.
- **Ticked Phase 0 checkboxes in ROADMAP.md.** Verified the one non-obvious item before ticking it: `prompt_version` is threaded through `llm.py` into both the `llm_calls` SQLite table and the JSONL trace.
- **Removed `prompts/placement_v2.md`.** A v2 stub existing before v1 has an eval score contradicts the Phase 4 rule of one prompt version per eval run. `placement_v1.md` and `strategy_v1.md` stay as placeholders — they get written in Phases 3 and 5 against the eval harness, not before.
- **`git init -b main`.** Before the first commit, confirmed via `git check-ignore` that `.env`, `data/costs.db`, `traces/*`, and `.venv/` are excluded, and grepped every staged file for API-key patterns (none found).
- Added `.claude/settings.local.json` to `.gitignore` — per-machine Claude Code permissions with absolute local paths, not project config.
- Added `.gitattributes` (`* text=auto eol=lf`) so Windows CRLF conversion doesn't churn every diff.
- `uv run pytest` → 3 passed.
- Installed the GitHub CLI (`winget install GitHub.cli`); user authenticated once with `gh auth login` (HTTPS, gh as git credential helper). Gotcha: VS Code's integrated terminals keep the PATH from when VS Code launched, so `gh` isn't recognized until VS Code is fully restarted — use `& "C:\Program Files\GitHub CLI\gh.exe"` until then.
- Git identity set **repo-locally** (not global): `Samyantak Mohanty` / GitHub noreply email, so the personal email never appears in public commit metadata if the repo is made public later.
- Created **private** repo [Samyantak/sponsorfit](https://github.com/Samyantak/sponsorfit) and pushed `main`. Private for now; will go public once there are results worth showing.
- Working convention from here on: every meaningful chunk of work ends with a PROGRESS.md entry, a commit, and a push.
- Plan-vs-reality review: Phase 0 finished in ~5 calendar days, in line with the roadmap's 3–5 day estimate. Updated the stale README "Status" section (it still described Phase 0 as in progress). Flagged two items for later phases — no local embedding model pulled yet (needed by Phase 2), and the implications of a local-14B reranker for the headline comparison (Phase 3).

## 2026-09-22 — Roadmap: embedding model + local/cloud reranker comparison added to Phase 3

**Goal:** fold the two issues flagged in the plan review into the plan itself: no embedding model exists yet, and a local 14B reranker may weaken the headline result.

- Added a **Models** section to ROADMAP.md Phase 3:
  - **Embedding model:** pull a dedicated local embedding model (`qwen3:8b`/`14b` are chat models), add a metered `embed()` to `llm.py`, and cache vectors by `(embedding_model, text_hash)`. The Phase 2 baseline and the Phase 3 top-50 shortlist must share the **same model and cache**. Otherwise "rerank vs. embeddings" compares two embedding spaces, not reasoning vs. similarity. Added retrieval **recall@50** as a tracked number, since it caps everything downstream.
  - **Reranker comparison:** `qwen3:14b` local stays the default for all development and Phase 4 iteration. Add **one** Claude Sonnet run on identical inputs (same prompt version, split, cached candidate sets), with cost estimated beforehand from the local run's logged token counts × `pricing.py`, and a hard **$10 cap per cloud run**. Cloud runs at most twice total (the Phase 3 prompt, then optionally the final Phase 4 prompt).
- **Placement decision:** the user asked for this in Phase 3. The embedding *setup* is also referenced from Phase 2 baseline #4, because that baseline is the model's first user and can't run without it. The content lives in Phase 3; Phase 2 points to it.
- Updated the routing table in README.md and the comments in `.env.example` to match.

## 2026-09-22 — Phase 1.1: publisher screening (in progress)

**Goal:** before writing any extraction code, find out which candidate newsletters are actually usable as sources: reachable, robots-permitted, and visibly carrying sponsors.

**Built:**
- `src/sponsorfit/scrape/fetch.py` — `PoliteFetcher`, the single path for all scraping (the `llm.py` equivalent for HTTP). robots.txt checked on every fetch using **protego** (RFC 9309; stdlib `urllib.robotparser` ignores `*` wildcards, which Substack's robots.txt uses, and treats a 403 on robots.txt as "disallow all"). 4xx robots → allow, 5xx/unreachable → disallow. Per-host spacing ≥ 2s or the site's `Crawl-delay`. Crawl delays over 30s are **declined, never violated** (`CrawlDelayTooLong`). Successful responses cached to `data/raw/http/` (gitignored), so re-runs are free.
- `src/sponsorfit/scrape/screen.py` — deterministic screener, no LLM calls: feed discovery (`<link rel=alternate>` + common paths), **sitemap fallback** for sites with no feed (follows sitemap indexes, orders issues by `<lastmod>`), reads each sampled issue's feed body **and** its web page, and scans for sponsor markers.
- `scripts/screen_publishers.py` → `data/sources/screening.csv` + `screening_samples.jsonl` (per-issue evidence). Tracked in git.
- `data/sources/candidates.csv` — 99 candidates across 8 verticals: 75 from prior knowledge, 24 from web research (beehiiv's parenting and fitness top-10 lists, Reletter's Substack parenting chart, food Substack roundups).
- New deps: `feedparser`, `protego`. Tests: 3 → 20, all passing, no network.

**Real problems found by looking at real output (none were caught by the first unit tests):**
1. **Cooperpress** (JS/Go/Postgres Weekly) scored 0%. Its feed rewrites links, so the `utm_content=sponsored` param seen on the web page isn't in the feed; the only marker is a small `<span>sponsor</span>` label after the company name. Added a detector for it.
2. **The Spoon's robots.txt sets `Crawl-delay: 600`** (10 min/request), which stalled the first run for over an hour. The fetcher was correctly honoring it; screening now declines instead of waiting.
3. **Link-roundup feeds** (Console, Sidebar) point items at *external* articles, so the screener fetched github.com, anthropic.com, etc. and scored the wrong pages. Console's first-pass 0.4 turned out to be **entirely false positives** from those pages. Issue pages are now only fetched on the newsletter's own domain.
4. **beehiiv strips ad blocks from RSS.** The Neuron's feed has full text but no sponsors; the web page has `<div id="sponsored">` blocks under a "FROM OUR PARTNERS" heading. The screener now always reads the web page as well, and detects both.
5. **~30 sites had no feed** (mostly beehiiv with RSS disabled). The sitemap fallback recovered Exec Sum, This Week in Fintech, Out-of-Pocket, Superhuman AI, Every Baby Is Different Anyways, and others.
6. **Some feeds are the website, not the newsletter** (The Daily Upside, Motherly, ParentData, Marketing Millennials' `/articles`). Their 0% says nothing about the email. Not fixed yet.

**Changed the pass rule.** It was "≥ 40% of sampled issues sponsored"; now it's "≥ 1 sampled issue with a sponsor signal", plus human review. Phase 1 needs *pairings*: a newsletter sponsored in 1 of 3 issues still yields ~15 pairings over a 50-issue archive. The 40% rule was rejecting clearly sponsored newsletters (Growth Memo/Semrush, Today's Ajenda/Pique, Money with Katie/Betterment).

**Spot-check (the roadmap's "check real output" discipline, applied early):** read all 239 detected sponsor snippets.
- Snippet-level precision ≈ **89%** (~25 false positives in ~230). False positives fall into three kinds: mid-sentence prose ("presented by counsel", "sponsored by herbal tincture brands"), self/cross-promo ("Together with Go Weekly", "not boring world", "PRESENTED BY The Pragmatic Engineer"), and markup noise.
- 3 sites had **only** false positives and are rejected (Fintech Business Weekly, Generation Iron, Naptime Kitchen). Verdicts and reasons are in `data/sources/review.csv`.

**Carry-forward for Phase 1.2 extraction:**
- The sponsor name is often **only in a logo image** (Exec Sum "Together with [logo]", The Neuron "FROM OUR PARTNERS", Out-of-Pocket "brought to you by..."). Text-only extraction will miss them, so read `<img alt>` and outbound link domains too.
- Self-promo and cross-promo need an explicit exclusion rule, or they become fake pairings.
- beehiiv pages repeat body text inside JSON, so dedupe before extraction.

**Current result:** 37 candidates → **34 kept** after review. Tech 14, health/fitness 6, indie business 4, marketing 3, personal finance 3, design 2, food 1, parenting 1. That's below the 40–60 target and badly imbalanced: consumer creator newsletters (recipes, parenting) mostly don't run sponsors, which is itself a real finding about the market. Next: decide the vertical mix with the user.
