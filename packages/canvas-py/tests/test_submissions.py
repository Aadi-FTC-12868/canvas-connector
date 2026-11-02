"""Unit tests for submissions endpoints.

Writes (submit_text, submit_url, submit_files, add_comment) MUST NOT be
tested against live Canvas — submitting a dummy consumes attempts. All
write tests here inspect the generated request shape.
"""
from __future__ import annotations

import pytest

from canvas_py.endpoints.submissions import (
    AddSubmissionComment,
    GetSubmission,
    ListSubmissions,
    MarkSubmissionRead,
    SubmitFileUpload,
    SubmitMediaRecording,
    SubmitTextEntry,
    SubmitUrl,
)


# --- reads --------------------------------------------------------------

def test_get_submission_default_includes():
    req = GetSubmission(course_id=2995, assignment_id=100)
    assert req.path == "courses/2995/assignments/100/submissions/self"
    assert req.method == "GET"
    pairs = req.query_pairs()
    included = [v for k, v in pairs if k == "include[]"]
    assert "submission_comments" in included
    assert "submission_history" in included
    assert "rubric_assessment" in included


def test_get_submission_with_explicit_user_id():
    req = GetSubmission(course_id=1, assignment_id=2, user_id=42)
    assert req.path == "courses/1/assignments/2/submissions/42"


def test_list_submissions_is_paginated():
    req = ListSubmissions(course_id=1, assignment_id=2)
    assert req.response_is_paginated is True
    assert req.path == "courses/1/assignments/2/submissions"


# --- writes: text entry -------------------------------------------------

def test_submit_text_entry_body_shape():
    req = SubmitTextEntry(1, 2, "Hello, world!")
    body = req.json_body()
    assert body["submission"]["submission_type"] == "online_text_entry"
    assert body["submission"]["body"] == "Hello, world!"
    assert "comment" not in body  # no comment passed


def test_submit_text_entry_with_comment():
    req = SubmitTextEntry(1, 2, "Essay text.", comment="see attached analysis")
    body = req.json_body()
    assert body["comment"]["text_comment"] == "see attached analysis"


def test_submit_text_entry_rejects_empty_body():
    with pytest.raises(ValueError, match="body cannot be empty"):
        SubmitTextEntry(1, 2, "")


# --- writes: URL entry --------------------------------------------------

def test_submit_url_rejects_non_http():
    with pytest.raises(ValueError, match="http"):
        SubmitUrl(1, 2, "ftp://example.com/file")


def test_submit_url_accepts_https():
    req = SubmitUrl(1, 2, "https://example.com/my-essay")
    body = req.json_body()
    assert body["submission"]["submission_type"] == "online_url"
    assert body["submission"]["url"] == "https://example.com/my-essay"


# --- writes: file upload ------------------------------------------------

def test_submit_file_upload_body_shape():
    req = SubmitFileUpload(1, 2, file_ids=[111, 222])
    body = req.json_body()
    assert body["submission"]["submission_type"] == "online_upload"
    assert body["submission"]["file_ids"] == [111, 222]


def test_submit_file_upload_rejects_empty_ids():
    with pytest.raises(ValueError, match="file_ids cannot be empty"):
        SubmitFileUpload(1, 2, file_ids=[])


# --- writes: media ------------------------------------------------------

def test_submit_media_rejects_bad_type():
    with pytest.raises(ValueError, match="audio.*video"):
        SubmitMediaRecording(1, 2, "media_123", "text")


def test_submit_media_body_shape():
    req = SubmitMediaRecording(1, 2, "media_123", "video")
    body = req.json_body()
    assert body["submission"]["submission_type"] == "media_recording"
    assert body["submission"]["media_comment_id"] == "media_123"
    assert body["submission"]["media_comment_type"] == "video"


# --- writes: comment ----------------------------------------------------

def test_add_submission_comment_is_put():
    req = AddSubmissionComment(1, 2, "Great work!")
    assert req.method == "PUT"
    assert req.path == "courses/1/assignments/2/submissions/self"


def test_add_submission_comment_body_shape():
    req = AddSubmissionComment(1, 2, "Feedback text", file_ids=[55])
    body = req.json_body()
    assert body["comment"]["text_comment"] == "Feedback text"
    assert body["comment"]["file_ids"] == [55]


def test_add_submission_comment_rejects_empty_text():
    with pytest.raises(ValueError, match="cannot be empty"):
        AddSubmissionComment(1, 2, "")


# --- mark read ----------------------------------------------------------

def test_mark_submission_read_path():
    req = MarkSubmissionRead(2995, 100)
    assert req.method == "PUT"
    assert req.path == "courses/2995/assignments/100/submissions/self/read"
    assert req.json_body() is None
    assert req.decode({}) is None
