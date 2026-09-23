"""Phase 1.2 step B: LLM sponsor extraction from one newsletter issue.

The model sees a cleaned version of the issue page plus two side channels —
outbound links (anchor text → domain) and image alt text — because sponsor
names are often only in a logo ("Together with [logo]"). Its answer is
schema-constrained JSON, and every sponsor's `evidence` quote is checked
against the issue text: a sponsor the model can't ground in the text is kept
for auditing but never becomes a pairing.
"""

import hashlib
import html
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from sponsorfit.llm import MeteredLLMClient
from sponsorfit.scrape.archive import site_key
from sponsorfit.scrape.screen import html_to_text

MAX_TEXT_CHARS = 24_000  # ~6k tokens; head + tail kept when longer
OLLAMA_OPTIONS = {"num_ctx": 16384, "temperature": 0}
# v1 used 1500: every one of its bad_json results stopped at exactly 1500
# output tokens (a long, over-extracted list cut off mid-JSON).
MAX_OUTPUT_TOKENS = 3000

# Script/style carry beehiiv's JSON copy of the issue (duplicates, 500k+ chars);
# the issue itself is rendered as HTML too, so dropping these loses no sponsor.
_DROP_BLOCKS_RE = re.compile(r"<(script|style|noscript|svg|head)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_LINK_RE = re.compile(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE)
_ALT_RE = re.compile(r"<img\b[^>]*\balt=[\"']([^\"']{2,120})[\"']", re.IGNORECASE)

# Never a sponsor's own site: hosting platforms, social networks, podcast apps.
_NON_SPONSOR_DOMAINS = {
    "substack.com", "substackcdn.com", "beehiiv.com", "twitter.com", "x.com", "linkedin.com",
    "facebook.com", "instagram.com", "youtube.com", "youtu.be", "tiktok.com", "threads.net",
    "spotify.com", "bsky.app", "reddit.com", "t.co", "wordpress.com", "gravatar.com",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "sponsors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "domain": {"type": ["string", "null"]},
                    "evidence": {"type": "string"},
                    "placement": {"type": "string", "enum": ["primary", "secondary", "classified", "unclear"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["name", "domain", "evidence", "placement", "confidence"],
            },
        },
        "excluded": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reason": {"type": "string", "enum": ["self_promo", "cross_promo", "editorial_mention", "ad_sales_cta", "affiliate"]},
                },
                "required": ["name", "reason"],
            },
        },
    },
    "required": ["sponsors", "excluded"],
}


@dataclass
class PreparedIssue:
    text: str
    links: list[str]  # "anchor text -> domain"
    alts: list[str]

    def render(self, newsletter: str, issue_url: str) -> str:
        return (
            f"NEWSLETTER: {newsletter}\nISSUE URL: {issue_url}\n\n=== ISSUE TEXT ===\n{self.text}\n\n"
            f"=== LINKS (anchor text -> domain) ===\n" + ("\n".join(self.links) or "(none)") + "\n\n"
            f"=== IMAGE ALT TEXT ===\n" + ("\n".join(self.alts) or "(none)")
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps([self.text, self.links, self.alts]).encode("utf-8")).hexdigest()[:16]


def _looks_like_code(line: str) -> bool:
    return ("{" in line and "}" in line) or line.count('":') >= 2 or line.startswith(("p span[", "@media", "."))


def prepare_issue(page_html: str, site_url: str) -> PreparedIssue:
    body = _DROP_BLOCKS_RE.sub(" ", page_html)
    lines = [ln for ln in dict.fromkeys(html_to_text(body).splitlines()) if not _looks_like_code(ln)]
    text = "\n".join(lines)
    if len(text) > MAX_TEXT_CHARS:  # sponsors sit at the top (header slot) and bottom (classifieds)
        text = text[: MAX_TEXT_CHARS * 3 // 4] + "\n[... middle of issue omitted ...]\n" + text[-MAX_TEXT_CHARS // 4 :]

    own = site_key(site_url)
    links, seen_domains = [], set()
    for href, inner in _LINK_RE.findall(body):
        host = (urlsplit(html.unescape(href)).hostname or "").removeprefix("www.")
        key = site_key(f"https://{host}")
        if not host or key == own or key in _NON_SPONSOR_DOMAINS or host in seen_domains:
            continue
        seen_domains.add(host)
        anchor = " ".join(html_to_text(inner).split())[:80] or "(image/no text)"
        links.append(f"{anchor} -> {host}")
    alts = list(dict.fromkeys(" ".join(a.split()) for a in _ALT_RE.findall(body)))
    return PreparedIssue(text=text, links=links[:80], alts=alts[:40])


# --- evidence grounding ---------------------------------------------------

_QUOTE_MAP = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def _norm(s: str) -> str:
    return " ".join(s.translate(_QUOTE_MAP).lower().split())


STITCH_MIN_COVERAGE = 0.75  # share of evidence words that must be verbatim
STITCH_MIN_CHUNK = 3  # words; shorter matches are too generic to count
STITCH_MAX_SPAN = 800  # chars between the first and last matched chunk


def _stitched(ev_words: list[str], haystack: str, name: str) -> bool:
    """Evidence assembled from verbatim pieces that sit close together.

    Issues put the label on its own line above a list ("Brought to You by" /
    "• Buildkite – the CI platform…") or after the ad ("…Postgres tables. /
    ParadeDB sponsor"), and the model joins them into one quote that occurs
    nowhere contiguously. Accept it only if most of it is verbatim, the
    pieces are within STITCH_MAX_SPAN chars of each other, and the sponsor's
    name occurs inside that span — invented evidence fails all three."""
    i, covered, positions = 0, 0, []
    while i < len(ev_words):
        j = len(ev_words)
        while j - i >= STITCH_MIN_CHUNK and " ".join(ev_words[i:j]) not in haystack:
            j -= 1
        if j - i >= STITCH_MIN_CHUNK:
            chunk = " ".join(ev_words[i:j])
            positions.append((haystack.find(chunk), len(chunk)))
            covered += j - i
            i = j
        else:
            i += 1
    if not positions or covered / len(ev_words) < STITCH_MIN_COVERAGE:
        return False
    start = min(p for p, _ in positions)
    end = max(p + n for p, n in positions)
    return end - start <= STITCH_MAX_SPAN and _norm(name) in haystack[max(0, start - 200) : end + 200]


def evidence_grounding(evidence: str, issue: PreparedIssue, name: str = "") -> str:
    """exact | prefix | stitched | none — does the quoted evidence actually occur?"""
    haystack = _norm(issue.text + "\n" + "\n".join(issue.links) + "\n" + "\n".join(issue.alts))
    ev = _norm(evidence).strip(" .\"'")
    if ev and ev in haystack:
        return "exact"
    words = ev.split()
    if len(words) >= 4 and " ".join(words[:6]) in haystack:
        return "prefix"  # model paraphrased the tail of an otherwise real quote
    if name and len(words) >= STITCH_MIN_CHUNK and _stitched(words, haystack, name):
        return "stitched"
    return "none"


_SIGNAL_RE = re.compile(
    r"sponsor|brought to you|presented by|together with|in partnership|from our partners|advertis|"
    r"paid (?:post|partnership)|compensated|promoted by|a word from|a message from",
    re.IGNORECASE,
)


def sponsorship_signal_nearby(evidence: str, issue: PreparedIssue, window: int = 300) -> bool:
    """LLM-independent sanity check: is there any sponsorship wording in the
    evidence itself or within `window` chars of where it occurs? Verbatim
    grounding alone can't catch v1's failure — "Brex secured a $235m credit
    facility" is a real quote, just not a sponsorship. A flag, not a filter:
    logo-only slots can legitimately lack nearby wording."""
    if _SIGNAL_RE.search(evidence):
        return True
    words = evidence.translate(_QUOTE_MAP).split()[:6]
    if not words:
        return False
    m = re.search(r"\s+".join(map(re.escape, words)), issue.text.translate(_QUOTE_MAP), re.IGNORECASE)
    if not m:
        return False
    return bool(_SIGNAL_RE.search(issue.text[max(0, m.start() - window) : m.end() + window]))


def extract_sponsors(
    client: MeteredLLMClient,
    *,
    system_prompt: str,
    prompt_version: str,
    model: str,
    newsletter: str,
    issue_url: str,
    issue: PreparedIssue,
) -> dict:
    resp = client.call(
        provider="ollama",
        model=model,
        system=system_prompt,
        messages=[{"role": "user", "content": issue.render(newsletter, issue_url)}],
        max_tokens=MAX_OUTPUT_TOKENS,
        prompt_version=prompt_version,
        agent="sponsor_extractor",
        json_schema=SCHEMA,
        ollama_options=OLLAMA_OPTIONS,
    )
    result = {"call_id": resp.call_id, "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out, "latency_ms": resp.latency_ms}
    try:
        parsed = json.loads(resp.text)
    except json.JSONDecodeError as e:
        return result | {"status": "bad_json", "error": str(e), "raw": resp.text[:2000], "sponsors": [], "excluded": []}
    sponsors = [s | {"grounding": evidence_grounding(s.get("evidence", ""), issue, s.get("name", ""))} for s in parsed.get("sponsors", [])]
    truncated = resp.tokens_in >= OLLAMA_OPTIONS["num_ctx"] - 32  # Ollama truncates silently at num_ctx
    return result | {"status": "context_truncated" if truncated else "ok", "sponsors": sponsors, "excluded": parsed.get("excluded", [])}
