"""Shared extractor plumbing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WS_RE = re.compile(r"\s+")

#: Page regions that belong to the site, not to the page. On a group site these
#: carry the group's central phone number, the group's address and every sibling
#: property's social handle -- none of which belong to the row being filled.
CHROME_SELECTORS = (
    "header", "nav", "footer",
    "[role=banner]", "[role=navigation]", "[role=contentinfo]",
    ".header", ".site-header", ".page-header", ".topbar", ".top-bar",
    ".nav", ".navbar", ".navigation", ".menu", ".mega-menu", ".megamenu",
    ".footer", ".site-footer", ".page-footer", ".bottom-bar",
    "#header", "#nav", "#navigation", "#menu", "#footer", "#site-footer",
    ".cookie", ".cookies", ".cookie-banner", ".consent", "#cookie-banner",
)


def strip_chrome(html_body: str) -> str:
    """Remove header, navigation, footer and cookie chrome from a page.

    Used only on shared/group domains, where those regions describe the group rather
    than the property whose row is being filled.
    """
    if not html_body:
        return html_body
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html_body)
    except Exception:  # pragma: no cover - selectolax is very forgiving
        return html_body
    removed = 0
    for selector in CHROME_SELECTORS:
        try:
            nodes = tree.css(selector)
        except Exception:  # pragma: no cover - a selector this build cannot parse
            continue
        for node in nodes:
            node.decompose()
            removed += 1
    if removed == 0:
        return html_body
    return tree.html or html_body


@dataclass(slots=True)
class RawFind:
    """One literal thing found on one page.

    `matched_text` is the substring exactly as it appeared. That is the audit trail:
    a human can open the cached page, search for this string, and see it.
    """

    value: str
    matched_text: str
    offset: int = -1
    method: str = ""
    context: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def find_offset(haystack: str, needle: str) -> int:
    """Byte offset of `needle` in the original page body, or -1 if transformed away."""
    if not needle:
        return -1
    index = haystack.find(needle)
    return index if index >= 0 else -1


def squeeze(text: str) -> str:
    """Collapse whitespace to single spaces."""
    return _WS_RE.sub(" ", text).strip()


def context_around(text: str, index: int, width: int = 160) -> str:
    """A readable window of text around an offset, for evidence and review."""
    if index < 0:
        return ""
    start = max(0, index - width // 2)
    return squeeze(text[start : index + width // 2])


def dedupe(finds: list[RawFind]) -> list[RawFind]:
    """First occurrence of each value wins; ordering is the caller's priority."""
    seen: set[str] = set()
    out: list[RawFind] = []
    for find in finds:
        key = find.value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(find)
    return out
