from __future__ import annotations

from pathlib import Path

from app.tools.catalog import ToolCatalog


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_APP = REPO_ROOT / "backend" / "app"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_runtime_has_no_graph_chain_runtime_imports() -> None:
    forbidden = (
        "lang" + "graph",
        "State" + "Graph",
        "Sqlite" + "Saver",
        "lang" + "chain",
    )
    hits: list[str] = []
    for path in BACKEND_APP.rglob("*.py"):
        text = _read(path)
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(REPO_ROOT)}:{token}")
    assert hits == []


def test_llm_client_has_no_legacy_tool_call_compatibility_layer() -> None:
    text = _read(BACKEND_APP / "llm.py")
    forbidden = (
        "Native" + "ToolMessage",
        "complete" + "_with_tools",
        "client.chat." + "completions.create",
        "openai_sdk" + "_compat",
        "native_tool" + "_calls",
    )
    hits = [token for token in forbidden if token in text]
    assert hits == []


def test_specialists_are_native_agent_as_tool_runtime() -> None:
    registry_text = _read(BACKEND_APP / "agents" / "registry.py")
    assert ".as_tool(" in registry_text
    assert "runtime = \"agent_as_tool\"" in registry_text
    assert "agent_as_tool" in registry_text
    assert "MaterialsAdvisor(" not in registry_text
    assert "EvidenceReviewer(" not in registry_text
    assert "CasePatchWriter(" not in registry_text
    assert "ReportWriter(" not in registry_text
    for path in (
        BACKEND_APP / "agents" / "materials_advisor" / "agent.py",
        BACKEND_APP / "agents" / "patch_builder" / "agent.py",
        BACKEND_APP / "agents" / "report_writer" / "agent.py",
    ):
        text = _read(path)
        assert "class " not in text
        assert "complete_structured" not in text


def test_tool_catalog_keeps_patch_internal_and_report_writes_approval_gated() -> None:
    catalog = ToolCatalog()
    visible_names = {item["name"] for item in catalog.visible_tools()}
    assert "write_case_patch" not in visible_names
    assert catalog.get("write_case_patch").internal_only is True
    assert catalog.get("write_case_file").approval_mode == "always"
    assert catalog.get("render_pdf").approval_mode == "always"


def test_frontend_exposes_approval_resume_client() -> None:
    api_text = _read(REPO_ROOT / "renderer" / "src" / "lib" / "api.ts")
    app_text = _read(REPO_ROOT / "renderer" / "src" / "App.tsx")
    chat_text = _read(REPO_ROOT / "renderer" / "src" / "components" / "CaseChat.tsx")
    assert "resumeApproval" in api_text
    assert "/approval" in api_text
    assert "pendingApprovals" in app_text
    assert "ApprovalPanel" in chat_text


def test_streaming_runtime_uses_push_sse_and_threaded_tools() -> None:
    streaming_text = _read(BACKEND_APP / "runtime" / "streaming.py")
    api_text = _read(BACKEND_APP / "api" / "agent_runs.py")
    runner_text = _read(BACKEND_APP / "runtime" / "turn_runner.py")
    assert "async def subscribe" in streaming_text
    assert "await asyncio.sleep(0.1)" not in api_text
    assert "asyncio.to_thread(call_tool)" in runner_text
    assert "threading.RLock" in runner_text
