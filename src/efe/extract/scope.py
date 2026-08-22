"""The scope guard: does this page speak for this entity?

Many PARTNERS rows point at a group or chain site rather than the property's own:
`W Verbier` -> `marriott.com`, `Airelles Val d'Isere` -> `airelles.com`,
`Fouquet's Courchevel` and `Hotel Barriere Les Neiges` -> the same
`hotelsbarriere.com`. A corporate address harvested from such a site is genuinely
published, and genuinely the wrong contact for the row.

So on a shared or chain domain, a value is only accepted when the page it came from
is demonstrably about that entity: its distinguishing name tokens, or its resort
town, appear in the URL path, the page title, or the surrounding text. Tokens already
present in the domain are ignored -- `airelles` on `airelles.com` proves nothing.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlparse

from efe.config import ScopeConfig
from efe.models import ScopeVerdict

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def fold(text: str) -> str:
    """Lowercase, accent-stripped form: `Val d'Isere` and `Val d'Isère` compare equal."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def tokenise(text: str, stopwords: set[str]) -> list[str]:
    """Meaningful lowercase tokens of a name, in order, stopwords and stubs removed."""
    return [
        token
        for token in _TOKEN_SPLIT_RE.split(fold(text))
        if len(token) > 1 and token not in stopwords
    ]


@dataclass(slots=True)
class ScopeDecision:
    verdict: ScopeVerdict
    reason: str
    matched_tokens: tuple[str, ...] = ()
    required_tokens: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.verdict is not ScopeVerdict.SHARED_UNMATCHED


class ScopeGuard:
    """Decides whether a page may supply values for a given entity."""

    def __init__(
        self,
        config: ScopeConfig,
        domain_row_counts: Counter[str] | None = None,
        domain_resorts: dict[str, list[str]] | None = None,
    ):
        self.config = config
        self.domain_row_counts = domain_row_counts or Counter()
        #: domain -> every row's Resort_Base on that domain, so the resort fallback
        #: can tell whether a resort actually distinguishes this entity from its
        #: siblings on the same site.
        self.domain_resorts = domain_resorts or {}
        self._stopwords = {fold(w) for w in config.name_stopwords}
        self._chains = {fold(d) for d in config.chain_domains}

    def resort_is_distinctive(self, domain: str, resort_base: str) -> bool:
        """Whether this resort separates the entity from its siblings on this domain.

        Hotel Barriere Les Neiges and Fouquet's Courchevel share hotelsbarriere.com
        and both sit in Courchevel, so `courchevel` on a page tells you nothing about
        which of the two it belongs to.
        """
        wanted = fold(resort_base).strip()
        if not wanted:
            return False
        siblings = self.domain_resorts.get(domain)
        if siblings is None:
            return True
        return sum(1 for r in siblings if fold(r).strip() == wanted) <= 1

    # -- domain classification ---------------------------------------------
    def is_chain(self, domain: str) -> bool:
        folded = fold(domain)
        return folded in self._chains or any(
            folded.endswith("." + chain) for chain in self._chains
        )

    def is_shared(self, domain: str) -> bool:
        """A domain used by several rows, or a known global chain."""
        if not domain:
            return False
        if self.is_chain(domain):
            return True
        return self.domain_row_counts.get(domain, 0) >= self.config.shared_domain_min_rows

    def shared_reason(self, domain: str) -> str:
        rows = self.domain_row_counts.get(domain, 0)
        if self.is_chain(domain):
            return f"{domain} is a known group/chain domain"
        return f"{domain} is shared by {rows} PARTNERS rows"

    # -- name matching ------------------------------------------------------
    def discriminating_tokens(self, entity_name: str, domain: str) -> list[str]:
        """Name tokens that could tell this entity apart from its siblings.

        Tokens already contained in the domain are dropped: on `airelles.com` the
        word `airelles` appears on every page and distinguishes nothing.
        """
        domain_flat = re.sub(r"[^a-z0-9]", "", fold(domain))
        return [
            token
            for token in tokenise(entity_name, self._stopwords)
            if token not in domain_flat
        ]

    def decide(
        self,
        *,
        entity_name: str,
        domain: str,
        resort_base: str,
        page_url: str,
        identity_text: str = "",
        is_homepage: bool = False,
    ) -> ScopeDecision:
        """Whether `page_url` may supply values for `entity_name`.

        `identity_text` must be the page's *identity*: its title, og:title and
        headings. Never the whole body -- a chain site's mega-menu names every
        property it owns, which would make the guard accept a group contact page for
        any of them and defeat the entire point.

        `is_homepage` exists because even headings are not enough there: a group
        homepage lists every destination it sells, so `airelles.com/` names
        Courchevel, Val d'Isere and Gordes alike. It therefore identifies no single
        property, and the only row it can speak for is the group itself.
        """
        if not self.is_shared(domain):
            return ScopeDecision(
                verdict=ScopeVerdict.OWN_DOMAIN,
                reason=f"{domain} is the entity's own domain",
            )

        required = self.discriminating_tokens(entity_name, domain)
        resort_tokens = tokenise(resort_base, self._stopwords)

        path = fold(urlparse(page_url).path)
        identity = fold(identity_text)

        def hit(token: str) -> bool:
            return token in path or token in identity

        matched = tuple(token for token in required if hit(token))
        resort_hit = tuple(token for token in resort_tokens if hit(token))

        shared = self.shared_reason(domain)

        if not required:
            # Nothing distinguishes this entity from the domain itself (e.g. the row
            # *is* the group). Accept: the group's own contact details are correct.
            return ScopeDecision(
                verdict=ScopeVerdict.SHARED_MATCHED,
                reason=(
                    f"{shared}, but the entity name adds no token beyond the domain, "
                    "so this row is the group itself"
                ),
            )

        if is_homepage:
            # The group homepage advertises every property, so matching a name token
            # there proves nothing about whose contact details are on the page.
            return ScopeDecision(
                verdict=ScopeVerdict.SHARED_UNMATCHED,
                reason=(
                    f"{shared} and this is the group homepage, which names every "
                    "property it sells; it cannot identify this one. Nothing is "
                    "written; the row needs a property-level Website_URL"
                ),
                required_tokens=tuple(required),
            )

        ratio = len(matched) / len(required)
        if ratio >= self.config.name_match_min_token_ratio:
            return ScopeDecision(
                verdict=ScopeVerdict.SHARED_MATCHED,
                reason=(
                    f"{shared}; the page URL/title/headings match "
                    f"{len(matched)}/{len(required)} distinguishing name tokens "
                    f"{list(matched)}"
                ),
                matched_tokens=matched,
                required_tokens=tuple(required),
            )
        if resort_hit and self.resort_is_distinctive(domain, resort_base):
            return ScopeDecision(
                verdict=ScopeVerdict.SHARED_MATCHED,
                reason=(
                    f"{shared}; the page URL/title/headings name the entity's "
                    f"resort base {list(resort_hit)}, and no sibling row on this "
                    "domain shares that resort"
                ),
                matched_tokens=resort_hit,
                required_tokens=tuple(required),
            )
        if resort_hit:
            return ScopeDecision(
                verdict=ScopeVerdict.SHARED_UNMATCHED,
                reason=(
                    f"{shared}; the page names the resort {resort_base!r} but another "
                    "row on this domain is in the same resort, so the resort does not "
                    f"identify this entity (needed any of {required}). Nothing is "
                    "written; the row needs a property-level Website_URL"
                ),
                required_tokens=tuple(required),
            )

        return ScopeDecision(
            verdict=ScopeVerdict.SHARED_UNMATCHED,
            reason=(
                f"{shared} and the page title/URL does not name this entity "
                f"(needed any of {required}"
                + (f" or resort {resort_tokens}" if resort_tokens else "")
                + "). A group-level contact is not this property's contact, so "
                "nothing is written; the row needs a property-level Website_URL"
            ),
            required_tokens=tuple(required),
        )
