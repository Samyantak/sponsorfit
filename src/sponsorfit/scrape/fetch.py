"""Polite HTTP fetcher — the single path every scraper goes through, the same
way every LLM call goes through llm.py.

Guarantees, per the roadmap's Phase 1 rules:
- robots.txt is checked before every fetch (RFC 9309 semantics via protego,
  which — unlike urllib.robotparser — supports `*`/`$` wildcards).
- Requests to the same host are spaced at least `min_interval_s` apart
  (or the site's Crawl-delay, if larger).
- Every successful response is cached to data/raw/http/, so nothing is ever
  fetched twice. Cached files are raw and never edited.
"""

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from protego import Protego

USER_AGENT = "SponsorFitResearchBot/0.1 (non-commercial research; respects robots.txt)"
ROBOTS_TOKEN = "SponsorFitResearchBot"
DEFAULT_CACHE_DIR = os.path.join("data", "raw", "http")


class RobotsDisallowed(Exception):
    pass


class CrawlDelayTooLong(Exception):
    """The site's Crawl-delay exceeds what this caller is willing to wait.
    We never fetch faster than asked — we decline instead."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    text: str
    from_cache: bool


class PoliteFetcher:
    def __init__(
        self,
        cache_dir: str = DEFAULT_CACHE_DIR,
        min_interval_s: float = 2.0,
        timeout_s: float = 20.0,
        user_agent: str = USER_AGENT,
        max_crawl_delay_s: float = 30.0,
    ):
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.max_crawl_delay_s = max_crawl_delay_s
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        self._robots: dict[str, Protego | None] = {}  # None = unreachable → disallow all
        self._last_request: dict[str, float] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # --- public ---------------------------------------------------------

    def get(self, url: str) -> FetchResult:
        cached = self._read_cache(url)
        if cached is not None:
            return cached
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        delay = self._crawl_delay(self._origin(url))
        if delay > self.max_crawl_delay_s:
            raise CrawlDelayTooLong(f"{url} (Crawl-delay {delay:.0f}s)")
        status, final_url, content_type, text = self._throttled_request(url)
        result = FetchResult(url, final_url, status, content_type, text, from_cache=False)
        if status == 200:
            self._write_cache(result)
        return result

    def allowed(self, url: str) -> bool:
        host = self._origin(url)
        with self._lock_for(host):
            if host not in self._robots:
                self._robots[host] = self._load_robots(host)
        rules = self._robots[host]
        return rules is not None and rules.can_fetch(url, ROBOTS_TOKEN)

    def sitemaps(self, url: str) -> list[str]:
        """Sitemap URLs the site declares in robots.txt."""
        self.allowed(url)  # ensures robots.txt is loaded
        rules = self._robots.get(self._origin(url))
        return list(rules.sitemaps) if rules else []

    # --- robots.txt -----------------------------------------------------

    def _load_robots(self, origin: str) -> Protego | None:
        # RFC 9309: 4xx → no restrictions; 5xx / unreachable → assume disallow all.
        try:
            status, _, _, text = self._throttled_request(origin + "/robots.txt")
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        if status == 200:
            return Protego.parse(text)
        if 400 <= status < 500:
            return Protego.parse("")
        return None

    def _crawl_delay(self, origin: str) -> float:
        rules = self._robots.get(origin)
        delay = rules.crawl_delay(ROBOTS_TOKEN) if rules else None
        return max(self.min_interval_s, float(delay or 0))

    # --- network --------------------------------------------------------

    def _throttled_request(self, url: str) -> tuple[int, str, str, str]:
        origin = self._origin(url)
        with self._lock_for(origin):
            wait = self._last_request.get(origin, 0) + self._crawl_delay(origin) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                return self._request(url)
            finally:
                self._last_request[origin] = time.monotonic()

    def _request(self, url: str) -> tuple[int, str, str, str]:
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.status, resp.geturl(), resp.headers.get("Content-Type", ""), body.decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, url, e.headers.get("Content-Type", "") if e.headers else "", ""

    # --- cache ----------------------------------------------------------

    def _cache_path(self, url: str) -> str:
        host = urlsplit(url).netloc.replace(":", "_")
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return os.path.join(self.cache_dir, host, f"{digest}.json")

    def _read_cache(self, url: str) -> FetchResult | None:
        path = self._cache_path(url)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return FetchResult(d["url"], d["final_url"], d["status"], d["content_type"], d["text"], from_cache=True)

    def _write_cache(self, r: FetchResult) -> None:
        path = self._cache_path(r.url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "url": r.url,
                    "final_url": r.final_url,
                    "status": r.status,
                    "content_type": r.content_type,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "text": r.text,
                },
                f,
            )

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    def _lock_for(self, origin: str) -> threading.Lock:
        with self._locks_guard:
            return self._host_locks.setdefault(origin, threading.RLock())
