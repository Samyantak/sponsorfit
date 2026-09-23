"""Issue discovery: turn a newsletter's URL into a dated list of its issues.

Shared by Phase 1.1 screening (a handful of recent issues) and Phase 1.2
collection (a date-spread sample of the archive). Sources, in order of how
much they can be trusted for dates:

- Substack archive API — exact post dates, full history, paid/free flag
- beehiiv /archive?page=N — exact send dates (scheduled_at), full history
- RSS/Atom feed (paged on WordPress) — exact dates, but shallow elsewhere
- Cooperpress /issues index — full history; pages carry no date, so dates
  are estimated from issue number (strictly weekly) and marked as such
- sitemap.xml — last resort; <lastmod> can be wildly wrong (bulk re-saves)

Dates from anything but the issue page itself are provisional: collection
re-reads the publish date from each fetched page.
"""

import html
import json
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import feedparser

from sponsorfit.scrape.fetch import CrawlDelayTooLong, PoliteFetcher, RobotsDisallowed

FEED_FALLBACK_PATHS = ["/feed", "/rss/", "/rss", "/feed.xml", "/index.xml", "/atom.xml"]
MIN_FEED_ITEMS = 3


@dataclass
class Issue:
    link: str
    feed_html: str = ""  # body as carried in the feed, if any
    date: datetime | None = None
    source: str = ""  # feed | substack_api | beehiiv_archive | wp_paged | cooperpress_estimated | sitemap
    audience: str = ""  # Substack only: everyone | only_paid | founding


# --- small helpers ------------------------------------------------------


def site_key(url: str) -> str:
    # Last two host labels: www.therundown.ai and therundown.ai match.
    # Naive for co.uk-style suffixes, fine for this corpus.
    host = urlsplit(url).hostname or ""
    return ".".join(host.split(".")[-2:])


def parse_date(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _entry_date(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


_PUBLISHED_PATTERNS = [
    re.compile(r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']article:published_time', re.IGNORECASE),
    re.compile(r'\\?"datePublished\\?"\s*:\s*\\?"([^"\\]+)'),
    re.compile(r'<time[^>]+datetime=["\']([^"\']+)', re.IGNORECASE),
]


def published_date_from_html(page_html: str) -> datetime | None:
    """The issue page's own publish date — the only date we fully trust."""
    for pat in _PUBLISHED_PATTERNS:
        m = pat.search(page_html)
        if m and (d := parse_date(m.group(1))):
            return d
    return None


# --- feeds ----------------------------------------------------------------


def discover_feed(fetcher: PoliteFetcher, site_url: str):
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
        except Exception:
            continue
        if r.status != 200:
            continue
        parsed = feedparser.parse(r.text)
        if parsed.entries:
            return feed_url, parsed
    return None, None


def platform_of(parsed, feed_url: str) -> str:
    gen = (parsed.feed.get("generator") or "").lower()
    for name in ("substack", "beehiiv", "ghost", "wordpress", "buttondown", "convertkit", "kit"):
        if name in gen or name in feed_url:
            return name
    if "/issues/" in (parsed.entries[0].get("link") or ""):
        return "cooperpress-like"
    return gen.split()[0] if gen else "unknown"


def feed_issues(parsed, source: str = "feed") -> list[Issue]:
    issues = []
    for e in parsed.entries:
        body = e.content[0].get("value", "") if e.get("content") else ""
        issues.append(Issue(link=e.get("link", ""), feed_html=body or e.get("summary", ""), date=_entry_date(e), source=source))
    return issues


def wordpress_paged_issues(fetcher: PoliteFetcher, feed_url: str, since: datetime, first_page: list[Issue] = (), max_pages: int = 40) -> list[Issue]:
    """WordPress feeds page back through history with ?paged=N."""
    issues, seen = [], {i.link for i in first_page}
    sep = "&" if "?" in feed_url else "?"
    for page in range(2, max_pages + 1):
        try:
            r = fetcher.get(f"{feed_url}{sep}paged={page}")
        except (RobotsDisallowed, CrawlDelayTooLong):
            raise
        except Exception:
            break
        if r.status != 200:
            break
        batch = [i for i in feed_issues(feedparser.parse(r.text), source="wp_paged") if i.link not in seen]
        if not batch:
            break  # empty, or a non-WordPress server ignoring ?paged and repeating page 1
        seen.update(i.link for i in batch)
        issues += batch
        if all(i.date and i.date < since for i in batch):
            break
    return issues


# --- platform archives ----------------------------------------------------


def substack_issues(fetcher: PoliteFetcher, site_url: str, since: datetime, page_size: int = 50) -> list[Issue]:
    """Substack's public archive API (allowed by robots.txt): exact dates and
    the paid/free flag. Paid posts still expose their opening, which is where
    sponsor blocks sit, so they're kept and flagged rather than dropped."""
    base = site_url.rstrip("/")
    issues, offset = [], 0
    while True:
        r = fetcher.get(f"{base}/api/v1/archive?sort=new&offset={offset}&limit={page_size}")
        if r.status != 200:
            break
        try:
            posts = json.loads(r.text)
        except ValueError:
            break
        if not posts:
            break
        for p in posts:
            if p.get("type") == "thread":
                continue  # discussion threads, not issues
            issues.append(
                Issue(
                    link=p.get("canonical_url", ""),
                    date=parse_date(p.get("post_date", "") or ""),
                    source="substack_api",
                    audience=p.get("audience", ""),
                )
            )
        oldest = parse_date(posts[-1].get("post_date", "") or "")
        if oldest and oldest < since:
            break
        offset += page_size
    return issues


_ISSUE_NUMBER_RE = re.compile(r"/issues/(\d+)/?$")


def cooperpress_issues(fetcher: PoliteFetcher, site_url: str, anchors: list[Issue]) -> list[Issue]:
    """Cooperpress lists every numbered issue at /issues, but issue pages carry
    no date. They publish strictly weekly, so each number's date is estimated
    from the nearest dated feed issue (±1–2 weeks where a holiday week was
    skipped — fine for a month-granularity time split). source marks it."""
    base = site_url.rstrip("/")
    r = fetcher.get(f"{base}/issues")
    if r.status != 200:
        return []
    numbers = sorted({int(n) for n in re.findall(r'href=["\']/issues/(\d+)["\']', r.text)}, reverse=True)
    dated = [(int(m.group(1)), i.date) for i in anchors if i.date and (m := _ISSUE_NUMBER_RE.search(i.link))]
    issues = []
    for n in numbers:
        date = None
        if dated:
            anchor_n, anchor_date = min(dated, key=lambda a: abs(a[0] - n))
            date = anchor_date - timedelta(weeks=anchor_n - n)
        issues.append(Issue(link=f"{base}/issues/{n}", date=date, source="cooperpress_estimated"))
    return issues


_BEEHIIV_POST_LINK_RE = re.compile(r'/p/([a-z0-9][a-z0-9-]*)(?=["\'\\?#/])')
_BEEHIIV_DATE_RE = re.compile(r'\\?"(?:override_scheduled_at|scheduled_at|publish_date)\\?":\\?"(\d{4}-\d\d-\d\dT[^"\\]+)')


def _beehiiv_posts_on_page(page: str) -> list[tuple[str, datetime | None]]:
    """(slug, approx send date) for each post on one archive page, newest first.

    Slugs come only from /p/<slug> links — the JSON also has "slug" keys for
    content tags and authors, which produced 404s (/p/fintech, /p/mena) when
    matched directly. Each post's date is the date field nearest its
    "slug" key; field order differs between sites, so look both ways. Only
    approximate: collection re-reads the true date from the issue page."""
    slugs = list(dict.fromkeys(_BEEHIIV_POST_LINK_RE.findall(page)))
    dates = [(m.start(), parse_date(m.group(1))) for m in _BEEHIIV_DATE_RE.finditer(page)]
    posts = []
    for slug in slugs:
        key = re.search(r'\\?"slug\\?":\\?"' + re.escape(slug) + r'\\?"', page)
        near = [(abs(pos - key.start()), d) for pos, d in dates if d and abs(pos - key.start()) < 1500] if key else []
        posts.append((slug, min(near, key=lambda x: x[0])[1] if near else None))
    return posts


def beehiiv_issues(fetcher: PoliteFetcher, site_url: str, since: datetime, max_pages: int = 120) -> list[Issue]:
    """beehiiv's /archive?page=N lists posts newest-first with their send
    dates embedded — no need to open every issue to find the window.
    Needed because beehiiv sitemap <lastmod> is unreliable (Exec Sum's
    shows 2025-04-15 on a 2022 issue: the archive was re-saved in bulk)."""
    base = site_url.rstrip("/")
    issues: dict[str, Issue] = {}
    for page_no in range(1, max_pages + 1):
        try:
            r = fetcher.get(f"{base}/archive?page={page_no}")
        except (RobotsDisallowed, CrawlDelayTooLong):
            raise
        except Exception:
            break
        if r.status != 200:
            break
        new = [(s, d) for s, d in _beehiiv_posts_on_page(r.text) if f"{base}/p/{s}" not in issues]
        if not new:
            break
        for slug, date in new:
            issues[f"{base}/p/{slug}"] = Issue(link=f"{base}/p/{slug}", date=date, source="beehiiv_archive")
        dates = [d for _, d in new if d]
        if dates and max(dates) < since:
            break
    return list(issues.values())


_ISSUE_PATH_RE = re.compile(r"/(?:p|issues?|archive|newsletter)/[^/?#]+/?$")
_SITEMAP_ENTRY_RE = re.compile(r"<(url|sitemap)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)


def sitemap_issues(fetcher: PoliteFetcher, site_url: str, max_child_sitemaps: int = 5) -> list[Issue]:
    """Issue pages from the sitemap, newest first by <lastmod> when present.
    Covers beehiiv newsletters with RSS switched off."""
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
        for kind, block in _SITEMAP_ENTRY_RE.findall(r.text):
            loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.DOTALL)
            if not loc:
                continue
            loc = html.unescape(loc.group(1))
            if kind.lower() == "sitemap":  # sitemap index → follow a few children
                if visited_children < max_child_sitemaps:
                    visited_children += 1
                    to_visit.append(loc)
                continue
            if site_key(loc) == site_key(site_url) and _ISSUE_PATH_RE.search(urlsplit(loc).path):
                lastmod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block)
                issues.append(Issue(link=loc, date=parse_date(lastmod.group(1)) if lastmod else None, source="sitemap"))
    dated = sorted((i for i in issues if i.date), key=lambda i: i.date, reverse=True)
    undated = [i for i in issues if not i.date][::-1]  # sitemaps usually list oldest first
    return dated + undated


# --- selection ------------------------------------------------------------


def _spread(items: list, n: int) -> list:
    """n items evenly spaced across the list, always including both ends."""
    if len(items) <= n:
        return list(items)
    step = (len(items) - 1) / (n - 1)
    return [items[round(k * step)] for k in range(n)]


def select_issues(issues: list[Issue], n: int, since: datetime, now: datetime | None = None) -> list[Issue]:
    """Pick n issues spread evenly over [since, now] rather than the latest n:
    for a daily newsletter the latest 40 cover ~2 months, which gives few
    distinct sponsors and nothing to split on by time.

    Undated issues (Cooperpress, sitemaps without <lastmod>) are assumed
    newest-first; we keep only as many as the observed cadence says fit in
    the window, then spread across those."""
    now = now or datetime.now(timezone.utc)
    by_link: dict[str, Issue] = {}
    for i in issues:
        if i.link and (i.link not in by_link or (i.date and not by_link[i.link].date)):
            by_link[i.link] = i
    unique = list(by_link.values())

    dated = sorted((i for i in unique if i.date and since <= i.date <= now), key=lambda i: i.date)
    undated = [i for i in unique if not i.date]
    if len(dated) >= n or not undated:
        return _spread(dated, n)

    all_dates = sorted(i.date for i in unique if i.date)
    gaps = [(b - a).total_seconds() for a, b in zip(all_dates, all_dates[1:]) if b > a]
    cadence_days = statistics.median(gaps) / 86400 if gaps else 7.0
    fit = max(1, int((now - since).days / max(cadence_days, 1.0)))
    return dated + _spread(undated[:fit][::-1], n - len(dated))


def enumerate_issues(fetcher: PoliteFetcher, site_url: str, since: datetime) -> tuple[str, list[Issue]]:
    """(platform, issues) using the deepest source available for the platform."""
    feed_url, parsed = discover_feed(fetcher, site_url)
    issues = feed_issues(parsed) if feed_url else []
    platform = platform_of(parsed, feed_url) if feed_url else "sitemap"

    if platform == "substack":
        issues += substack_issues(fetcher, site_url, since)
        return platform, _own(issues, site_url)
    if platform == "cooperpress-like":
        issues += cooperpress_issues(fetcher, site_url, anchors=issues)
        return platform, _own(issues, site_url)

    # Many WordPress sites strip the feed's generator tag (Hospitalogy,
    # tim.blog), so any unknown-platform feed gets a paging attempt too.
    if feed_url and platform in ("wordpress", "unknown"):
        if paged := wordpress_paged_issues(fetcher, feed_url, since, first_page=issues):
            return "wordpress", _own(issues + paged, site_url)
    if beehiiv := beehiiv_issues(fetcher, site_url, since):
        if min((i.date for i in beehiiv if i.date), default=since) > since:
            # Archive paging never reached the window start — broken paging
            # (Health Tech Nerds redirects /archive and drops ?page=, so every
            # page repeats). Add the sitemap too; collection verifies its dates.
            beehiiv += sitemap_issues(fetcher, site_url)
        return "beehiiv", _own(issues + beehiiv, site_url)
    return platform, _own(issues + sitemap_issues(fetcher, site_url), site_url)


def _own(issues: list[Issue], site_url: str) -> list[Issue]:
    return [i for i in issues if i.link and site_key(i.link) == site_key(site_url)]
