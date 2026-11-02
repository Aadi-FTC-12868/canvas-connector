"""Canvas pagination via RFC-5988 Link headers.

Canvas paginates by returning a `Link` header with rel=next, rel=prev, rel=first,
rel=last URLs. The canvas-ios `GetNextRequest<T>` pattern threads the original
request's decoder through the next-page URL so the response keeps its types.
We do the same.
"""
from __future__ import annotations

import re
from typing import NamedTuple


class PaginationLinks(NamedTuple):
    """Parsed Link-header URLs for a paginated response."""
    current: str | None = None
    next: str | None = None
    prev: str | None = None
    first: str | None = None
    last: str | None = None

    @property
    def has_next(self) -> bool:
        return self.next is not None


_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


def parse_link_header(header_value: str | None) -> PaginationLinks:
    """Parse an RFC-5988 Link header into a PaginationLinks tuple.

    Canvas's Link header looks like:
        <https://.../api/v1/courses?page=2>; rel="next",
        <https://.../api/v1/courses?page=1>; rel="current",
        <https://.../api/v1/courses?page=1>; rel="first",
        <https://.../api/v1/courses?page=5>; rel="last"

    Unknown rels are ignored. Missing header returns empty tuple.
    """
    if not header_value:
        return PaginationLinks()

    urls: dict[str, str] = {}
    for match in _LINK_RE.finditer(header_value):
        url, rel = match.group(1), match.group(2)
        urls[rel] = url

    return PaginationLinks(
        current=urls.get("current"),
        next=urls.get("next"),
        prev=urls.get("prev"),
        first=urls.get("first"),
        last=urls.get("last"),
    )
