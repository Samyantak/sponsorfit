import json
from datetime import datetime, timedelta, timezone

from sponsorfit.scrape.archive import (
    Issue,
    published_date_from_html,
    select_issues,
    substack_issues,
)
from tests.test_scrape import FakeFetcher

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
SINCE = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _daily(n_days: int) -> list[Issue]:
    return [Issue(link=f"https://n.com/p/{d}", date=NOW - timedelta(days=d)) for d in range(n_days)]


def test_selection_spreads_across_window_not_latest_n():
    picked = select_issues(_daily(700), n=40, since=SINCE, now=NOW)
    assert len(picked) == 40
    dates = sorted(i.date for i in picked)
    assert dates[0] - SINCE < timedelta(days=3)  # reaches back to the window start
    assert NOW - dates[-1] < timedelta(days=1)  # and up to now
    assert (dates[-1] - dates[0]).days > 600  # not clustered in the last few weeks


def test_selection_excludes_issues_outside_window():
    old = [Issue(link="https://n.com/p/ancient", date=datetime(2020, 1, 1, tzinfo=timezone.utc))]
    picked = select_issues(old + _daily(10), n=40, since=SINCE, now=NOW)
    assert all(i.date >= SINCE for i in picked)


def test_selection_dedupes_and_prefers_dated_copy():
    a = Issue(link="https://n.com/p/x")
    b = Issue(link="https://n.com/p/x", date=NOW)
    picked = select_issues([a, b], n=5, since=SINCE, now=NOW)
    assert len(picked) == 1 and picked[0].date == NOW


def test_undated_issues_are_trimmed_to_what_fits_the_window():
    # Weekly cadence from dated feed items; 200 undated numbered issues newest-first.
    feed = [Issue(link=f"https://n.com/issues/{900 - k}", date=NOW - timedelta(weeks=k)) for k in range(4)]
    index = [Issue(link=f"https://n.com/issues/{n}") for n in range(896, 696, -1)]
    picked = select_issues(feed + index, n=40, since=SINCE, now=NOW)
    numbers = [int(i.link.rsplit("/", 1)[1]) for i in picked if not i.date]
    # ~90 weeks fit since 2025-01-01, so nothing older than ~issue 806 should be chosen
    assert min(numbers) > 896 - 95
    assert len(picked) == 40


def test_published_date_prefers_page_metadata():
    page = '<meta property="article:published_time" content="2026-03-04T09:00:00Z"><time datetime="2020-01-01">'
    assert published_date_from_html(page) == datetime(2026, 3, 4, 9, tzinfo=timezone.utc)


def test_published_date_from_json_ld():
    page = '<script type="application/ld+json">{"datePublished":"2025-11-20T12:00:00.000Z"}</script>'
    assert published_date_from_html(page).date().isoformat() == "2025-11-20"


def test_substack_api_paginates_until_window_start_and_skips_threads(tmp_path):
    def page(posts):
        return (200, json.dumps(posts))

    base = "https://s.com/api/v1/archive?sort=new&limit=2&offset="
    f = FakeFetcher(
        {
            base.replace("limit=2&offset=", "offset=0&limit=2"): page(
                [
                    {"canonical_url": "https://s.com/p/a", "post_date": "2026-09-01T00:00:00Z", "audience": "everyone", "type": "newsletter"},
                    {"canonical_url": "https://s.com/p/t", "post_date": "2026-08-01T00:00:00Z", "audience": "everyone", "type": "thread"},
                ]
            ),
            base.replace("limit=2&offset=", "offset=2&limit=2"): page(
                [
                    {"canonical_url": "https://s.com/p/b", "post_date": "2024-12-01T00:00:00Z", "audience": "only_paid", "type": "newsletter"},
                    {"canonical_url": "https://s.com/p/c", "post_date": "2024-11-01T00:00:00Z", "audience": "everyone", "type": "newsletter"},
                ]
            ),
        },
        cache_dir=str(tmp_path),
    )
    issues = substack_issues(f, "https://s.com", since=SINCE, page_size=2)
    assert [i.link for i in issues] == ["https://s.com/p/a", "https://s.com/p/b", "https://s.com/p/c"]
    assert issues[1].audience == "only_paid"
    assert not any("offset=4" in u for u in f.requested)  # stopped once past the window start


def test_cooperpress_dates_estimated_from_issue_number(tmp_path):
    from sponsorfit.scrape.archive import cooperpress_issues

    index = "".join(f'<a href="/issues/{n}">#{n}</a>' for n in range(803, 700, -1))
    f = FakeFetcher({"https://jw.com/issues": (200, index)}, cache_dir=str(tmp_path))
    anchors = [Issue(link="https://jw.com/issues/803", date=NOW)]
    issues = {i.link: i for i in cooperpress_issues(f, "https://jw.com", anchors)}
    assert issues["https://jw.com/issues/803"].date == NOW
    assert issues["https://jw.com/issues/751"].date == NOW - timedelta(weeks=52)
    assert issues["https://jw.com/issues/751"].source == "cooperpress_estimated"


def test_beehiiv_archive_pairs_slug_with_its_own_date(tmp_path):
    from sponsorfit.scrape.archive import beehiiv_issues

    def post(slug, ts, date_first=False, tag=None):
        tags = f'"content_tags":[{{"slug":"{tag}"}}],' if tag else ""
        fields = [f'"override_scheduled_at":"{ts}"', f'"slug":"{slug}"'] if date_first else [f'"slug":"{slug}"', f'"scheduled_at":"{ts}"']
        return f'<a href="/p/{slug}">x</a>' + "{" + tags + ",".join(fields) + "}"

    # Real traps: a content-tag "slug" (was producing /p/fintech 404s), and
    # sites that put the date field before the slug (Notable Daily).
    page1 = post("new-one", "2026-09-01T10:45:00Z", tag="fintech") + post("older-one", "2025-06-01T10:45:00Z", date_first=True)
    page2 = post("ancient", "2024-06-01T10:45:00Z")
    f = FakeFetcher(
        {"https://b.com/archive?page=1": (200, page1), "https://b.com/archive?page=2": (200, page2)},
        cache_dir=str(tmp_path),
    )
    issues = {i.link: i.date.date().isoformat() for i in beehiiv_issues(f, "https://b.com", since=SINCE)}
    assert issues == {
        "https://b.com/p/new-one": "2026-09-01",
        "https://b.com/p/older-one": "2025-06-01",
        "https://b.com/p/ancient": "2024-06-01",
    }
    assert "https://b.com/archive?page=3" not in f.requested  # stopped once a whole page was before the window


def _rss(links_dates):
    items = "".join(f"<item><link>{l}</link><pubDate>{d}</pubDate></item>" for l, d in links_dates)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'


def test_paged_feed_stops_when_server_ignores_paged(tmp_path):
    import feedparser

    from sponsorfit.scrape.archive import feed_issues, wordpress_paged_issues

    page1 = _rss([("https://w.com/a", "Mon, 01 Sep 2026 00:00:00 +0000")])
    f = FakeFetcher({"https://w.com/feed/?paged=2": (200, page1)}, cache_dir=str(tmp_path))
    first = feed_issues(feedparser.parse(page1))
    # A non-WordPress server that ignores ?paged just repeats page 1: that must
    # yield nothing (so the site isn't mislabelled as WordPress), after one request.
    assert wordpress_paged_issues(f, "https://w.com/feed/", since=SINCE, first_page=first) == []
    assert "https://w.com/feed/?paged=3" not in f.requested
