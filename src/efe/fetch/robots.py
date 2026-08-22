"""robots.txt handling.

Fetched once per domain, cached for the run, and obeyed. A disallowed URL is skipped
and reported -- never fetched anyway. A `Crawl-delay` stricter than our own default
is adopted; an absurd one means we leave the domain alone entirely and say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.robotparser import RobotFileParser

_SITEMAP_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.I | re.M)
_CRAWL_DELAY_RE = re.compile(r"^\s*crawl-delay\s*:\s*([0-9.]+)", re.I | re.M)


@dataclass
class RobotsPolicy:
    """What one domain's robots.txt permits."""

    domain: str
    fetched: bool = False
    parser: RobotFileParser | None = None
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)
    error: str = ""

    def allows(self, url: str, user_agent: str) -> bool:
        """Absent or unreadable robots.txt means allowed, per the standard."""
        if self.parser is None:
            return True
        try:
            return self.parser.can_fetch(user_agent, url)
        except Exception:  # pragma: no cover - defensive; malformed rules
            return True


class RobotsCache:
    """One `RobotsPolicy` per domain, fetched lazily."""

    def __init__(self, user_agent: str, respect: bool = True) -> None:
        self.user_agent = user_agent
        self.respect = respect
        self._policies: dict[str, RobotsPolicy] = {}
        self.blocked_urls: list[str] = []

    def has(self, domain: str) -> bool:
        return domain in self._policies

    def get(self, domain: str) -> RobotsPolicy | None:
        return self._policies.get(domain)

    def record(self, domain: str, body: str | None, error: str = "") -> RobotsPolicy:
        """Parse and store a fetched robots.txt body (or the failure to fetch one)."""
        policy = RobotsPolicy(domain=domain, fetched=True, error=error)
        if body:
            parser = RobotFileParser()
            parser.parse(body.splitlines())
            policy.parser = parser
            policy.sitemaps = [m.strip() for m in _SITEMAP_RE.findall(body)]
            delays = [float(d) for d in _CRAWL_DELAY_RE.findall(body)]
            if delays:
                policy.crawl_delay = max(delays)
        self._policies[domain] = policy
        return policy

    def allows(self, domain: str, url: str) -> bool:
        if not self.respect:
            return True
        policy = self._policies.get(domain)
        if policy is None:
            return True
        allowed = policy.allows(url, self.user_agent)
        if not allowed:
            self.blocked_urls.append(url)
        return allowed
