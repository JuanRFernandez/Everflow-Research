"""Routing an address to General_Email (I) or Sales_B2B_Email (J).

The decision is made from the local part first, because that is what the company
itself chose to publish, and only then from the page it was found on. Every decision
carries a `reason` string that is printed verbatim in `--dry-run` output, so the
judgement can be inspected rather than trusted.

Only an address whose local part matches a known role token is ever written to the
workbook. Anything else -- an unrecognised local part, or one shaped like a person's
name -- is routed to the review queue instead. That is deliberate: an address that
cannot be shown to be a corporate role address is not worth the risk of a bounce or a
GDPR complaint, and `config.yaml` is where you widen the vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from efe.config import EmailConfig, GdprConfig
from efe.models import DataClass, Field_, PageKind

_SEGMENT_SPLIT_RE = re.compile(r"[._\-+]+")


@dataclass(slots=True)
class EmailRouting:
    """Where one address should go, and why."""

    address: str
    field: Field_ | None
    data_class: DataClass
    reason: str
    matched_token: str = ""
    token_rank: int = 999
    writable: bool = False

    @property
    def column_label(self) -> str:
        if self.field is Field_.SALES_B2B_EMAIL:
            return "Sales_B2B_Email"
        if self.field is Field_.GENERAL_EMAIL:
            return "General_Email"
        return "(not written)"


def local_part_of(address: str) -> str:
    """Lowercased local part with any `+tag` suffix removed."""
    local = address.split("@", 1)[0].lower().strip()
    return local.split("+", 1)[0]


def _segments(local: str) -> set[str]:
    """Separator-delimited pieces of a local part, plus the whole thing.

    Segment matching, not substring matching: `partnersupport` must not match
    `partner`, but `sales.megeve` and `travel-trade` must match `sales` and `trade`.
    """
    return {piece for piece in _SEGMENT_SPLIT_RE.split(local) if piece} | {local}


def _first_match(local: str, tokens: list[str]) -> tuple[str, int] | None:
    """Highest-priority token from `tokens` present as a segment of `local`."""
    segments = _segments(local)
    for rank, token in enumerate(tokens):
        candidate = token.lower()
        if candidate in segments:
            return candidate, rank
        # Multi-word tokens like `travel-trade` are compared against the whole local part.
        if _SEGMENT_SPLIT_RE.search(candidate) and candidate == local:
            return candidate, rank
    return None


def is_personal_local_part(local: str, gdpr: GdprConfig) -> bool:
    """Whether a local part is shaped like a named individual (`j.mueller`, `anna_b`).

    Only consulted after role-token matching has failed, so `travel.trade` -- which
    matches one of these shapes -- is never misread as a person.
    """
    return any(re.match(pattern, local) for pattern in gdpr.personal_local_part_patterns)


def classify_email(
    address: str,
    page_kind: PageKind,
    email_config: EmailConfig,
    gdpr_config: GdprConfig,
) -> EmailRouting:
    """Decide the destination column for one address."""
    local = local_part_of(address)

    sales = _first_match(local, email_config.sales_local_parts)
    if sales is not None:
        token, rank = sales
        return EmailRouting(
            address=address,
            field=Field_.SALES_B2B_EMAIL,
            data_class=DataClass.CORPORATE_ROLE,
            reason=(
                f"local part {local!r} contains the trade/B2B token {token!r} "
                f"(sales priority {rank}) -> Sales_B2B_Email"
            ),
            matched_token=token,
            token_rank=rank,
            writable=True,
        )

    general = _first_match(local, email_config.general_local_parts)
    if general is not None:
        token, rank = general
        suffix = ""
        if page_kind is PageKind.TRADE:
            suffix = (
                "; found on a trade/B2B page, recorded as a trade contact in the "
                "review queue but kept in General_Email because the address itself "
                "is the company's general one"
            )
        return EmailRouting(
            address=address,
            field=Field_.GENERAL_EMAIL,
            data_class=DataClass.CORPORATE_ROLE,
            reason=(
                f"local part {local!r} contains the role token {token!r} "
                f"(general priority {rank}) -> General_Email{suffix}"
            ),
            matched_token=token,
            token_rank=rank,
            writable=True,
        )

    if is_personal_local_part(local, gdpr_config):
        return EmailRouting(
            address=address,
            field=None,
            data_class=DataClass.PERSONAL_NAMED,
            reason=(
                f"local part {local!r} is shaped like a named individual -> GDPR "
                "personal data, never written to PARTNERS; sent to the review queue"
            ),
            writable=False,
        )

    routed = (
        Field_.SALES_B2B_EMAIL if page_kind is PageKind.TRADE else Field_.GENERAL_EMAIL
    )
    return EmailRouting(
        address=address,
        field=routed,
        data_class=DataClass.UNKNOWN,
        reason=(
            f"local part {local!r} matches no known role token; page kind is "
            f"{page_kind.value!r} so it would belong in "
            f"{'Sales_B2B_Email' if routed is Field_.SALES_B2B_EMAIL else 'General_Email'}, "
            "but an unrecognised local part is never written -- held for review "
            "(add the token to config.yaml if it is a role address)"
        ),
        writable=False,
    )


def is_freemail(address: str, email_config: EmailConfig) -> bool:
    domain = address.split("@", 1)[-1].lower()
    return domain in {d.lower() for d in email_config.freemail_domains}


def domain_matches_site(address: str, site_domain: str) -> bool:
    """Whether the address belongs to the site that published it.

    A registrable-suffix comparison would need a public-suffix list; comparing the
    last two labels is enough here and errs toward accepting, which is safe because
    the value still has to clear the scope guard and the confidence threshold.
    """
    address_domain = address.split("@", 1)[-1].lower()
    if not site_domain:
        return False
    if address_domain == site_domain or address_domain.endswith("." + site_domain):
        return True
    return site_domain.endswith("." + address_domain)
