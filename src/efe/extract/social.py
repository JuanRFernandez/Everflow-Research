"""LinkedIn company pages and Instagram handles.

Column O is `LinkedIn_URL`, which in this schema means the *company* page. A
`linkedin.com/in/<person>` URL is a named individual, so it never lands there; it is
recorded as a person reference in the review queue instead.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from efe.config import SocialConfig
from efe.extract.base import RawFind, dedupe, find_offset

_LINKEDIN_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company|showcase|school|in)/([^/?\"'\s<>#]+)",
    re.I,
)
_INSTAGRAM_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})",
    re.I,
)


def _hrefs(html_body: str) -> list[str]:
    try:
        tree = HTMLParser(html_body)
    except Exception:  # pragma: no cover
        return []
    return [
        (node.attributes.get("href") or "").strip()
        for node in tree.css("a[href]")
        if node.attributes.get("href")
    ]


def extract_linkedin(html_body: str, config: SocialConfig) -> list[RawFind]:
    """Company LinkedIn URLs. Personal `/in/` profiles are returned flagged, not dropped."""
    if not html_body:
        return []
    decoded = html.unescape(html_body)
    haystack = " ".join(_hrefs(html_body)) + " " + decoded

    allowed = {p.lower() for p in config.linkedin_company_paths}
    finds: list[RawFind] = []
    for match in _LINKEDIN_RE.finditer(haystack):
        kind, slug = match.group(1).lower(), match.group(2)
        url = f"https://www.linkedin.com/{kind}/{slug}"
        find = RawFind(
            value=url,
            matched_text=match.group(0),
            offset=find_offset(html_body, match.group(0)),
            method="linkedin",
        )
        if kind not in allowed:
            find.extra["personal_profile"] = "true"
            find.extra["rejected"] = (
                "linkedin.com/in/ is an individual's profile, not a company page"
            )
        finds.append(find)
    return dedupe(finds)


def extract_instagram(html_body: str, config: SocialConfig) -> list[RawFind]:
    """Instagram handles as `@handle`, excluding platform routes like /p/ and /reel/."""
    if not html_body:
        return []
    decoded = html.unescape(html_body)
    haystack = " ".join(_hrefs(html_body)) + " " + decoded

    rejected = {p.lower() for p in config.instagram_reject_paths}
    finds: list[RawFind] = []
    for match in _INSTAGRAM_RE.finditer(haystack):
        handle = match.group(1).strip(".")
        if not handle or handle.lower() in rejected:
            continue
        if handle.lower() in ("instagram", "explore", "accounts"):
            continue
        finds.append(
            RawFind(
                value="@" + handle,
                matched_text=match.group(0),
                offset=find_offset(html_body, match.group(0)),
                method="instagram",
            )
        )
    return dedupe(finds)


def is_own_profile(url_or_handle: str, entity_tokens: set[str]) -> bool:
    """Whether a handle plausibly belongs to the entity rather than a shared brand.

    Used only to rank candidates; the scope guard makes the binding decision.
    """
    text = urlparse(url_or_handle).path if "://" in url_or_handle else url_or_handle
    lowered = re.sub(r"[^a-z0-9]", "", text.lower())
    return any(token and token in lowered for token in entity_tokens)
