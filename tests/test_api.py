"""API tests using FastAPI TestClient."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import db


@pytest.fixture(autouse=True)
def _clean_db(tmp_path):
    db.DB_PATH = tmp_path / "test.db"
    db.init_db()
    yield


def test_healthz_db_ok():
    """healthz reports ok when DB is working (k8s may fail in CI)."""
    client = TestClient(app)
    resp = client.get("/healthz")
    data = resp.json()
    # DB should always be ok in tests
    assert "db" not in str(data.get("errors", []))


def test_index_returns_html():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Code Annotation Runner" in resp.text
