from __future__ import annotations

import tempfile
from pathlib import Path

import database


def pytest_sessionstart(session) -> None:
    database.DB_PATH = Path(tempfile.gettempdir()) / "citytwin-test.db"
    if database.DB_PATH.exists():
        database.DB_PATH.unlink()
    database.init_db()
