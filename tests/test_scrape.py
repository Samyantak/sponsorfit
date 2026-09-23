import pytest

from sponsorfit.scrape.fetch import PoliteFetcher, RobotsDisallowed
from sponsorfit.scrape.screen import find_sponsor_markers

# --- sponsor markers: real formats seen while probing sources ------------


def test_substack_brought_to_you_by():
    html = "<blockquote><p><strong>Brought to you by:</strong></p><p><a href='x'>Optimizely</a></p></blockquote>"
    assert find_sponsor_markers(html)


def test_beehiiv_escaped_json_header():
    # beehiiv ships issue HTML as an escaped JSON string inside <script>
    # Escapes built from parts so no editor or tool can decode them in the source.
    lt, gt = "\\" + "u003c", "\\" + "u003e"
    raw = f'"html":"{lt}h6 style=\\"margin:0\\"{gt}TOGETHER WITH UNWRAP{lt}/h6{gt}{lt}p{gt}Body{lt}/p{gt}"'
    assert "<h6" not in raw  # guard: this test is only meaningful with the escapes intact
    markers = find_sponsor_markers(raw)
    assert markers and "UNWRAP" in markers[0]


def test_cooperpress_utm_sponsored_link():
    html = '<a href="https://www.meticulous.ai/?utm_source=jsweekly&amp;utm_content=sponsored">Meticulous</a>'
    assert find_sponsor_markers(html)


def test_cooperpress_trailing_sponsor_label():
    # The feed rewrites links (no utm params); the only marker is this label.
    html = '<p>ParadeDB <span style="font-size: 0.9em">sponsor</span></p>'
    markers = find_sponsor_markers(html)
    assert markers and "ParadeDB" in markers[0]


def test_prose_sponsored_listings_is_not_a_sponsor():
    html = "<p>Checkouts can route purchases around Amazon’s sponsored listings, the engine of its ad business.</p>"
    assert find_sponsor_markers(html) == []


def test_together_with_mid_sentence_is_not_a_sponsor():
    html = "<p>She built the company together with her co-founder in 2019.</p>"
    assert find_sponsor_markers(html) == []


def test_self_promo_footer_is_not_a_sponsor():
    html = "<footer><p>Interested in sponsoring? Become a sponsor — presented by our ads team.</p></footer>"
    assert find_sponsor_markers(html) == []


# --- polite fetcher: robots.txt + cache, network mocked ------------------


class FakeFetcher(PoliteFetcher):
    def __init__(self, responses, **kw):
        super().__init__(min_interval_s=0, **kw)
        self.responses = responses  # url -> (status, body) or Exception
        self.requested = []

    def _request(self, url):
        self.requested.append(url)
        r = self.responses.get(url, (404, ""))
        if isinstance(r, Exception):
            raise r
        status, body = r
        return status, url, "text/html", body


def test_robots_disallow_blocks_fetch(tmp_path):
    f = FakeFetcher({"https://a.com/robots.txt": (200, "User-agent: *\nDisallow: /private/\n")}, cache_dir=str(tmp_path))
    with pytest.raises(RobotsDisallowed):
        f.get("https://a.com/private/page")
    assert "https://a.com/private/page" not in f.requested


def test_robots_wildcards_are_honored(tmp_path):
    # Substack's real robots.txt uses wildcards; urllib.robotparser ignores them.
    f = FakeFetcher({"https://a.com/robots.txt": (200, "User-agent: *\nDisallow: /p/*/comment/*\n")}, cache_dir=str(tmp_path))
    assert not f.allowed("https://a.com/p/some-post/comment/123")
    assert f.allowed("https://a.com/p/some-post")


def test_robots_404_means_allow_all(tmp_path):
    f = FakeFetcher({"https://a.com/page": (200, "ok")}, cache_dir=str(tmp_path))
    assert f.get("https://a.com/page").text == "ok"


def test_robots_unreachable_means_disallow_all(tmp_path):
    f = FakeFetcher({"https://a.com/robots.txt": OSError("connection refused")}, cache_dir=str(tmp_path))
    with pytest.raises(RobotsDisallowed):
        f.get("https://a.com/page")


def test_second_fetch_is_served_from_cache(tmp_path):
    responses = {"https://a.com/page": (200, "hello")}
    f = FakeFetcher(responses, cache_dir=str(tmp_path))
    assert f.get("https://a.com/page").from_cache is False
    again = FakeFetcher(responses, cache_dir=str(tmp_path)).get("https://a.com/page")
    assert again.from_cache is True and again.text == "hello"


def test_error_responses_are_not_cached(tmp_path):
    f = FakeFetcher({"https://a.com/page": (503, "")}, cache_dir=str(tmp_path))
    f.get("https://a.com/page")
    f.get("https://a.com/page")
    assert f.requested.count("https://a.com/page") == 2


def test_long_crawl_delay_is_declined_not_violated(tmp_path):
    from sponsorfit.scrape.fetch import CrawlDelayTooLong

    # thespoon.tech really does ask for Crawl-delay: 600
    f = FakeFetcher({"https://a.com/robots.txt": (200, "User-agent: *\nCrawl-delay: 600\n")}, cache_dir=str(tmp_path))
    with pytest.raises(CrawlDelayTooLong):
        f.get("https://a.com/page")
    assert "https://a.com/page" not in f.requested


def test_link_roundup_items_are_not_followed_off_site(tmp_path):
    from sponsorfit.scrape.archive import Issue
    from sponsorfit.scrape.screen import _issue_html

    f = FakeFetcher({}, cache_dir=str(tmp_path))
    issue = Issue(link="https://github.com/someone/repo", feed_html="<p>short blurb</p>")
    assert _issue_html(f, issue, "https://sidebar.io") == "<p>short blurb</p>"
    assert not any("github.com" in u for u in f.requested)


def test_beehiiv_ad_slot_markup():
    # Real beehiiv page structure (The Neuron): ad slots are id="sponsored" divs
    # under a "FROM OUR PARTNERS" heading — neither appears in the RSS body.
    html = '<div id="sponsored"><p><b>FROM OUR PARTNERS</b></p><p>Try Acme today.</p></div>'
    assert find_sponsor_markers(html)
    assert find_sponsor_markers('<div id="sponsored"><p>Try Acme.</p></div>')


def test_sitemap_fallback_follows_index_and_orders_newest_first(tmp_path):
    from sponsorfit.scrape.archive import sitemap_issues

    index = """<sitemapindex><sitemap><loc>https://news.example.com/sitemap-posts.xml</loc></sitemap></sitemapindex>"""
    posts = """<urlset>
      <url><loc>https://news.example.com/p/old-issue</loc><lastmod>2026-01-05</lastmod></url>
      <url><loc>https://news.example.com/p/new-issue</loc><lastmod>2026-09-01T08:00:00Z</lastmod></url>
      <url><loc>https://news.example.com/about</loc></url>
      <url><loc>https://elsewhere.com/p/not-ours</loc></url>
    </urlset>"""
    f = FakeFetcher(
        {
            "https://news.example.com/robots.txt": (200, "User-agent: *\nAllow: /\nSitemap: https://news.example.com/sitemap.xml\n"),
            "https://news.example.com/sitemap.xml": (200, index),
            "https://news.example.com/sitemap-posts.xml": (200, posts),
        },
        cache_dir=str(tmp_path),
    )
    issues = sitemap_issues(f, "https://news.example.com")
    assert [i.link for i in issues] == ["https://news.example.com/p/new-issue", "https://news.example.com/p/old-issue"]
