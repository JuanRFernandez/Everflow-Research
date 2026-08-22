"""Deciding which pages to fetch for an entity.

Order of preference, cheapest and most reliable first:

1. The homepage.
2. For DE/AT sites, the Impressum -- legally mandatory, and by far the highest-yield
   page in this dataset. It is probed as a first-class case, not a fallback.
3. `sitemap.xml` (and any sitemap named in robots.txt), filtered to contact-ish URLs.
4. Links on the homepage whose href or text looks like a contact route.
5. Only then, blind probing of the configured path candidates.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

from selectolax.parser import HTMLParser

from efe.config import DiscoveryConfig
from efe.models import PageKind

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")

_PAGE_KIND_TOKENS: tuple[tuple[PageKind, tuple[str, ...]], ...] = (
    (PageKind.IMPRESSUM, ("impressum", "imprint", "rechtliches")),
    (
        PageKind.TRADE,
        ("trade", "b2b", "travel-agent", "travelagent", "travel-trade", "agents",
         "agences", "agenzie", "agencias", "partner", "partners", "espace-pro", "/pro"),
    ),
    (
        PageKind.CONTACT,
        ("contact", "kontakt", "contatti", "contacto", "contato", "nous-contacter",
         "contattaci", "reservation", "reservierung"),
    ),
    (PageKind.TEAM, ("team", "our-team", "people", "staff", "mitarbeiter", "equipe")),
    (
        PageKind.LEGAL,
        ("legal", "mentions-legales", "legal-notice", "note-legali", "aviso-legal",
         "termini", "privacy"),
    ),
    (PageKind.ABOUT, ("about", "ueber-uns", "uber-uns", "qui-sommes-nous", "chi-siamo",
                      "sobre", "quienes-somos", "story", "history")),
)


def classify_page(url: str) -> PageKind:
    """What kind of page this URL is, from its path alone.

    Path-only on purpose: a page's *address* is evidence a human can re-check, and it
    cannot be spoofed by body text. The kind feeds the confidence decision.
    """
    parsed = urlparse(url)
    path = (parsed.path or "/").lower().rstrip("/")
    if path in ("", "/index.html", "/index.php", "/home"):
        return PageKind.HOME
    if path.endswith(("sitemap.xml", "sitemap_index.xml")):
        return PageKind.SITEMAP
    for kind, tokens in _PAGE_KIND_TOKENS:
        if any(token in path for token in tokens):
            return kind
    return PageKind.OTHER


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """Return (page URLs, child sitemap URLs) from a sitemap or sitemap index."""
    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError:
        return [], []

    tag = root.tag.split("}")[-1]
    if tag == "sitemapindex":
        children = [
            loc.text.strip()
            for loc in root.iter(f"{_SITEMAP_NS}loc")
            if loc.text and loc.text.strip()
        ]
        return [], children
    urls = [
        loc.text.strip()
        for loc in root.iter(f"{_SITEMAP_NS}loc")
        if loc.text and loc.text.strip()
    ]
    return urls, []


def normalise_sitemap_url(raw: str, root: str) -> str:
    """An absolute http(s) URL, or "" if the entry cannot be made into one.

    Sitemaps in the wild carry relative paths (`/contact`) and scheme-less hosts
    (`www.example.com/x`). Passing those through produced requests to
    `https:///robots.txt` that failed and still consumed the per-entity page budget.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # `mailto:`, `javascript:`, `tel:` and friends are not pages. Catch them before
    # the scheme-less branch, which would otherwise glue `https://` onto the front.
    scheme_prefix = _SCHEME_RE.match(text)
    if scheme_prefix and scheme_prefix.group(1).lower() not in ("http", "https"):
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif text.startswith("/"):
        text = urljoin(root + "/", text.lstrip("/"))
    elif "://" not in text:
        # `www.example.com/page` -- a host with no scheme.
        text = "https://" + text if "." in text.split("/")[0] else urljoin(root + "/", text)
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return text


def filter_sitemap_urls(
    urls: list[str], config: DiscoveryConfig, limit: int, root: str = ""
) -> list[str]:
    """Keep only sitemap entries that look like contact routes, best kinds first."""
    if root:
        urls = [u for u in (normalise_sitemap_url(u, root) for u in urls) if u]
    else:
        urls = [u for u in urls if urlparse(u).scheme in ("http", "https")
                and urlparse(u).netloc]
    scored: list[tuple[int, str]] = []
    priority = {
        PageKind.IMPRESSUM: 0,
        PageKind.CONTACT: 1,
        PageKind.TRADE: 2,
        PageKind.LEGAL: 3,
        PageKind.TEAM: 4,
        PageKind.ABOUT: 5,
    }
    for url in urls[: config.max_sitemap_urls]:
        path = urlparse(url).path.lower()
        if not any(token in path for token in config.contact_url_tokens):
            continue
        kind = classify_page(url)
        rank = priority.get(kind, 9)
        # Shorter paths are more likely to be the real landing page, not an article.
        scored.append((rank * 1000 + len(path), url))
    scored.sort()
    seen: set[str] = set()
    out: list[str] = []
    for _, url in scored:
        if url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= limit:
            break
    return out


def contact_links(html: str, base_url: str, config: DiscoveryConfig,
                  limit: int = 20) -> list[str]:
    """In-page links whose href or anchor text looks like a contact route."""
    try:
        tree = HTMLParser(html)
    except Exception:  # pragma: no cover - selectolax is forgiving
        return []

    base_host = urlparse(base_url).netloc.lower()
    tokens = tuple(config.contact_url_tokens)
    texts = tuple(t.lower() for t in config.contact_link_text)

    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        # Stay on the site: a group site's own domain is handled by the scope guard.
        if parsed.netloc.lower().removeprefix("www.") != base_host.removeprefix("www."):
            continue
        clean = absolute.split("#")[0]
        if clean in seen:
            continue

        path = parsed.path.lower()
        label = (node.text(strip=True) or "").lower()
        href_hit = any(token in path for token in tokens)
        text_hit = any(t in label for t in texts if t)
        if not (href_hit or text_hit):
            continue

        seen.add(clean)
        kind = classify_page(clean)
        rank = {
            PageKind.IMPRESSUM: 0,
            PageKind.CONTACT: 1,
            PageKind.TRADE: 2,
            PageKind.LEGAL: 3,
            PageKind.TEAM: 4,
            PageKind.ABOUT: 5,
        }.get(kind, 9)
        found.append((rank * 1000 + len(path), clean))

    found.sort()
    return [url for _, url in found[:limit]]


def wants_impressum_first(domain: str, country: str, config: DiscoveryConfig) -> bool:
    """German and Austrian sites: Impressum before anything else."""
    if country.strip().upper() in {c.upper() for c in config.impressum_first_countries}:
        return True
    return any(domain.endswith(tld) for tld in config.impressum_first_tlds)


def candidate_paths(config: DiscoveryConfig) -> list[str]:
    """The blind-probe list, expanded across language prefixes, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for path in config.path_candidates:
        for prefix in config.language_prefixes:
            candidate = f"{prefix}{path}"
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def plan_urls(
    base_url: str,
    domain: str,
    country: str,
    config: DiscoveryConfig,
    *,
    sitemap_urls: list[str] | None = None,
    page_links: list[str] | None = None,
    limit: int = 8,
) -> list[str]:
    """Ordered list of URLs to fetch for one entity, homepage excluded.

    De-duplicated across all four sources, capped at `limit`.
    """
    root = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    ordered: list[str] = []
    seen: set[str] = {base_url.rstrip("/"), root}

    def add(url: str) -> None:
        clean = url.split("#")[0].rstrip("/") or url
        parsed_candidate = urlparse(clean)
        if parsed_candidate.scheme not in ("http", "https") or not parsed_candidate.netloc:
            return
        if clean not in seen:
            seen.add(clean)
            ordered.append(clean)

    if wants_impressum_first(domain, country, config):
        for path in config.impressum_paths:
            add(urljoin(root + "/", path.lstrip("/")))

    for url in filter_sitemap_urls(sitemap_urls or [], config, limit * 2, root=root):
        add(url)

    for url in page_links or []:
        add(url)

    for path in candidate_paths(config):
        add(urljoin(root + "/", path.lstrip("/")))

    return ordered[:limit]


def strip_scripts(html: str) -> str:
    """HTML with script/style/noscript removed, for text-level extraction."""
    return re.sub(
        r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I
    )
