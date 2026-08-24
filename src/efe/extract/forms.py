"""Contact-form detection, for the FORM-ONLY sentinel.

Many Alpine properties publish no address at all -- only a contact form. That is a
real, verifiable fact about the site, and recording it beats leaving TBD: it tells
the operator "there is a channel, but it is a form", and it stops the row from being
re-crawled as if nothing had been found.

The sentinel is only ever written when NO email was found anywhere on the site, and
it lives in `empty_tokens`, so a real address found on a later run replaces it. It
is never, under any circumstance, turned into a guessed address.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from efe.extract.base import RawFind, find_offset

FORM_ONLY = "FORM-ONLY"

#: Input names that mark a form as a *contact* form rather than a login/search box.
_CONTACT_INPUT_TOKENS = (
    "email", "e-mail", "mail", "message", "nachricht", "anfrage", "request",
    "enquiry", "inquiry", "comment", "betreff", "subject", "telefon", "phone",
)
#: Forms that are definitely not contact forms, whatever fields they carry.
_NON_CONTACT_TOKENS = ("login", "signin", "sign-in", "search", "suche", "newsletter")


def _form_identity(form) -> str:
    bits = [form.attributes.get(a) or "" for a in ("id", "name", "class", "action")]
    return " ".join(bits).lower()


def detect_contact_form(html_body: str) -> RawFind | None:
    """The first genuine contact form on a page, or None.

    A form qualifies when it carries an email field or a free-text message area and
    is not a login, search or newsletter box. The returned `matched_text` is the
    form's opening tag as published -- the auditable evidence that the form exists.
    """
    if not html_body:
        return None
    try:
        tree = HTMLParser(html_body)
    except Exception:  # pragma: no cover - selectolax is very forgiving
        return None

    for form in tree.css("form"):
        identity = _form_identity(form)
        if any(tok in identity for tok in _NON_CONTACT_TOKENS):
            continue

        has_email = any(
            (node.attributes.get("type") or "").lower() == "email"
            or any(tok in (node.attributes.get("name") or "").lower()
                   for tok in ("email", "e-mail", "e_mail"))
            for node in form.css("input")
        )
        has_message = bool(form.css("textarea"))
        named_contact = any(
            tok in (node.attributes.get("name") or "").lower()
            for node in form.css("input, textarea, select")
            for tok in _CONTACT_INPUT_TOKENS
        )
        if not ((has_email and (has_message or named_contact)) or (has_email and has_message)
                or (has_message and named_contact)):
            continue

        raw = form.html or ""
        opening = raw.split(">", 1)[0] + ">" if ">" in raw else raw[:120]
        return RawFind(
            value=FORM_ONLY,
            matched_text=opening[:200],
            offset=find_offset(html_body, opening[:80]),
            method="contact-form",
            context=identity[:160],
        )
    return None
