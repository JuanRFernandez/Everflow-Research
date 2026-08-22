"""Named contacts and their roles.

Person data is GDPR personal data and the hardest thing on a page to extract without
inventing something, so this module is deliberately strict. It recognises exactly
three shapes, all of which are a publisher deliberately presenting a person:

    P1  `Geschäftsführer: Katharina Meier`                 a labelled line
    P2  `<h3>Elena Vargas</h3><p>Director of Sales</p>`    adjacent elements
    P3  `Elena Vargas — Director of Sales`                 one line, explicit separator

Anything looser was tried and rejected. Matching "a role keyword somewhere near a
capitalised phrase" produced `Accept All / coo` from a cookie banner, `Villars Palace
/ socia` from the word "social", and `French Data Protection Act / cco` from
"account". A wrong named contact is worse than an empty cell, so the loose path is
gone.

The element or line holding the name must be *entirely* the name, and the one holding
the role must *start* with the role. Half a pair is always discarded.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from efe.extract.base import RawFind, dedupe, squeeze

#: Role vocabulary across the languages in this dataset. Longer forms first so
#: "Director of Sales" wins over "Director".
ROLE_PATTERNS: tuple[str, ...] = (
    # German -- the Impressum vocabulary, legally standardised
    r"Gesch(?:\u00e4|a|ae)ftsf(?:\u00fc|u|ue)hrerin",
    r"Gesch(?:\u00e4|a|ae)ftsf(?:\u00fc|u|ue)hrer", r"Vertretungsberechtigte[rn]?",
    r"Verkaufsleiterin", r"Verkaufsleiter", r"Hoteldirektorin", r"Hoteldirektor",
    r"Leiterin Vertrieb", r"Leiter Vertrieb", r"Vertriebsleiter(?:in)?",
    r"Inhaberin", r"Inhaber", r"Vorstand", r"Prokuristin", r"Prokurist",
    r"Direktorin", r"Direktor",
    # English
    r"Director of Sales(?: (?:and|&) Marketing)?", r"Director of Business Development",
    r"Head of (?:Sales|Partnerships|Business Development|Reservations|Marketing)",
    r"Chief Executive Officer", r"Chief Commercial Officer", r"Chief Operating Officer",
    r"Managing Director", r"General Manager", r"Hotel Manager", r"Resort Manager",
    r"Sales (?:Director|Manager|Executive)", r"Business Development Manager",
    r"Partnerships? Manager", r"Reservations? Manager", r"Revenue Manager",
    r"Guest Relations Manager", r"Travel Trade Manager", r"Account Manager",
    r"Marketing Manager", r"Operations Manager", r"Founder(?: (?:and|&) CEO)?",
    r"Co-?Founder", r"Owner", r"Proprietor", r"Head Concierge", r"Chef Concierge",
    # French
    r"Directrice (?:G[ée]n[ée]rale|Commerciale|des Ventes)",
    r"Directeur (?:G[ée]n[ée]ral|Commercial|des Ventes)",
    r"Responsable (?:Commercial[e]?|des Ventes|R[ée]servations|Partenariats)",
    r"G[ée]rante", r"G[ée]rant", r"Propri[ée]taire", r"Chef de R[ée]ception",
    # Italian
    r"Direttrice (?:Generale|Commerciale)", r"Direttore (?:Generale|Commerciale)",
    r"Responsabile (?:Commerciale|Vendite|Prenotazioni)", r"Titolare",
    # Spanish / Portuguese
    r"Directora? (?:General|Comercial|de Ventas)", r"Gerente(?: Comercial| de Ventas)?",
    r"Respons[áa]vel Comercial", r"Propriet[áa]ri[oa]",
)

#: Acronyms match case-sensitively and as whole words. `coo` inside "Cookies" and
#: `cco` inside "account" are exactly how the loose version produced nonsense.
ACRONYM_PATTERN = r"(?:CEO|COO|CCO|CFO|CMO|MD)"

_ROLE_ALTERNATION = "|".join(ROLE_PATTERNS)

#: Public: `impressum.py` uses this to trim a captured name back to just the name.
ROLE_RE = re.compile(r"\b(?:" + _ROLE_ALTERNATION + r")\b", re.I)

_ROLE_OR_ACRONYM_RE = re.compile(
    r"\b(?:" + _ROLE_ALTERNATION + r")\b|\b" + ACRONYM_PATTERN + r"\b"
)
#: The role must begin the line, optionally after a bullet or dash.
_ROLE_LEADING_RE = re.compile(
    r"^[\-–•*\s]*((?:" + _ROLE_ALTERNATION + r")|" + ACRONYM_PATTERN + r")\b",
    re.I,
)
#: `Geschäftsführer: Name`
_LABELLED_RE = re.compile(
    r"^[\-–•*\s]*((?:" + _ROLE_ALTERNATION + r")|" + ACRONYM_PATTERN + r")"
    r"\s*[:–-]\s*(.+)$",
    re.I,
)
#: `Name — Role`, `Name, Role`, `Name | Role`
_SEPARATED_RE = re.compile(
    r"^(?P<name>[^,|–\-]{4,60}?)\s*[,|–\-]\s*"
    r"(?P<role>(?:" + _ROLE_ALTERNATION + r")|" + ACRONYM_PATTERN + r")\b.*$",
    re.I,
)

#: One capitalised word, optionally hyphenated or apostrophed: `Jean-Luc`, `O'Brien`.
_WORD = (
    r"[A-ZÄÖÜÀ-ÞŠŽ]"
    r"[a-zßäöüà-ÿšž]{1,20}"
    r"(?:['’\-]"
    r"[A-Za-zÄÖÜÀ-Þßäöü"
    r"à-ÿŠŽšž]{1,20}){0,2}"
)
#: Two to four such words, a nobiliary particle allowed between them, and nothing else.
_NAME_RE = re.compile(
    r"^" + _WORD
    + r"(?:\s+(?:van|von|de|del|della|da|di|dos|das|du|le|la)\s+|\s+)"
    + _WORD
    + r"(?:\s+" + _WORD + r"){0,2}$"
)

#: Words that appear inside capitalised phrases which are not people. Cookie banners,
#: legal boilerplate and navigation are the three big sources of false positives.
_NOT_A_NAME_WORDS = {
    "accept", "reject", "cookie", "cookies", "consent", "settings", "manage",
    "privacy", "policy", "policies", "terms", "conditions", "protection", "data",
    "act", "law", "gdpr", "rgpd", "impressum", "kontakt", "contact", "contacts",
    "book", "booking", "reserve", "reservation", "discover", "découvrir", "decouvrir",
    "read", "more", "menu", "home", "team", "our", "all", "hotel", "hotels", "resort",
    "chalet", "palace", "spa", "collection", "group", "gmbh", "sarl", "ag",
    "ltd", "inc", "llc", "srl", "newsletter", "subscribe", "follow", "share",
    "download", "view", "learn", "click", "here", "next", "previous", "close",
    "search", "login", "register", "account", "français", "english", "deutsch",
    "italiano", "español", "rights", "reserved", "copyright", "sitemap", "legal",
    "handelsregister", "umsatzsteuer", "amtsgericht", "registergericht", "opening",
    "hours", "address", "phone", "email", "website", "press", "media", "news",
    "gallery", "offers", "rooms", "suites", "dining", "wellness", "experiences",
    "courchevel", "verbier", "megeve", "gstaad", "zermatt", "chamonix", "andermatt",
    # Loyalty programmes and awards read exactly like `Given Surname`.
    "club", "leaders", "members", "membership", "society", "association", "federation",
    "programme", "program", "loyalty", "award", "awards", "guide", "world", "luxury",
    "preferred", "virtuoso", "signature", "relais", "chateaux",
}

_MAX_LINE_CHARS = 120
_CONTAINER_SELECTORS = ("li", "article", "figcaption", "td", "div", "section")
_CHILD_SELECTORS = {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "span", "p", "div"}


def looks_like_name(candidate: str) -> bool:
    """Whether a string is, on its own, plausibly a person's name.

    Strict by design: the whole string must be the name -- two to four capitalised
    words, none of which is boilerplate or a place this dataset is full of.
    """
    text = squeeze(candidate)
    if not 5 <= len(text) <= 60:
        return False
    if not _NAME_RE.match(text):
        return False
    words = [w.strip(".,;:'’-").lower() for w in text.split()]
    if any(word in _NOT_A_NAME_WORDS for word in words):
        return False
    return not _ROLE_OR_ACRONYM_RE.search(text)


def _normalise_role(raw: str) -> str:
    return squeeze(raw).strip(":,-– ")


def _make(name: str, role: str, evidence: str, method: str) -> RawFind:
    return RawFind(
        value=name,
        matched_text=evidence[:200],
        offset=-1,
        method=method,
        context=evidence[:200],
        extra={"role": _normalise_role(role)},
    )


def _from_labelled_line(line: str) -> RawFind | None:
    """P1: `Geschäftsführer: Katharina Meier`."""
    text = squeeze(line)
    match = _LABELLED_RE.match(text)
    if not match:
        return None
    name = squeeze(match.group(2)).split(",")[0]
    if not looks_like_name(name):
        return None
    return _make(name, match.group(1), text, "labelled-line")


def _from_separated_line(line: str) -> RawFind | None:
    """P3: `Elena Vargas — Director of Sales`."""
    text = squeeze(line)
    if len(text) > _MAX_LINE_CHARS:
        return None
    match = _SEPARATED_RE.match(text)
    if not match:
        return None
    name = squeeze(match.group("name"))
    if not looks_like_name(name):
        return None
    return _make(name, match.group("role"), text, "separated-line")


def _pair_with_following(texts: list[str], index: int) -> tuple[str, str] | None:
    """Look one or two positions ahead for a line that starts with a role."""
    for offset in (1, 2):
        if index + offset >= len(texts):
            return None
        following = texts[index + offset]
        if not following or len(following) > _MAX_LINE_CHARS:
            continue
        role_match = _ROLE_LEADING_RE.match(following)
        if role_match:
            return role_match.group(1), following
    return None


def _from_adjacent_elements(tree: HTMLParser) -> list[RawFind]:
    """P2: a name element immediately followed by a role element.

    The team-page pattern: `<h3>Elena Vargas</h3><p>Director of Sales</p>`. Requiring
    the two to be siblings, in that order, is what makes it precise.
    """
    finds: list[RawFind] = []
    seen: set[tuple[str, str]] = set()
    for container in _CONTAINER_SELECTORS:
        for node in tree.css(container):
            texts = [
                squeeze(child.text(separator=" ", strip=True) or "")
                for child in node.iter(include_text=False)
                if child.tag in _CHILD_SELECTORS
            ]
            for index, name in enumerate(texts):
                if not looks_like_name(name):
                    continue
                pair = _pair_with_following(texts, index)
                if pair is None:
                    continue
                role, following = pair
                key = (name, role)
                if key in seen:
                    continue
                seen.add(key)
                finds.append(
                    _make(name, role, f"{name} / {following}", "adjacent-elements")
                )
    return finds


def _from_adjacent_lines(text: str) -> list[RawFind]:
    """P2 again, for markup separating name and role with `<br>` rather than tags."""
    lines = [squeeze(line) for line in text.splitlines()]
    finds: list[RawFind] = []
    for index, line in enumerate(lines):
        if not looks_like_name(line):
            continue
        pair = _pair_with_following(lines, index)
        if pair is None:
            continue
        role, following = pair
        finds.append(_make(line, role, f"{line} / {following}", "adjacent-lines"))
    return finds


def extract_persons(html_body: str) -> list[RawFind]:
    """Name/role pairs a page deliberately presents together.

    Each find carries the role in `extra["role"]`. A find without a role is never
    produced, and a name is never inferred from a role's surroundings.
    """
    if not html_body:
        return []
    try:
        tree = HTMLParser(html_body)
    except Exception:  # pragma: no cover - selectolax is very forgiving
        return []
    for tag in ("script", "style", "noscript"):
        for node in tree.css(tag):
            node.decompose()

    finds = _from_adjacent_elements(tree)

    plain = tree.text(separator="\n")
    finds += _from_adjacent_lines(plain)
    for line in plain.splitlines():
        labelled = _from_labelled_line(line)
        if labelled is not None:
            finds.append(labelled)
        separated = _from_separated_line(line)
        if separated is not None:
            finds.append(separated)

    return dedupe(finds)
