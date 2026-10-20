"""Step 1 checks: config loads and the app starts."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app


def test_settings_read_env(monkeypatch):
    monkeypatch.setenv("APPROVAL_THRESHOLD", "7000")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    s = Settings(_env_file=None)
    assert s.approval_threshold == 7000
    assert s.llm_model == "test-model"


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.history_limit == 10
    assert s.approval_ttl_minutes == 10


def test_health_endpoint():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
