"""Shared test fixtures.

Uses a temporary file-based SQLite database with check_same_thread=False
so that the task runtime can share it.
"""
from __future__ import annotations

import os
import tempfile

# Create a temp DB file BEFORE any application code imports core.db
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
_TEST_DB_PATH = _tmp.name
os.environ["ACCOUNT_MANAGER_DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
_TEST_APP_PASSWORD = "test-panel-password"
os.environ["APP_PASSWORD"] = _TEST_APP_PASSWORD

import pytest
from sqlmodel import SQLModel, create_engine

# Patch the engine before the app is created
from core import db as _db_module

_db_module.engine = create_engine(
    f"sqlite:///{_TEST_DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
)


@pytest.fixture(autouse=True)
def _reset_db():
    """Drop and recreate all tables between tests for full isolation."""
    SQLModel.metadata.drop_all(_db_module.engine)
    SQLModel.metadata.create_all(_db_module.engine)
    yield


@pytest.fixture()
def client(monkeypatch):
    """Authenticated FastAPI client without starting browser/background workers."""
    from services import solver_manager
    from services.task_runtime import task_runtime

    monkeypatch.setattr(solver_manager, "start_async", lambda: None)
    monkeypatch.setattr(solver_manager, "stop", lambda: None)
    monkeypatch.setattr(task_runtime, "start", lambda: None)
    monkeypatch.setattr(task_runtime, "stop", lambda: None)

    from main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.post("/api/auth/login", json={"password": _TEST_APP_PASSWORD})
        assert response.status_code == 200
        yield c


from fastapi.testclient import TestClient


def pytest_sessionfinish(session, exitstatus):
    """Clean up temp DB file."""
    try:
        os.unlink(_TEST_DB_PATH)
    except OSError:
        pass
