"""Tests for canvas_py.parsers — extraction of file links from Canvas HTML.

The real-world shape of a Canvas file-link anchor is documented in
docs/research/05-real-world-quirks.md §2. These tests lock that in.
"""
from __future__ import annotations

from canvas_py.parsers import FileLink, dedup_file_links, extract_file_links


HTML_SINGLE_LINK = '''
<p>Here is the worksheet:</p>
<a class="instructure_file_link"
   title="Trig01 SAS Area.pdf"
   href="https://imsa.instructure.com/courses/2995/files/1126216?verifier=abc"
   data-api-endpoint="https://imsa.instructure.com/api/v1/courses/2995/files/1126216"
   data-api-returntype="File">Trig01 SAS Area.pdf</a>
'''

HTML_MULTIPLE_LINKS = '''
<p>Read these:</p>
<a title="First.pdf"
   data-api-endpoint="https://x.instructure.com/api/v1/courses/1/files/100">First</a>
<a title="Second.pdf"
   data-api-endpoint="https://x.instructure.com/api/v1/courses/1/files/200">Second</a>
<a title="Duplicate.pdf"
   data-api-endpoint="https://x.instructure.com/api/v1/courses/1/files/100">Dup</a>
'''

HTML_NON_FILE_LINK = '''
<a href="https://imsa.instructure.com/courses/2995/pages/intro"
   data-api-endpoint="https://imsa.instructure.com/api/v1/courses/2995/pages/intro"
   data-api-returntype="Page">Intro page</a>
'''

HTML_WITHOUT_ENDPOINT = '''
<a href="https://imsa.instructure.com/files/1126216/download">Direct file link (no data-api-endpoint)</a>
'''

HTML_MALFORMED = '<a title="X" data-api-endpoint="/api/v1/files/555">unclosed'  # missing </a>


def test_extract_single_link():
    result = extract_file_links(HTML_SINGLE_LINK)
    assert len(result) == 1
    link = result[0]
    assert link.file_id == 1126216
    assert link.title == "Trig01 SAS Area.pdf"
    assert "api/v1/courses/2995/files/1126216" in link.api_endpoint
    assert link.href is not None
    assert "verifier=abc" in link.href


def test_extract_multiple_dedups_by_file_id():
    result = extract_file_links(HTML_MULTIPLE_LINKS)
    assert len(result) == 2
    ids = {link.file_id for link in result}
    assert ids == {100, 200}


def test_skip_non_file_data_api_endpoints():
    result = extract_file_links(HTML_NON_FILE_LINK)
    assert result == []


def test_skip_links_without_data_api_endpoint():
    result = extract_file_links(HTML_WITHOUT_ENDPOINT)
    assert result == []


def test_empty_html_returns_empty_list():
    assert extract_file_links("") == []
    assert extract_file_links(None) == []


def test_malformed_html_still_extracts():
    """BeautifulSoup is permissive and usually recovers from malformed HTML;
    either way, the parser should still find the file_id.
    """
    result = extract_file_links(HTML_MALFORMED)
    assert len(result) == 1
    assert result[0].file_id == 555


def test_dedup_file_links_preserves_order():
    links = [
        FileLink(file_id=1, title="a", api_endpoint=""),
        FileLink(file_id=2, title="b", api_endpoint=""),
        FileLink(file_id=1, title="a2", api_endpoint=""),
        FileLink(file_id=3, title="c", api_endpoint=""),
    ]
    result = dedup_file_links(links)
    assert [l.file_id for l in result] == [1, 2, 3]
