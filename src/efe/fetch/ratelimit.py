"""Politeness limits.

Several of these companies are future partners. Getting blocked is a commercial
problem, not a technical one -- so the default is deliberately slow: one request per
domain every two seconds, with a global cap on how many domains are in flight.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class RateLimiter:
    """Per-domain minimum spacing plus a global concurrency ceiling."""

    def __init__(self, per_domain_delay: float, global_concurrency: int) -> None:
        self.per_domain_delay = per_domain_delay
        self._semaphore = asyncio.Semaphore(max(1, global_concurrency))
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = {}
        self._overrides: dict[str, float] = {}
        self.total_waited = 0.0

    def set_domain_delay(self, domain: str, delay: float) -> None:
        """Honour a robots.txt `Crawl-delay` that is stricter than our default."""
        if delay > self.per_domain_delay:
            self._overrides[domain] = delay

    def delay_for(self, domain: str) -> float:
        return self._overrides.get(domain, self.per_domain_delay)

    class _Slot:
        def __init__(self, limiter: RateLimiter, domain: str) -> None:
            self._limiter = limiter
            self._domain = domain

        async def __aenter__(self) -> None:
            limiter, domain = self._limiter, self._domain
            await limiter._semaphore.acquire()
            self._domain_lock = limiter._locks[domain]
            await self._domain_lock.acquire()
            delay = limiter.delay_for(domain)
            last = limiter._last_request.get(domain)
            if last is not None:
                wait = delay - (time.monotonic() - last)
                if wait > 0:
                    limiter.total_waited += wait
                    await asyncio.sleep(wait)

        async def __aexit__(self, *exc_info: object) -> None:
            self._limiter._last_request[self._domain] = time.monotonic()
            self._domain_lock.release()
            self._limiter._semaphore.release()

    def slot(self, domain: str) -> _Slot:
        """`async with limiter.slot(domain):` around every outbound request."""
        return RateLimiter._Slot(self, domain)
