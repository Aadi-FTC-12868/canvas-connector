"""Tests for RFC-5988 Link header parsing."""
from __future__ import annotations

from canvas_py._pagination import parse_link_header


def test_empty_header_returns_empty():
    links = parse_link_header(None)
    assert links.next is None
    assert links.prev is None
    assert not links.has_next


def test_typical_canvas_header():
    header = (
        '<https://example.instructure.com/api/v1/courses?page=1>; rel="current",'
        '<https://example.instructure.com/api/v1/courses?page=2>; rel="next",'
        '<https://example.instructure.com/api/v1/courses?page=1>; rel="first",'
        '<https://example.instructure.com/api/v1/courses?page=5>; rel="last"'
    )
    links = parse_link_header(header)
    assert links.current == "https://example.instructure.com/api/v1/courses?page=1"
    assert links.next == "https://example.instructure.com/api/v1/courses?page=2"
    assert links.first == "https://example.instructure.com/api/v1/courses?page=1"
    assert links.last == "https://example.instructure.com/api/v1/courses?page=5"
    assert links.has_next


def test_last_page_has_no_next():
    header = (
        '<https://x.instructure.com/api/v1/courses?page=5>; rel="current",'
        '<https://x.instructure.com/api/v1/courses?page=1>; rel="first",'
        '<https://x.instructure.com/api/v1/courses?page=4>; rel="prev"'
    )
    links = parse_link_header(header)
    assert links.next is None
    assert not links.has_next


def test_unknown_rel_ignored():
    header = '<https://x.instructure.com/api/v1/courses?page=2>; rel="meta"'
    links = parse_link_header(header)
    assert links.next is None


def test_urls_with_query_params_preserved():
    url = "https://x.instructure.com/api/v1/courses?page=2&per_page=50&include%5B%5D=term"
    header = f'<{url}>; rel="next"'
    links = parse_link_header(header)
    assert links.next == url
