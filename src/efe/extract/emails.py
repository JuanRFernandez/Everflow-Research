"""Email address extraction.

Sources, in descending order of trust:

1. `mailto:` hrefs -- unambiguous, machine-readable, published by the site itself.
2. Plain text in the rendered body.
3. Obfuscated forms: `info [at] x [dot] com`, `info(at)x.com`, HTML entities,
   percent-encoding, right-to-left CSS reversal, and simple JavaScript concatenation.

Addresses rendered only as images are ignored. There is no OCR and no guessing: if
the characters are not in the page, the address does not exist as far as this tool is
concerned.
"""

from __future__ import annotations

import html
import re
from urllib.parse import unquote

from selectolax.parser import HTMLParser

from efe.config import EmailConfig
from efe.extract.base import RawFind, context_around, dedupe, find_offset

# Deliberately conservative: no trailing dots, no consecutive dots, sane TLD.
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"([A-Za-z0-9](?:[A-Za-z0-9._%+\-]{0,62}[A-Za-z0-9])?)"
    r"@"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})"
    r"(?![A-Za-z0-9\-])"
)

#: `info [at] example [dot] com`, `info (at) example . com`, `info AT example DOT com`
_AT_WORD = r"(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+|&#64;|&#x40;|\[@\])"
_DOT_WORD = r"(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+|\[\.\]|\(\.\))"
_OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9._%+\-]{0,62})"
    rf"\s*{_AT_WORD}\s*"
    r"((?:[A-Za-z0-9][A-Za-z0-9\-]{0,61}"
    rf"(?:\s*{_DOT_WORD}\s*|\.))+[A-Za-z]{{2,24}})",
    re.I,
)

#: `'in' + 'fo' + '@' + 'example.com'` and friends.
_JS_CONCAT_RE = re.compile(
    r"""['"]([^'"<>]{1,64})['"]\s*\+\s*['"]\s*@\s*['"]\s*\+\s*['"]([^'"<>]{1,64})['"]"""
)

#: A span whose CSS reverses its own text.
_RTL_STYLE_RE = re.compile(r"unicode-bidi\s*:\s*bidi-override|direction\s*:\s*rtl", re.I)

_DATA_ATTRS = ("data-email", "data-mail", "data-mailto", "data-contact", "data-address")

#: A retina asset reference -- `photo@2x.jpg`, `hero@3x.webp` -- is shaped exactly
#: like an address. These are file extensions, never top-level domains.
_ASSET_EXTENSIONS = frozenset(
    {
        "jpg", "jpeg", "png", "gif", "webp", "avif", "svg", "ico", "bmp", "tif",
        "tiff", "heic", "pdf", "css", "js", "mjs", "json", "xml", "woff", "woff2",
        "ttf", "otf", "eot", "mp4", "webm", "mov", "mp3", "wav", "zip", "gz",
        "map", "txt", "html", "htm", "php", "aspx",
    }
)
#: `@2x`, `@3x`, `@1.5x` -- a density descriptor, not a domain.
_DENSITY_RE = re.compile(r"^\d+(?:\.\d+)?x$", re.I)


def _clean_domain(raw: str) -> str:
    """Turn `example [dot] com` into `example.com`."""
    text = re.sub(_DOT_WORD, ".", raw, flags=re.I)
    text = re.sub(r"\s*\.\s*", ".", text)
    return text.strip(" .")


def _valid(address: str) -> bool:
    if address.count("@") != 1:
        return False
    local, _, domain = address.partition("@")
    if not local or not domain or ".." in address:
        return False
    labels = domain.lower().split(".")
    # `photo@2x.jpg` from a srcset: the "TLD" is a file extension and the "domain"
    # is a pixel-density descriptor.
    if labels[-1] in _ASSET_EXTENSIONS:
        return False
    if _DENSITY_RE.match(labels[0]):
        return False
    # A percent-escape in the local part means this came out of a URL, not a mailto.
    if "%" in local:
        return False
    if local.startswith(".") or local.endswith("."):
        return False
    if domain.startswith(("-", ".")) or domain.endswith(("-", ".")):
        return False
    tld = domain.rsplit(".", 1)[-1]
    return len(domain.split(".")) >= 2 and tld.isalpha() and 2 <= len(tld) <= 24


def acceptable(address: str, config: EmailConfig) -> tuple[bool, str]:
    """Whether an address may be used at all, and why not when it may not."""
    local, _, domain = address.lower().partition("@")
    base_local = local.split("+")[0]
    if base_local in {p.lower() for p in config.reject_local_parts}:
        return False, f"rejected local part {base_local!r} (noise or unusable)"
    if domain in {d.lower() for d in config.reject_domains}:
        return False, f"rejected domain {domain!r} (placeholder or third-party vendor)"
    if any(domain.endswith("." + d) for d in ("wixpress.com", "sentry.io")):
        return False, f"rejected vendor subdomain {domain!r}"
    if len(local) > 64 or len(address) > 254:
        return False, "address exceeds RFC length limits"
    return True, ""


def _from_mailto(tree: HTMLParser, body: str) -> list[RawFind]:
    finds: list[RawFind] = []
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href.lower().startswith("mailto:"):
            continue
        raw = href[7:].split("?")[0]
        decoded = html.unescape(unquote(raw)).strip()
        for part in re.split(r"[;,]", decoded):
            address = part.strip().strip("<>")
            if not address or not _valid(address):
                continue
            finds.append(
                RawFind(
                    value=address.lower(),
                    matched_text=href,
                    offset=find_offset(body, href),
                    method="mailto",
                    context=node.text(strip=True)[:160],
                )
            )
    return finds


def _from_data_attributes(tree: HTMLParser, body: str) -> list[RawFind]:
    finds: list[RawFind] = []
    for node in tree.css("*"):
        for attribute in _DATA_ATTRS:
            raw = node.attributes.get(attribute)
            if not raw:
                continue
            candidate = html.unescape(unquote(raw)).strip()
            if _valid(candidate):
                finds.append(
                    RawFind(
                        value=candidate.lower(),
                        matched_text=f'{attribute}="{raw}"',
                        offset=find_offset(body, raw),
                        method="data-attribute",
                    )
                )
    return finds


def _from_reversed_spans(tree: HTMLParser, body: str) -> list[RawFind]:
    finds: list[RawFind] = []
    for node in tree.css("[style]"):
        style = node.attributes.get("style") or ""
        if not _RTL_STYLE_RE.search(style):
            continue
        text = (node.text(strip=True) or "")[::-1]
        for match in _EMAIL_RE.finditer(text):
            address = match.group(0)
            if _valid(address):
                finds.append(
                    RawFind(
                        value=address.lower(),
                        matched_text=node.text(strip=True)[:120],
                        offset=find_offset(body, node.text(strip=True)),
                        method="css-reversed",
                    )
                )
    return finds


def _from_text(text: str, body: str) -> list[RawFind]:
    finds: list[RawFind] = []
    for match in _EMAIL_RE.finditer(text):
        address = match.group(0)
        if not _valid(address):
            continue
        finds.append(
            RawFind(
                value=address.lower(),
                matched_text=address,
                offset=find_offset(body, address),
                method="plain-text",
                context=context_around(text, match.start()),
            )
        )
    return finds


def _from_obfuscation(text: str, body: str) -> list[RawFind]:
    finds: list[RawFind] = []
    for match in _OBFUSCATED_RE.finditer(text):
        local, domain = match.group(1).strip(), _clean_domain(match.group(2))
        address = f"{local}@{domain}".lower()
        if not _valid(address):
            continue
        finds.append(
            RawFind(
                value=address,
                matched_text=match.group(0).strip(),
                offset=find_offset(body, match.group(0).strip()),
                method="obfuscated",
                context=context_around(text, match.start()),
            )
        )
    return finds


def _from_js_concat(body: str) -> list[RawFind]:
    finds: list[RawFind] = []
    for match in _JS_CONCAT_RE.finditer(body):
        local = re.sub(r"['\"\s+]", "", match.group(1))
        domain = re.sub(r"['\"\s+]", "", match.group(2))
        address = f"{local}@{domain}".lower()
        if _valid(address):
            finds.append(
                RawFind(
                    value=address,
                    matched_text=match.group(0),
                    offset=match.start(),
                    method="js-concat",
                )
            )
    return finds


def extract_emails(html_body: str, config: EmailConfig) -> list[RawFind]:
    """All acceptable addresses on a page, best-provenance first.

    Ordering is by extraction method, not by address: `mailto:` links first, then
    plain text, then the obfuscated forms. The classifier decides which column an
    address belongs in; this function only decides whether it is real.
    """
    if not html_body:
        return []

    try:
        tree = HTMLParser(html_body)
    except Exception:  # pragma: no cover - selectolax is very forgiving
        tree = None

    entity_decoded = html.unescape(html_body)
    text = tree.text(separator=" ") if tree is not None else entity_decoded

    finds: list[RawFind] = []
    if tree is not None:
        finds += _from_mailto(tree, html_body)
        finds += _from_data_attributes(tree, html_body)
        finds += _from_reversed_spans(tree, html_body)
    finds += _from_text(text, html_body)
    finds += _from_text(entity_decoded, html_body)
    finds += _from_obfuscation(text, html_body)
    finds += _from_obfuscation(entity_decoded, html_body)
    finds += _from_js_concat(html_body)

    accepted: list[RawFind] = []
    for find in dedupe(finds):
        ok, reason = acceptable(find.value, config)
        if ok:
            accepted.append(find)
        else:
            find.extra["rejected"] = reason
    return accepted
