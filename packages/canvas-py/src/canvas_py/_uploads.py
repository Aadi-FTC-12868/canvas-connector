"""Bounded, credential-safe Canvas multipart uploads.

The official mobile apps request a context-specific target, pass its signed
fields unchanged before the file part, and decode the final File response.
Current protocol: https://canvas.instructure.com/doc/api/file.file_uploads.html
"""
from __future__ import annotations

import hashlib
import math
import mimetypes
import re
import tempfile
from pathlib import Path
from typing import BinaryIO

import httpx
from pydantic import ValidationError

from ._errors import CanvasError, CanvasTransportError
from ._http import CanvasHTTP
from .endpoints.file_uploads import ConfirmUpload, RequestUploadTarget, UploadContext
from .endpoints.files import GetFile
from .models.file import CanvasFile
from .models.upload import UploadTarget

_MAX_RESPONSE_BYTES = 1024 * 1024
_FINALIZE_PATH = re.compile(r"/api/v1/files/([1-9][0-9]*)(/create_success)?/?$")


class UploadVerificationError(CanvasError):
    """Bytes were sent, but the result could not be verified. Do not auto-retry.

    ``file_id`` is safe to return to the caller for inspection if known. This
    exception intentionally excludes signed URLs and storage response bodies.
    """

    def __init__(self, message: str, *, file_id: int | None = None, status: int | None = None):
        super().__init__(message, status=status)
        self.file_id = file_id


def _storage_upload(
    http: CanvasHTTP,
    target: UploadTarget,
    stream: BinaryIO,
    *,
    filename: str,
    content_type: str,
    timeout: float,
) -> tuple[int, str | None, bytes]:
    try:
        url = httpx.URL(target.upload_url)
    except httpx.InvalidURL:
        raise CanvasError("invalid Canvas upload target URL") from None
    # Reuse the download transport's public-host validation and DNS pinning.
    # The configured Canvas origin is trusted, including a deliberate local
    # Canvas installation; every external storage host must use public HTTPS.
    try:
        public_address = http._validate_download_url(url)
    except CanvasError as exc:
        raise CanvasError("Canvas upload target failed network safety validation") from exc
    connect_url = url.copy_with(host=public_address) if public_address else url
    # Hosted adapters may inject an egress policy for this anonymous transfer.
    # In particular, same-origin targets must not bypass their DNS safeguards.
    factory = getattr(http, "_anonymous_client_factory", None)
    anonymous_client = factory(timeout=timeout) if factory else httpx.Client(
        timeout=timeout, follow_redirects=False, trust_env=False,
    )
    with anonymous_client as anonymous:
        request = anonymous.build_request(
            "POST", connect_url,
            data=target.upload_params,
            # httpx emits all data fields first and the file part last.
            files={"file": (filename, stream, content_type)},
        )
        if public_address:
            request.headers["Host"] = url.netloc.decode("ascii")
            request.headers["Connection"] = "close"
            request.extensions["sni_hostname"] = url.host
        try:
            response = anonymous.send(request, auth=None, follow_redirects=False, stream=True)
            try:
                status = response.status_code
                if status not in (200, 201, 301, 302, 303, 307, 308):
                    raise UploadVerificationError(
                        "Canvas upload transfer failed; inspect Canvas before retrying",
                        status=status,
                    )
                body = bytearray()
                if status in (200, 201):
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise UploadVerificationError("Canvas upload response exceeded its size limit")
                        body.extend(chunk)
                return status, response.headers.get("Location"), bytes(body)
            finally:
                response.close()
        except httpx.RequestError:
            raise CanvasTransportError(
                "Canvas upload transport failed; outcome unknown, inspect Canvas before retrying"
            ) from None


def _decode_file(data: bytes) -> CanvasFile | None:
    try:
        file = CanvasFile.model_validate_json(data)
        return file if file.id > 0 else None
    except (ValidationError, ValueError):
        return None


def _finalize(http: CanvasHTTP, target: UploadTarget, location: str) -> CanvasFile:
    try:
        # Relative locations are relative to the original storage URL, never
        # its pinned IP. They are accepted only if they resolve back to Canvas.
        url = httpx.URL(target.upload_url).join(location)
        http._validate_api_url(url)
    except (CanvasError, httpx.InvalidURL):
        raise UploadVerificationError("Canvas upload returned an unsafe confirmation location") from None
    match = _FINALIZE_PATH.fullmatch(url.path)
    if match is None:
        raise UploadVerificationError("Canvas upload returned an unsupported confirmation location")
    expected_id = int(match.group(1))
    try:
        try:
            file = http.send_absolute(ConfirmUpload(str(url)), absolute_url=str(url))
        except CanvasError as exc:
            # Current docs require GET. Permit the older empty POST contract
            # only after a definite method rejection at create_success. Never
            # retry a possibly completed multipart transfer.
            if exc.status not in (405, 501) or not match.group(2):
                raise
            file = http.send_absolute(ConfirmUpload(str(url), method="POST"), absolute_url=str(url))
    except (CanvasError, ValidationError):
        raise UploadVerificationError(
            "Upload sent; Canvas confirmation failed, inspect the file before retrying",
            file_id=expected_id,
        ) from None
    if file.id != expected_id:
        raise UploadVerificationError(
            "Canvas upload confirmation returned a different file ID", file_id=expected_id,
        )
    return file


def upload_file(
    http: CanvasHTTP,
    path: Path | str,
    *,
    context: UploadContext,
    name: str | None = None,
    content_type: str | None = None,
    on_duplicate: str = "rename",
    parent_folder_path: str | None = None,
    parent_folder_id: int | None = None,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    verify: bool = True,
    timeout: float = 120.0,
) -> CanvasFile:
    """Upload a private snapshot of local bytes and verify the finalized ID/size.

    Expected size and SHA-256 bind a reviewed action to the exact local bytes.
    This never submits an assignment, even in assignment_submission context.
    ``verify=False`` skips the fresh metadata read only; the finalized response
    must still contain the expected size and a positive file ID. The API does
    not expose a content hash, so server verification is metadata verification.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError("upload source is not a regular local file")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if expected_size is not None and (type(expected_size) is not int or expected_size < 0):
        raise ValueError("expected_size must be a nonnegative integer")
    if expected_sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("expected_sha256 must be a SHA-256 hex digest")
    filename = source.name if name is None else name
    mime_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # Validate all arguments before copying bytes or creating a pending file.
    request = RequestUploadTarget(
        context, name=filename, content_type=mime_type,
        on_duplicate=on_duplicate,  # type: ignore[arg-type]
        parent_folder_path=parent_folder_path, parent_folder_id=parent_folder_id,
    )
    with tempfile.TemporaryFile(mode="w+b") as snapshot:
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as incoming:
            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                size += len(chunk)
                if expected_size is not None and size > expected_size:
                    raise ValueError("upload source size no longer matches the reviewed file")
                digest.update(chunk)
                snapshot.write(chunk)
        if expected_size is not None and size != expected_size:
            raise ValueError("upload source size no longer matches the reviewed file")
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256.lower():
            raise ValueError("upload source hash no longer matches the reviewed file")
        snapshot.seek(0)
        request.size = size  # Ask Canvas to check quota before sending the bytes.
        target = http.send(request)
        _status, location, body = _storage_upload(
            http, target, snapshot, filename=filename, content_type=mime_type, timeout=timeout,
        )
    # A Location on a successful response is authoritative and must be followed
    # to finalize older storage uploads even if that response also has JSON.
    file = _finalize(http, target, location) if location else _decode_file(body)
    if file is None:
        raise UploadVerificationError(
            "Upload sent; response had neither a valid file nor a Location header, inspect Canvas before retrying"
        )
    if file.size is not None and file.size != size:
        raise UploadVerificationError("Canvas upload returned a different file size", file_id=file.id)
    if verify:
        try:
            verified = http.send(GetFile(file.id))
        except (CanvasError, ValidationError):
            raise UploadVerificationError(
                "Upload sent; finalized file metadata could not be read, inspect Canvas before retrying",
                file_id=file.id,
            ) from None
        if verified.id != file.id or verified.size != size:
            raise UploadVerificationError("Canvas upload metadata failed ID/size verification", file_id=file.id)
        if parent_folder_id is not None and verified.folder_id != parent_folder_id:
            raise UploadVerificationError("Canvas upload metadata reported a different folder", file_id=file.id)
        file = verified
    elif file.size != size:
        raise UploadVerificationError("Canvas upload response did not verify file size", file_id=file.id)
    return file
