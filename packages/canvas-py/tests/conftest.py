"""Shared pytest fixtures for canvas-py tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Prevent tests from inheriting live credential configuration."""
    for key in ("CANVAS_BASE_URL", "CANVAS_API_TOKEN", "CANVAS_TOKEN_EXPIRES", "CANVAS_ENV_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir("/tmp")
    yield
