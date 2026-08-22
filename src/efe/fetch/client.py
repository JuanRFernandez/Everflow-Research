"""The HTTP client.

Honest User-Agent naming the crawler and a contact address, conservative timeouts, a
response size cap, retries only on conditions worth retrying, robots.txt obeyed, and
every response written to the disk cache.
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from efe.config import FetchConfig
from efe.fetch.cache import CachedPage, PageCache
from efe.fetch.ratelimit import RateLimiter
from efe.fetch.robots import RobotsCache

log = logging.getLogger(__name__)

ROBOTS_BLOCKED = "robots.txt disallows this URL"
CRAWL_DELAY_TOO_LONG = "robots.txt Crawl-delay exceeds the configured maximum"


class _Retryable(Exception):
    """Marks a response worth retrying: a timeout, a 5xx, or a 429."""


def domain_of_url(url: str) -> str:
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


class Fetcher:
    """Cache-first, robots-respecting, rate-limited page fetcher."""

    def __init__(
        self,
        config: FetchConfig,
        cache: PageCache,
        limiter: RateLimiter | None = None,
        robots: RobotsCache | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.cache = cache
        self.limiter = limiter or RateLimiter(
            config.per_domain_delay_seconds, config.global_concurrency
        )
        self.robots = robots or RobotsCache(config.user_agent, config.respect_robots)
        self._client = client
        self._owns_client = client is None
        self.requests_made = 0
        self.skipped_domains: dict[str, str] = {}
        self._consecutive_failures: dict[str, int] = {}

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> Fetcher:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en,de;q=0.9,fr;q=0.8,it;q=0.7,es;q=0.6,pt;q=0.5",
                },
                timeout=httpx.Timeout(
                    self.config.timeout_seconds, connect=self.config.connect_timeout_seconds
                ),
                follow_redirects=self.config.follow_redirects,
                max_redirects=self.config.max_redirects,
                verify=self.config.verify_tls,
                http2=True,
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- robots -------------------------------------------------------------
    async def ensure_robots(self, domain: str) -> None:
        """Fetch and record robots.txt for a domain, once."""
        if not self.config.respect_robots or self.robots.has(domain):
            return
        url = f"https://{domain}/robots.txt"
        page = await self._request(url, domain, is_robots=True)
        # A 404 here is normal and means "no restrictions"; a 429 or a TLS failure
        # is the site refusing us, and counts toward abandoning the domain.
        if not page.ok and page.status != 404:
            self._note_outcome(domain, page)
        policy = self.robots.record(
            domain, page.body if page.ok else None, error=page.error or ""
        )
        if (
            self.config.honour_crawl_delay
            and policy.crawl_delay is not None
        ):
            if policy.crawl_delay > self.config.max_crawl_delay_seconds:
                self.skipped_domains[domain] = (
                    f"{CRAWL_DELAY_TOO_LONG} "
                    f"({policy.crawl_delay}s > {self.config.max_crawl_delay_seconds}s)"
                )
            else:
                self.limiter.set_domain_delay(domain, policy.crawl_delay)

    # -- fetching -----------------------------------------------------------
    async def get(self, url: str) -> CachedPage:
        """Fetch one URL, cache-first, honouring robots.txt and the rate limit."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return self._failed(url, f"not a usable http(s) URL: {url!r}")
        domain = domain_of_url(url)
        cached = self.cache.get(url)
        if cached is not None:
            return cached

        await self.ensure_robots(domain)

        if domain in self.skipped_domains:
            return self._failed(url, self.skipped_domains[domain])
        if not self.robots.allows(domain, url):
            log.info("robots.txt blocks %s", url)
            return self._failed(url, ROBOTS_BLOCKED)

        page = await self._request(url, domain)
        self._note_outcome(domain, page)
        # Only stable outcomes are cached. A 429 or a timeout is a moment in time,
        # not a fact about the page; caching it would make the failure permanent
        # across every future run.
        if page.ok or page.status == 404:
            self.cache.put(page)
        return page

    def _note_outcome(self, domain: str, page: CachedPage) -> None:
        """Abandon a domain that keeps failing.

        A site answering 429 to every request is asking us to stop. Continuing to
        walk its path list is impolite, pointless, and these are future partners.
        A 404 is a normal miss when probing candidate paths and does not count.
        """
        if page.ok or page.status == 404:
            self._consecutive_failures[domain] = 0
            return
        count = self._consecutive_failures.get(domain, 0) + 1
        self._consecutive_failures[domain] = count
        if count >= self.config.max_consecutive_failures and domain not in self.skipped_domains:
            reason = page.error or f"HTTP {page.status}"
            self.skipped_domains[domain] = (
                f"abandoned after {count} consecutive failures ({reason})"
            )
            log.info("abandoning %s: %s", domain, self.skipped_domains[domain])

    async def _request(self, url: str, domain: str, is_robots: bool = False) -> CachedPage:
        assert self._client is not None, "use `async with Fetcher(...)`"
        started = datetime.now()

        async def attempt() -> CachedPage:
            async with self.limiter.slot(domain):
                self.requests_made += 1
                response = await self._client.get(url)
            if response.status_code in self.config.retry_on_status:
                raise _Retryable(f"HTTP {response.status_code}")
            body = response.text
            if len(response.content) > self.config.max_response_bytes:
                body = response.text[: self.config.max_response_bytes]
            return CachedPage(
                url=url,
                final_url=str(response.url),
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                body=body,
                fetched_at=datetime.now(),
            )

        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.config.max_retries),
            wait=wait_exponential(multiplier=self.config.retry_backoff_seconds, max=30),
            retry=retry_if_exception_type(
                (_Retryable, httpx.TimeoutException, httpx.NetworkError)
            ),
            reraise=True,
        )
        try:
            async for state in retrying:
                with state:
                    return await attempt()
        except Exception as exc:  # noqa: BLE001 - reported, never raised onward
            label = "robots.txt" if is_robots else "page"
            log.info("fetch failed (%s) %s: %s", label, url, exc)
            return self._failed(url, f"{type(exc).__name__}: {exc}", started)
        return self._failed(url, "no attempt was made", started)  # pragma: no cover

    @staticmethod
    def _failed(url: str, error: str, when: datetime | None = None) -> CachedPage:
        return CachedPage(
            url=url,
            final_url=url,
            status=0,
            body="",
            fetched_at=when or datetime.now(),
            error=error,
        )

    @staticmethod
    def absolute(base: str, href: str) -> str:
        return urljoin(base, href)
