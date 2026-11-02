"""HTML content parsers for Canvas.

Canvas's rich-content editor emits predictable HTML with `data-api-endpoint`
attributes on file links. This module extracts those so agents can discover
files via assignment descriptions (see research/05-real-world-quirks.md §2).

Why this matters: the Files API often 403s for students at the enumeration
level, but file IDs mentioned in assignment descriptions are fully accessible
via `GET /api/v1/files/:id`. Parsing descriptions is the reliable discovery
path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from bs4 import BeautifulSoup

# Fallback regex for when HTML is malformed enough that BS4 chokes.
_API_ENDPOINT_FILES_RE = re.compile(
    r'data-api-endpoint=["\']([^"\']*?/api/v1/(?:courses/\d+/)?files/(\d+))["\']',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FileLink:
    """A file reference extracted from Canvas HTML.

    Attributes:
        file_id: Canvas file ID as integer.
        title: human-readable title (from the `title` attribute or link text).
        api_endpoint: full API URL from data-api-endpoint (absolute).
        href: the public-facing href attribute (may include verifier token).
    """
    file_id: int
    title: str
    api_endpoint: str
    href: str | None = None


def extract_file_links(html: str | None) -> list[FileLink]:
    """Extract every Canvas file link from an HTML description.

    Targets anchors with `data-api-endpoint` matching the Canvas files pattern.
    Falls back to regex on BS4 failure.

    Returns:
        Deduplicated list (by file_id), preserving first-seen order.
    """
    if not html:
        return []

    found: dict[int, FileLink] = {}

    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", attrs={"data-api-endpoint": True}):
            endpoint = a.get("data-api-endpoint") or ""
            match = re.search(r"/files/(\d+)", endpoint)
            if not match:
                continue
            file_id = int(match.group(1))
            if file_id in found:
                continue

            title = (
                a.get("title")
                or (a.string and a.string.strip())
                or a.get_text(strip=True)
                or f"file_{file_id}"
            )
            href = a.get("href")
            found[file_id] = FileLink(
                file_id=file_id,
                title=title,
                api_endpoint=endpoint,
                href=href,
            )
    except Exception:
        # Fall back to pure regex.
        for match in _API_ENDPOINT_FILES_RE.finditer(html):
            endpoint = match.group(1)
            file_id = int(match.group(2))
            if file_id in found:
                continue
            found[file_id] = FileLink(
                file_id=file_id,
                title=f"file_{file_id}",
                api_endpoint=endpoint,
            )

    return list(found.values())


def dedup_file_links(links: Iterable[FileLink]) -> list[FileLink]:
    """Deduplicate by file_id, preserving order."""
    seen: set[int] = set()
    out: list[FileLink] = []
    for link in links:
        if link.file_id in seen:
            continue
        seen.add(link.file_id)
        out.append(link)
    return out
