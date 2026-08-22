"""Phone number extraction, normalised to E.164.

Normalisation uses `phonenumbers` (Google's libphonenumber port) rather than regex,
because deciding that `+41 27 966 03 03` and `027 966 03 03` are the same Swiss
number needs a real numbering-plan database. It is offline and deterministic.

WhatsApp is treated as a separate, stricter case: a number only reaches the WhatsApp
column if the site published it *as* a WhatsApp contact -- a `wa.me` link, an
`api.whatsapp.com` link, or a number explicitly labelled as such. A WhatsApp value is
never derived from the Phone value, because a landline is not a WhatsApp account.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse

import phonenumbers
from selectolax.parser import HTMLParser

from efe.config import PhoneConfig
from efe.extract.base import RawFind, context_around, dedupe, find_offset

_WA_LINK_RE = re.compile(
    r"https?://(?:api\.whatsapp\.com/send|wa\.me|web\.whatsapp\.com/send)[^\s\"'<>]*",
    re.I,
)
_WA_LABEL_RE = re.compile(r"whats\s?app", re.I)
_DIGITS_RE = re.compile(r"\d")


def region_for(country: str, domain: str, config: PhoneConfig) -> str | None:
    """Best ISO region hint for parsing: the Country column, then the TLD."""
    if country:
        mapped = config.country_to_region.get(country.strip())
        if mapped:
            return mapped
        for key, value in config.country_to_region.items():
            if value and key.strip().lower() == country.strip().lower():
                return value
    lowered = (domain or "").lower()
    for suffix, region in sorted(config.tld_to_region.items(), key=lambda kv: -len(kv[0])):
        if lowered.endswith(suffix):
            return region
    return None


def to_e164(raw: str, region: str | None, config: PhoneConfig) -> str | None:
    """E.164 form of `raw`, or None if it is not a valid dialable number."""
    digits = _DIGITS_RE.findall(raw)
    if not (config.min_digits <= len(digits) <= config.max_digits + 3):
        return None
    try:
        parsed = phonenumbers.parse(raw, None if raw.strip().startswith("+") else region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _tel_hrefs(tree: HTMLParser, body: str, region: str | None,
               config: PhoneConfig) -> list[RawFind]:
    finds: list[RawFind] = []
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href.lower().startswith("tel:"):
            continue
        raw = html.unescape(href[4:]).strip()
        normalised = to_e164(raw, region, config)
        if normalised:
            finds.append(
                RawFind(
                    value=normalised,
                    matched_text=href,
                    offset=find_offset(body, href),
                    method="tel-href",
                    context=node.text(strip=True)[:120],
                )
            )
    return finds


def extract_phones(
    html_body: str, region: str | None, config: PhoneConfig
) -> list[RawFind]:
    """Valid, E.164-normalised numbers on a page, `tel:` links first."""
    if not html_body:
        return []
    try:
        tree = HTMLParser(html_body)
        text = tree.text(separator=" ")
    except Exception:  # pragma: no cover
        tree, text = None, html.unescape(html_body)

    finds: list[RawFind] = []
    if tree is not None:
        finds += _tel_hrefs(tree, html_body, region, config)

    for match in phonenumbers.PhoneNumberMatcher(text, region or "ZZ"):
        if not phonenumbers.is_valid_number(match.number):
            continue
        finds.append(
            RawFind(
                value=phonenumbers.format_number(
                    match.number, phonenumbers.PhoneNumberFormat.E164
                ),
                matched_text=match.raw_string,
                offset=find_offset(html_body, match.raw_string),
                method="text",
                context=context_around(text, match.start),
            )
        )
    return dedupe(finds)


def extract_whatsapp(
    html_body: str, region: str | None, config: PhoneConfig
) -> list[RawFind]:
    """Numbers the site published specifically as WhatsApp contacts."""
    if not html_body:
        return []
    try:
        tree = HTMLParser(html_body)
        text = tree.text(separator=" ")
    except Exception:  # pragma: no cover
        tree, text = None, html.unescape(html_body)

    finds: list[RawFind] = []

    for match in _WA_LINK_RE.finditer(html.unescape(html_body)):
        url = match.group(0).rstrip('"\'')
        parsed = urlparse(url)
        raw = ""
        if "wa.me" in parsed.netloc.lower():
            raw = parsed.path.strip("/").split("/")[0]
        if not raw:
            raw = (parse_qs(parsed.query).get("phone") or [""])[0]
        if not raw:
            continue
        candidate = raw if raw.startswith("+") else "+" + raw.lstrip("+")
        normalised = to_e164(candidate, region, config)
        if normalised:
            finds.append(
                RawFind(
                    value=normalised,
                    matched_text=url,
                    offset=find_offset(html_body, url),
                    method="wa-link",
                )
            )

    # A number labelled WhatsApp *in the same block* counts. Scanning the flattened
    # page text would sweep in whatever number happened to precede the label -- on a
    # typical contact page, the office landline sitting one line above it.
    for block in _label_blocks(tree, text):
        if not _WA_LABEL_RE.search(block):
            continue
        for match in phonenumbers.PhoneNumberMatcher(block, region or "ZZ"):
            if not phonenumbers.is_valid_number(match.number):
                continue
            finds.append(
                RawFind(
                    value=phonenumbers.format_number(
                        match.number, phonenumbers.PhoneNumberFormat.E164
                    ),
                    matched_text=match.raw_string,
                    offset=find_offset(html_body, match.raw_string),
                    method="whatsapp-label",
                    context=block[:160],
                )
            )

    return dedupe(finds)


def _label_blocks(tree: HTMLParser | None, fallback: str) -> list[str]:
    """Short leaf-ish blocks of text, so a label and a number share a container."""
    if tree is None:
        return [fallback]
    blocks: list[str] = []
    seen: set[str] = set()
    for selector in ("p", "li", "td", "div", "span", "a", "h1", "h2", "h3", "figcaption"):
        for node in tree.css(selector):
            text = (node.text(separator=" ", strip=True) or "")[:300]
            if text and text not in seen:
                seen.add(text)
                blocks.append(text)
    return blocks
