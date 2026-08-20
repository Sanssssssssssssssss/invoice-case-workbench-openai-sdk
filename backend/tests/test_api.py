from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_blocking_agent_turn_api_is_removed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_WORKSPACE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("INVOICE_AGENT_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()

    client = TestClient(app)
    response = client.post(
        "/api/agent/turn",
        json={"case_id": "case_api_live_test", "message": "我现在需要准备什么？", "attachments": []},
    )
    assert response.status_code == 404


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


def test_upload_attachment_rejects_invalid_case_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_WORKSPACE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("INVOICE_AGENT_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()

    response = TestClient(app).post(
        "/api/cases/..bad/attachments",
        files={"file": ("invoice.md", b"invoice", "text/markdown")},
    )

    assert response.status_code == 400


def test_upload_attachment_rejects_oversized_body(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_WORKSPACE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("INVOICE_AGENT_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    import app.main as main

    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 8)
    response = TestClient(app).post(
        "/api/cases/case_too_large/attachments",
        files={"file": ("invoice.md", b"123456789", "text/markdown")},
    )

    assert response.status_code == 413
    assert not list((tmp_path / "cases" / "case_too_large" / "attachments").glob("*invoice.md"))


def test_cors_allows_electron_renderer_and_rejects_other_origins() -> None:
    client = TestClient(app)
    headers = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }

    allowed = client.options(
        "/api/cases",
        headers={**headers, "Origin": "http://127.0.0.1:5174"},
    )
    denied = client.options(
        "/api/cases",
        headers={**headers, "Origin": "https://attacker.example"},
    )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5174"
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers
