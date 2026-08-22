"""German and Austrian Impressum pages.

Section 5 TMG makes an Impressum legally mandatory for German commercial sites, and
Austria's ECG does the same. That makes it the single highest-yield page in this
dataset: it must carry a postal address, a phone number, an email address and the
name of the person authorised to represent the company.

It is handled as a first-class case rather than a fallback -- probed immediately
after the homepage for DE/AT entities -- and its labelled fields are parsed
explicitly rather than scraped generically, because a labelled value is much stronger
evidence than a loose regex hit.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from efe.extract.base import RawFind, squeeze
from efe.extract.persons import ROLE_RE

#: `Telefon: +49 ...`, `Tel. 08821 ...`, `Fon: ...`
_PHONE_LABEL_RE = re.compile(
    r"(?:Telefon|Tel\.?|Fon|Telefonnummer|Phone)\s*[:.]?\s*"
    r"([+()\d][\d\s()/\-.]{6,25}\d)",
    re.I,
)
#: `E-Mail: info@...`, `Mail: ...`
_EMAIL_LABEL_RE = re.compile(
    r"(?:E-?\s?Mail|Mail|Kontakt)\s*[:.]?\s*"
    r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24})",
    re.I,
)
#: The legally required representative.
_REPRESENTATIVE_RE = re.compile(
    r"(?:Vertreten\s+durch|Vertretungsberechtigte[rn]?(?:\s+Gesch[äa]ftsf[üu]hrer)?"
    r"|Gesch[äa]ftsf[üu]hrer(?:in)?|Inhaber(?:in)?|Vorstand|Verantwortlich(?:\s+f[üu]r"
    r"\s+den\s+Inhalt)?(?:\s+gem[äa][ßs]\s*§?\s*55\s*Abs\.?\s*2\s*RStV)?)"
    r"\s*[:.]?\s*"
    r"([A-ZÄÖÜ][A-Za-zÄÖÜäöüß.'\-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß.'\-]+){1,3})",
    re.I,
)
_REGISTER_RE = re.compile(
    r"(?:Handelsregister|HRB|HRA|Firmenbuchnummer|FN)\s*[:.]?\s*([A-Z0-9\s.\-]{3,20})",
    re.I,
)
_VAT_RE = re.compile(
    r"(?:USt-?IdNr\.?|Umsatzsteuer-?Identifikationsnummer|ATU|UID)\s*[:.]?\s*"
    r"([A-Z]{2}\s?[A-Z0-9]{6,14})",
    re.I,
)

_ROLE_FOR_LABEL = {
    "geschäftsführer": "Geschäftsführer",
    "geschaftsfuhrer": "Geschäftsführer",
    "geschäftsführerin": "Geschäftsführerin",
    "inhaber": "Inhaber",
    "inhaberin": "Inhaberin",
    "vorstand": "Vorstand",
    "vertreten durch": "Vertretungsberechtigter (Impressum)",
    "vertretungsberechtigter": "Vertretungsberechtigter (Impressum)",
    "vertretungsberechtigte": "Vertretungsberechtigte (Impressum)",
    "verantwortlich": "Verantwortlich für den Inhalt",
}


def looks_like_impressum(url: str, html_body: str) -> bool:
    """Whether this page really is an Impressum, not just named like one."""
    if "impressum" in url.lower() or "imprint" in url.lower():
        return True
    head = (html_body or "")[:4000].lower()
    return "impressum" in head and ("angaben gemäß" in head or "tmg" in head)


def _clean_name(raw: str) -> str:
    """Trim a captured representative name back to just the name.

    An Impressum usually states the same person twice -- `Vertreten durch: X` and
    `Geschaeftsfuehrer: X` -- and once the page is flattened to text the second
    label sits immediately after the first name. Without this the capture comes
    back as `Katharina Meier Geschaeftsfuehrer`.
    """
    name = squeeze(raw)
    role_match = ROLE_RE.search(name)
    if role_match:
        name = squeeze(name[: role_match.start()])
    name = squeeze(name.split(":")[0])
    return name


def _role_for(label: str) -> str:
    lowered = squeeze(label).lower().rstrip(":. ")
    for key, role in _ROLE_FOR_LABEL.items():
        if key in lowered:
            return role
    return "Vertretungsberechtigter (Impressum)"


def extract_impressum(html_body: str, url: str) -> dict[str, list[RawFind]]:
    """Labelled phone, email and representative fields from an Impressum page.

    Returns a mapping with keys `phone`, `email` and `person`. Values found here are
    the strongest evidence available on any page, because German law requires them to
    be accurate and kept current.
    """
    empty: dict[str, list[RawFind]] = {"phone": [], "email": [], "person": [], "registry": []}
    if not html_body:
        return empty
    try:
        tree = HTMLParser(html_body)
    except Exception:  # pragma: no cover
        return empty
    for tag in ("script", "style", "noscript"):
        for node in tree.css(tag):
            node.decompose()

    text = squeeze(tree.text(separator=" \n "))
    if not text:
        return empty

    out = dict(empty)

    for match in _PHONE_LABEL_RE.finditer(text):
        out["phone"].append(
            RawFind(
                value=squeeze(match.group(1)),
                matched_text=squeeze(match.group(0)),
                offset=html_body.find(match.group(1).strip()[:12]),
                method="impressum-label:Telefon",
                context=squeeze(text[max(0, match.start() - 60) : match.end() + 60]),
            )
        )

    for match in _EMAIL_LABEL_RE.finditer(text):
        out["email"].append(
            RawFind(
                value=match.group(1).lower(),
                matched_text=squeeze(match.group(0)),
                offset=html_body.find(match.group(1)),
                method="impressum-label:E-Mail",
                context=squeeze(text[max(0, match.start() - 60) : match.end() + 60]),
            )
        )

    generic = "Vertretungsberechtigter (Impressum)"
    people: dict[str, RawFind] = {}
    for match in _REPRESENTATIVE_RE.finditer(text):
        name = _clean_name(match.group(1))
        if len(name.split()) < 2 or len(name.split()) > 4:
            continue
        prefix = match.group(0)[: match.group(0).lower().find(match.group(1).lower())]
        role = _role_for(prefix or match.group(0))
        existing = people.get(name)
        # The same person is usually named under two labels. Keep the specific one
        # (`Geschaeftsfuehrer`) over the generic `Vertretungsberechtigter`.
        if existing is not None and (role == generic or existing.extra["role"] != generic):
            continue
        people[name] = RawFind(
            value=name,
            matched_text=squeeze(match.group(0)),
            offset=html_body.find(name),
            method="impressum-label:Vertretung",
            context=squeeze(text[max(0, match.start() - 40) : match.end() + 60]),
            extra={"role": role},
        )
    out["person"].extend(people.values())

    for pattern, label in ((_REGISTER_RE, "Handelsregister"), (_VAT_RE, "USt-IdNr")):
        for match in pattern.finditer(text):
            out["registry"].append(
                RawFind(
                    value=squeeze(match.group(1)),
                    matched_text=squeeze(match.group(0)),
                    offset=-1,
                    method=f"impressum-label:{label}",
                )
            )

    return out
