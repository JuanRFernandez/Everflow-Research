"""Commission and partner terms from published trade pages.

Only sentences the company actually wrote are captured, verbatim and trimmed to a
length that fits the cell. Nothing is summarised into a number that was not stated:
if a page says "competitive commission", that is what gets recorded -- it is never
turned into a percentage.

Only `/trade`, `/b2b`, `/partners`, `/agents`-style pages are consulted, because a
commission sentence on a blog post is not a partner programme.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from efe.config import TermsConfig
from efe.extract.base import RawFind, squeeze

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜÀ-Þ0-9])")
_PERCENT_RE = re.compile(r"\b\d{1,2}(?:[.,]\d{1,2})?\s?%")


def is_trade_page(url: str, config: TermsConfig) -> bool:
    """Whether a URL is the kind of page that can carry partner terms.

    Segments, not substrings. The token `pro` inside `provision-types` made
    `/travel-service/typically-tirolean/provision-types` look like a trade page, and
    "hiking provisions" was then captured as commission terms.
    """
    segments = [s for s in urlparse(url).path.lower().split("/") if s]
    tokens = [t.lower() for t in config.source_path_tokens]
    for segment in segments:
        stem = segment.rsplit(".", 1)[0]
        for token in tokens:
            if stem == token or stem.startswith((token + "-", token + "_")):
                return True
    return False


def extract_terms(html_body: str, url: str, config: TermsConfig) -> list[RawFind]:
    """Verbatim sentences stating commission, net rates, minimums or how to apply."""
    if not html_body or not is_trade_page(url, config):
        return []
    try:
        tree = HTMLParser(html_body)
    except Exception:  # pragma: no cover
        return []

    for tag in ("script", "style", "noscript", "nav", "footer"):
        for node in tree.css(tag):
            node.decompose()

    text = squeeze(tree.text(separator=" "))
    if not text:
        return []

    signals = [
        (raw, re.compile(r"\b" + raw, re.I)) for raw in config.signal_patterns
    ]
    finds: list[RawFind] = []
    seen: set[str] = set()

    for sentence in _SENTENCE_SPLIT_RE.split(text):
        clean = squeeze(sentence)
        if not 20 <= len(clean) <= 400:
            continue
        hits = [raw for raw, pattern in signals if pattern.search(clean)]
        if not hits:
            continue
        trimmed = clean[: config.max_chars].rstrip()
        if len(clean) > config.max_chars:
            trimmed = trimmed.rsplit(" ", 1)[0] + "..."
        if trimmed.lower() in seen:
            continue
        seen.add(trimmed.lower())

        finds.append(
            RawFind(
                value=trimmed,
                matched_text=clean,
                offset=html_body.find(clean[:60]),
                method="trade-page-sentence",
                extra={
                    "signals": ", ".join(hits[:4]),
                    "has_percentage": "true" if _PERCENT_RE.search(clean) else "false",
                },
            )
        )

    # A stated percentage is the most useful thing on the page; surface it first.
    finds.sort(key=lambda f: (f.extra.get("has_percentage") != "true", len(f.value)))
    return finds


def summarise(finds: list[RawFind], config: TermsConfig) -> str:
    """Join the strongest sentences into one cell value, still verbatim."""
    if not finds:
        return ""
    out: list[str] = []
    budget = config.max_chars
    for find in finds:
        if len(find.value) + 2 > budget:
            break
        out.append(find.value)
        budget -= len(find.value) + 2
    return " | ".join(out)
