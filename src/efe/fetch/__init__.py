"""HTTP fetching: cache, robots, rate limiting, client, page discovery.

This package knows nothing about Excel. A Phase-2 `Source` discovery plugin reuses
it unchanged; that is the point of the boundary.
"""

from efe.fetch.cache import CachedPage, PageCache
from efe.fetch.client import Fetcher
from efe.fetch.discovery import classify_page, plan_urls
from efe.fetch.ratelimit import RateLimiter
from efe.fetch.robots import RobotsCache

__all__ = [
    "CachedPage",
    "Fetcher",
    "PageCache",
    "RateLimiter",
    "RobotsCache",
    "classify_page",
    "plan_urls",
]
