"""Request protocol — the core abstraction for typed Canvas endpoints.

Distilled from canvas-ios's APIRequestable (see research/01-api-layer.md).
Every Canvas endpoint is represented by a subclass of CanvasRequest that
declares:

    - path:    URL path relative to /api/v1 (may include placeholders)
    - method:  HTTP method ("GET", "POST", "PUT", "DELETE", "PATCH")
    - query:   query parameters as list of (key, value) pairs to preserve
               Canvas's repeated-key syntax (include[]=x&include[]=y)
    - body:    request body (JSON-serializable dict, or None)
    - decode:  converts response JSON into a typed Python object

Pagination helpers thread the owning request's decoder through next-page
URL fetches so paginated results keep their types.
"""
from __future__ import annotations

from typing import Any, ClassVar, Generic, TypeVar

Response = TypeVar("Response")

QueryValue = str | int | float | bool | None
QueryPair = tuple[str, QueryValue]


class CanvasRequest(Generic[Response]):
    """Base class for every API endpoint definition.

    Subclasses set `path` and `method`, and optionally override `query_pairs()`,
    `json_body()`, `form_body()`, and `decode()`.

    Design notes:
        - Query parameters use `list[QueryPair]` rather than a dict so that
          Canvas's repeated-key idiom (include[]=a&include[]=b) works.
        - `decode` returns a typed Python object (Pydantic model, list, etc.).
        - `response_is_paginated` hints the client whether to offer paginate()
          over send().
    """

    method: ClassVar[str] = "GET"
    response_is_paginated: ClassVar[bool] = False
    # Existing endpoints retain their retry/redirect behavior. Non-idempotent
    # multipart mutations explicitly disable both to avoid replaying bytes.
    allow_retries: ClassVar[bool] = True
    allow_redirects: ClassVar[bool] = True
    # Opt-in for collection reads where another same-origin route could have
    # side effects. Pagination may change query parameters, never this path.
    pin_pagination_path: ClassVar[bool] = False

    @property
    def path(self) -> str:
        """URL path relative to the API base (no leading /api/v1)."""
        raise NotImplementedError

    def query_pairs(self) -> list[QueryPair]:
        """Query parameters as list of (key, value) pairs.

        List form supports Canvas's `include[]=a&include[]=b` idiom.
        """
        return []

    def json_body(self) -> dict[str, Any] | None:
        """JSON body for POST/PUT/PATCH. Return None for GET/DELETE."""
        return None

    def form_body(self) -> dict[str, Any] | None:
        """Form-encoded body. Use instead of json_body when the endpoint
        expects application/x-www-form-urlencoded. Mutually exclusive with
        json_body.
        """
        return None

    def multipart_files(self) -> dict[str, tuple[str, bytes, str]] | None:
        """Immutable file parts: field -> (filename, bytes, content type).

        May accompany form_body(), but never json_body(). Streams and local
        paths are deliberately unsupported: callers must snapshot bytes first.
        """
        return None

    def headers(self) -> dict[str, str]:
        """Per-request extra headers. Authorization is added by the client."""
        return {}

    def decode(self, data: Any) -> Response:
        """Convert parsed JSON into the typed Response.

        Override in subclasses. Default passthrough returns the raw parsed JSON.
        """
        return data  # type: ignore[return-value]
