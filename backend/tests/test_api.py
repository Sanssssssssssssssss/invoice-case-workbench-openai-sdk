from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_agent_turn_api(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_WORKSPACE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("INVOICE_AGENT_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()

    client = TestClient(app)
    response = client.post(
        "/api/agent/turn",
        json={"case_id": "case_api_live_test", "message": "我现在需要准备什么？", "attachments": []},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["case_id"] == "case_api_live_test"
    assert payload["reply"]
    assert payload["trace"]["model_calls"]
    assert payload["trace"]["planner_actions"]


def test_upload_attachment_api(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_WORKSPACE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("INVOICE_AGENT_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()

    client = TestClient(app)
    response = client.post(
        "/api/cases/case_api_upload/attachments",
        files={"file": ("invoice.md", b"Invoice INV-001 Amount 10000 CNY", "text/markdown")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "invoice.md"
    assert payload["relative_path"].startswith("attachments/")
    assert payload["bytes"] > 0
