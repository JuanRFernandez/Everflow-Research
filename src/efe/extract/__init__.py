"""Deterministic extractors.

Every extractor returns `RawFind` objects carrying the literal substring that was
matched and where it sat in the fetched page. Nothing is inferred: there is no code
path anywhere in this package that constructs an address, a number or a name from a
pattern observed elsewhere.

This package must not import from `efe.workbook`.
"""

from efe.extract.base import RawFind, find_offset
from efe.extract.classify import EmailRouting, classify_email, is_personal_local_part
from efe.extract.emails import extract_emails
from efe.extract.impressum import extract_impressum
from efe.extract.persons import extract_persons
from efe.extract.phones import extract_phones, extract_whatsapp, to_e164
from efe.extract.scope import ScopeGuard
from efe.extract.social import extract_instagram, extract_linkedin
from efe.extract.terms import extract_terms

__all__ = [
    "EmailRouting",
    "RawFind",
    "ScopeGuard",
    "classify_email",
    "extract_emails",
    "extract_impressum",
    "extract_instagram",
    "extract_linkedin",
    "extract_persons",
    "extract_phones",
    "extract_terms",
    "extract_whatsapp",
    "find_offset",
    "is_personal_local_part",
    "to_e164",
]
