"""Tests for Canvas error response classification."""
from __future__ import annotations

import pytest

from canvas_py._errors import (
    CanvasAuthError,
    CanvasError,
    CanvasNotFoundError,
    CanvasPermissionError,
    CanvasRateLimitError,
    CanvasServerError,
    parse_canvas_error,
)


def test_401_returns_auth_error():
    err = parse_canvas_error(401, "https://x/api", '{"errors":[{"message":"Invalid access token."}]}', {})
    assert isinstance(err, CanvasAuthError)
    assert "Invalid access token" in str(err)


def test_403_returns_permission_error():
    err = parse_canvas_error(403, "https://x/api", '{"status":"unauthorized","errors":[{"message":"user not authorized to perform that action"}]}', {})
    assert isinstance(err, CanvasPermissionError)
    assert "not authorized" in str(err)


def test_404_returns_not_found_error():
    err = parse_canvas_error(404, "https://x/api", '{"errors":[{"message":"The specified resource does not exist."}]}', {})
    assert isinstance(err, CanvasNotFoundError)


def test_429_returns_rate_limit_error_with_retry_after():
    err = parse_canvas_error(429, "https://x/api", '{"errors":[{"message":"Rate Limit Exceeded"}]}', {"Retry-After": "12"})
    assert isinstance(err, CanvasRateLimitError)
    assert err.retry_after == 12.0


def test_403_with_rate_limit_message_is_rate_limit_error():
    # Canvas sometimes returns 403 on rate limit rather than 429.
    err = parse_canvas_error(403, "https://x/api", '{"errors":[{"message":"Rate Limit Exceeded"}]}', {})
    assert isinstance(err, CanvasRateLimitError)


def test_500_returns_server_error():
    err = parse_canvas_error(500, "https://x/api", '{"errors":{"message":"Internal Server Error"}}', {})
    assert isinstance(err, CanvasServerError)


def test_field_keyed_errors_parsed():
    body = '{"errors":{"name":[{"message":"is required"}],"points":[{"message":"must be positive"}]}}'
    err = parse_canvas_error(400, "https://x/api", body, {})
    msg = str(err)
    assert "name:" in msg and "points:" in msg


def test_non_json_body_falls_back_to_raw():
    err = parse_canvas_error(500, "https://x/api", "<html>Something broke</html>", {})
    assert isinstance(err, CanvasServerError)
    assert err.body is not None


def test_attributes_populated():
    err = parse_canvas_error(401, "https://x/api/courses", '{"message":"nope"}', {})
    assert err.status == 401
    assert err.url == "https://x/api/courses"
    assert err.body == '{"message":"nope"}'
    assert err.parsed == {"message": "nope"}


def test_canvas_error_is_exception():
    with pytest.raises(CanvasError):
        raise parse_canvas_error(400, "https://x", '{"message":"bad"}', {})
