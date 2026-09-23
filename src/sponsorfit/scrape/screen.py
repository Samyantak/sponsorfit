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
from urllib.parse import urljoin, urlsplit

import feedparser

from sponsorfit.scrape.fetch import CrawlDelayTooLong, PoliteFetcher, RobotsDisallowed

FEED_FALLBACK_PATHS = ["/feed", "/rss/", "/rss", "/feed.xml", "/index.xml", "/atom.xml"]
MIN_FEED_ITEMS = 3
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


def _discover_feed(fetcher: PoliteFetcher, site_url: str):
    """Returns (feed_url, parsed_feed) or (None, None)."""
    candidates = []
    try:
        home = fetcher.get(site_url)
        for tag in re.findall(r"<link[^>]+>", home.text, re.IGNORECASE):
            if re.search(r"application/(?:rss|atom)\+xml", tag, re.IGNORECASE):
                href = re.search(r'href=["\']([^"\']+)', tag)
                if href:
                    candidates.append(urljoin(home.final_url, html.unescape(href.group(1))))
    except (RobotsDisallowed, CrawlDelayTooLong):
        raise
    except Exception:
        pass  # homepage unreachable/odd — still try the fallback paths
    base = site_url.rstrip("/")
    candidates += [base + p for p in FEED_FALLBACK_PATHS]

    seen = set()
    for feed_url in candidates:
        if feed_url in seen:
            continue
        seen.add(feed_url)
        try:
            r = fetcher.get(feed_url)
        except RobotsDisallowed:
            continue
        except Exception:
            continue
        if r.status != 200:
            continue
        parsed = feedparser.parse(r.text)
        if parsed.entries:
            return feed_url, parsed
    return None, None


def _platform(parsed, feed_url: str) -> str:
    gen = (parsed.feed.get("generator") or "").lower()
    for name in ("substack", "beehiiv", "ghost", "wordpress", "buttondown", "convertkit", "kit"):
        if name in gen or name in feed_url:
            return name
    if "/issues/" in (parsed.entries[0].get("link") or ""):
        return "cooperpress-like"
    return gen.split()[0] if gen else "unknown"


def _entry_date(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def _site_key(url: str) -> str:
    # Last two host labels: www.therundown.ai and therundown.ai match.
    # Naive for co.uk-style suffixes, fine for this candidate list.
    host = urlsplit(url).hostname or ""
    return ".".join(host.split(".")[-2:])


@dataclass
class Issue:
    link: str
    feed_html: str = ""  # body as carried in the feed, if any
    date: datetime | None = None


def _feed_issues(parsed) -> list[Issue]:
    issues = []
    for e in parsed.entries:
        body = e.content[0].get("value", "") if e.get("content") else ""
        issues.append(Issue(link=e.get("link", ""), feed_html=body or e.get("summary", ""), date=_entry_date(e)))
    return issues


_ISSUE_PATH_RE = re.compile(r"/(?:p|issues?|archive|newsletter)/[^/?#]+/?$")
_SITEMAP_LOC_RE = re.compile(r"<(url|sitemap)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)


def _sitemap_issues(fetcher: PoliteFetcher, site_url: str, max_child_sitemaps: int = 5) -> list[Issue]:
    """Fallback for newsletters with no feed (common on beehiiv): list issue
    pages from the sitemap, newest first by <lastmod> when present."""
    to_visit = fetcher.sitemaps(site_url) or [site_url.rstrip("/") + "/sitemap.xml"]
    issues, visited_children = [], 0
    while to_visit:
        sm_url = to_visit.pop(0)
        try:
            r = fetcher.get(sm_url)
        except (RobotsDisallowed, CrawlDelayTooLong):
            raise
        except Exception:
            continue
        if r.status != 200:
            continue
        for kind, block in _SITEMAP_LOC_RE.findall(r.text):
            loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.DOTALL)
            if not loc:
                continue
            loc = html.unescape(loc.group(1))
            if kind.lower() == "sitemap":  # sitemap index → follow a few children
                if visited_children < max_child_sitemaps:
                    visited_children += 1
                    to_visit.append(loc)
                continue
            if _site_key(loc) == _site_key(site_url) and _ISSUE_PATH_RE.search(urlsplit(loc).path):
                lastmod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block)
                issues.append(Issue(link=loc, date=_parse_date(lastmod.group(1)) if lastmod else None))
    dated = sorted((i for i in issues if i.date), key=lambda i: i.date, reverse=True)
    undated = [i for i in issues if not i.date][::-1]  # sitemaps usually list oldest first
    return dated + undated


def _parse_date(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _issue_html(fetcher: PoliteFetcher, issue: Issue, site_url: str) -> str:
    """Feed body plus the issue's own web page. Both are scanned because
    feeds can strip ad blocks (beehiiv does) and pages can hide them in markup."""
    if not issue.link or _site_key(issue.link) != _site_key(site_url):
        return issue.feed_html  # link-roundup feed: item is someone else's article, not an issue
    page = fetcher.get(issue.link)
    return issue.feed_html + "\n" + (page.text if page.status == 200 else "")


def screen_publisher(fetcher: PoliteFetcher, name: str, vertical: str, url: str, sample_size: int = 6) -> ScreenResult:
    res = ScreenResult(name=name, vertical=vertical, url=url)
    try:
        feed_url, parsed = _discover_feed(fetcher, url)
        issues = _feed_issues(parsed) if feed_url else []
        if len(issues) >= MIN_FEED_ITEMS:
            res.feed_url = feed_url
            res.platform = _platform(parsed, feed_url)
        else:  # no feed, or a stub feed (UX Design Weekly's has 1 item)
            issues = _sitemap_issues(fetcher, url)
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
