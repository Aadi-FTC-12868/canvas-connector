import os
from pathlib import Path

import pytest


@pytest.fixture
def core_python():
    candidate = os.environ.get("CANVAS_CORE_PYTHON")
    if candidate is None:
        candidate = str(Path(__file__).resolve().parents[3] / ".venv" / "bin" / "python")
    path = Path(candidate)
    if not path.is_absolute() or not path.is_file():
        pytest.fail("CANVAS_CORE_PYTHON must select the separately installed core Python")
    return str(path)

