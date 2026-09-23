"""Phase 1.1 publisher screening: is a candidate newsletter usable as a source?

No LLM calls — this is a cheap, deterministic pre-filter. For each candidate
it answers: can we find a feed, does robots.txt let us read it, and do recent
issues visibly carry sponsors? Real sponsor *extraction* is Phase 1.2; this
only estimates whether extraction would have anything to find.
"""

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sponsorfit.scrape.archive import (
    MIN_FEED_ITEMS,
    Issue,
    discover_feed,
    feed_issues,
    platform_of,
    site_key,
    sitemap_issues,
)
from sponsorfit.scrape.fetch import CrawlDelayTooLong, PoliteFetcher, RobotsDisallowed

# Phase 1 needs pairings, not mostly-sponsored newsletters: one sponsor in
# every few issues still yields plenty over a 20–50 issue archive. So any
# signal makes a candidate; data/sources/review.csv holds the human verdict.
MIN_SPONSORED_ISSUES = 1

# Structured sponsor phrasing only. Bare "sponsored" is deliberately absent:
# it matches prose like "Amazon's sponsored listings".
_TEXT_MARKERS = [
    r"brought to you by",
    r"presented by",
    r"sponsored by",
    r"in partnership with",
    r"today['’]s sponsor",
    r"(?:thanks|thank you) to (?:our|this week['’]s|today['’]s) sponsors?",
    r"a (?:word|message) from (?:our|this week['’]s|today['’]s) sponsors?",
    r"^[ \t]*together with\b",  # beehiiv-style section header, own line
    r"^[ \t]*\(?sponsor(?:ed)?\)?[ \t]*$",  # standalone "Sponsor" label line
    r"\((?:sponsor|sponsored)\)",
    r"^[ \t]*sponsor(?:ed)?[ \t]*[:|—–-]",  # "Sponsor: X" / "SPONSORED — X"
    r"^[^\n]{1,60}\bsponsor(?:ed)?[ \t]*$",  # Cooperpress: "ParadeDB sponsor" label ending a line
    r"^[ \t]*from our (?:partners?|sponsors?)\b",  # beehiiv ad-slot heading
]
TEXT_MARKER_RE = re.compile("|".join(f"(?:{p})" for p in _TEXT_MARKERS), re.IGNORECASE | re.MULTILINE)

# Markup-only signals: tracking params on sponsored links (Cooperpress web
# pages, beehiiv), and beehiiv's ad-slot container. Feeds often strip both.
HTML_MARKER_RE = re.compile(r"utm_[a-z]+=[^&\"'\s]*sponsor|\bid=\\?[\"']sponsored\\?[\"']", re.IGNORECASE)

# The newsletter selling its own ad slots — appears in nearly every footer,
# says nothing about whether this issue had a sponsor.
_SELF_PROMO_RE = re.compile(
    r"become a sponsor|sponsor us|sponsor this|advertise with|interested in sponsoring|want to sponsor|sponsorship opportunit",
    re.IGNORECASE,
)

_BLOCK_TAG_RE = re.compile(r"<\s*/?\s*(?:p|div|h[1-6]|li|ul|ol|br|hr|tr|td|table|section|blockquote|figure|header|footer)\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def html_to_text(raw: str) -> str:
    """Crude but dependency-free: good enough for marker detection, not for
    building profiles. Decodes \\uXXXX escapes first because beehiiv ships
    issue HTML as an escaped JSON string inside a <script> tag."""
    s = _UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), raw)
    s = s.replace('\\"', '"').replace("\\/", "/")
    s = _BLOCK_TAG_RE.sub("\n", s)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in s.splitlines())
    return "\n".join(line for line in lines if line)


def find_sponsor_markers(raw_html: str) -> list[str]:
    """Snippets around each sponsor marker, self-promo matches excluded."""
    text = html_to_text(raw_html)
    snippets = []
    for m in TEXT_MARKER_RE.finditer(text):
        window = text[max(0, m.start() - 60) : m.end() + 60]
        if _SELF_PROMO_RE.search(window):
            continue
        snippets.append(" ".join(text[m.start() : m.end() + 60].split()))
    if not snippets:
        for m in HTML_MARKER_RE.finditer(raw_html):
            snippets.append(f"[link param] {m.group(0)}")
    return snippets


@dataclass
class ScreenResult:
    name: str
    vertical: str
    url: str
    status: str = "unscreened"
    feed_url: str = ""
    platform: str = ""
    feed_items: int = 0
    newest_issue: str = ""
    oldest_issue_in_feed: str = ""
    sampled: int = 0
    sponsored: int = 0
    sponsor_rate: float = 0.0
    example: str = ""
    error: str = ""
    samples: list[dict] = field(default_factory=list)


def _issue_html(fetcher: PoliteFetcher, issue: Issue, site_url: str) -> str:
    """Feed body plus the issue's own web page. Both are scanned because
    feeds can strip ad blocks (beehiiv does) and pages can hide them in markup."""
    if not issue.link or site_key(issue.link) != site_key(site_url):
        return issue.feed_html  # link-roundup feed: item is someone else's article, not an issue
    page = fetcher.get(issue.link)
    return issue.feed_html + "\n" + (page.text if page.status == 200 else "")


def screen_publisher(fetcher: PoliteFetcher, name: str, vertical: str, url: str, sample_size: int = 6) -> ScreenResult:
    res = ScreenResult(name=name, vertical=vertical, url=url)
    try:
        feed_url, parsed = discover_feed(fetcher, url)
        issues = feed_issues(parsed) if feed_url else []
        if len(issues) >= MIN_FEED_ITEMS:
            res.feed_url = feed_url
            res.platform = platform_of(parsed, feed_url)
        else:  # no feed, or a stub feed (UX Design Weekly's has 1 item)
            issues = sitemap_issues(fetcher, url)
            res.platform = "sitemap"
    except RobotsDisallowed:
        res.status = "robots_blocked"
        return res
    except CrawlDelayTooLong as e:
        res.status = "crawl_delay_too_long"
        res.error = str(e)
        return res
    if not issues:
        res.status = "no_feed"
        return res

    res.feed_items = len(issues)
    dates = [i.date for i in issues if i.date]
    if dates:
        res.newest_issue = max(dates).date().isoformat()
        res.oldest_issue_in_feed = min(dates).date().isoformat()
    if res.feed_items < MIN_FEED_ITEMS:
        res.status = "too_few_items"
        return res

    for issue in issues[:sample_size]:
        try:
            markers = find_sponsor_markers(_issue_html(fetcher, issue, url))
        except RobotsDisallowed:
            res.samples.append({"link": issue.link, "error": "robots_blocked"})
            continue
        except Exception as e:
            res.samples.append({"link": issue.link, "error": f"{type(e).__name__}: {e}"})
            continue
        res.sampled += 1
        res.samples.append({"link": issue.link, "markers": markers[:3]})
        if markers:
            res.sponsored += 1
            res.example = res.example or markers[0][:140]

    if res.sampled == 0:
        res.status = "unreachable_issues"
        res.error = "; ".join(s.get("error", "") for s in res.samples)[:200]
        return res
    res.sponsor_rate = round(res.sponsored / res.sampled, 2)
    res.status = "pass" if res.sponsored >= MIN_SPONSORED_ISSUES else "no_sponsor_signal"
    return res
