"""On-disk page cache.

Every fetched page is written to `data/cache/`, keyed by the SHA-256 of its URL, so
re-runs cost nothing and you can audit exactly what the extractors saw. `index.jsonl`
maps URL -> hash in append-only form for that audit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class CachedPage(BaseModel):
    """One fetched page, exactly as it was received."""

    url: str
    final_url: str
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    fetched_at: datetime
    error: str = ""
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").lower()

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type or (
            not self.content_type and "<html" in self.body[:2000].lower()
        )


def url_key(url: str) -> str:
    """Stable cache key for a URL."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class PageCache:
    """Content-addressed page store. Absent entries simply miss; nothing raises."""

    def __init__(self, directory: Path, enabled: bool = True) -> None:
        self.directory = directory
        self.enabled = enabled
        self.hits = 0
        self.misses = 0

    def _path_for(self, url: str) -> Path:
        key = url_key(url)
        return self.directory / key[:2] / f"{key}.json"

    def get(self, url: str) -> CachedPage | None:
        if not self.enabled:
            return None
        path = self._path_for(url)
        if not path.is_file():
            self.misses += 1
            return None
        try:
            page = CachedPage.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.misses += 1
            return None
        self.hits += 1
        page.from_cache = True
        return page

    def put(self, page: CachedPage) -> None:
        if not self.enabled:
            return
        path = self._path_for(page.url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(page.model_dump_json(indent=None), encoding="utf-8")
        tmp.replace(path)

        index = self.directory / "index.jsonl"
        record = {
            "url": page.url,
            "final_url": page.final_url,
            "key": url_key(page.url),
            "status": page.status,
            "fetched_at": page.fetched_at.isoformat(timespec="seconds"),
            "bytes": len(page.body),
        }
        with index.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
