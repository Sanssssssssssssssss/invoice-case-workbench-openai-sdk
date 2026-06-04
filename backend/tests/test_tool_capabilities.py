from __future__ import annotations

from dataclasses import replace
from typing import get_args

import pytest
from pydantic import ValidationError

from app.context import ContextManager
from app.harness import HarnessRuntime
from app.llm import LlmClient
from app.runtime.tool_runtime import ToolRuntime
from app.state.case_store import CaseStore
from app.state.schemas import ToolName
from app.tools.capabilities import TOOL_CAPABILITIES
from app.tools.catalog import ToolCatalog
from app.tools.file_workspace import FileWorkspace


def test_all_existing_tools_have_capability_metadata() -> None:
    expected = set(get_args(ToolName)) | {"write_case_patch"}

    assert set(TOOL_CAPABILITIES) == expected
    for name, capability in TOOL_CAPABILITIES.items():
        metadata = capability.trace_metadata()
        assert capability.name == name
        assert metadata["side_effect"]
        assert metadata["idempotency"]
        assert metadata["context_exposure"]
        assert metadata["error_taxonomy"]


def test_tool_catalog_validates_input_payload_shape(tmp_path) -> None:
    registry = ToolCatalog(FileWorkspace(CaseStore(tmp_path / "cases")))

    with pytest.raises(ValidationError):
        registry.call("read_case_state", "case_001", {"unexpected": "value"})


def test_tool_catalog_rejects_unknown_tool(tmp_path) -> None:
    registry = ToolCatalog(FileWorkspace(CaseStore(tmp_path / "cases")))

    with pytest.raises(KeyError):
        registry.call("unknown_tool", "case_001", {})


def test_tool_catalog_visible_tools_and_internal_only(tmp_path) -> None:
    catalog = ToolCatalog(FileWorkspace(CaseStore(tmp_path / "cases")))
    visible = {item["name"]: item for item in catalog.visible_tools()}

    assert "write_case_patch" not in visible
    assert visible["read_attachment"]["risk_level"] == "extract"
    assert visible["read_attachment"]["approval_mode"] == "never"
    assert catalog.get("write_case_patch").internal_only is True


def test_tool_catalog_blocks_non_internal_write_case_patch(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    catalog = ToolCatalog(FileWorkspace(store))

    with pytest.raises(PermissionError):
        catalog.call("write_case_patch", "case_patch", {"patch": {}}, internal=False)


def test_tool_catalog_blocks_oversized_payload_before_validation(tmp_path) -> None:
    catalog = ToolCatalog(FileWorkspace(CaseStore(tmp_path / "cases")))

    with pytest.raises(ValueError, match="Tool input too large"):
        catalog.call("list_case_files", "case_big", {"unexpected": "x" * 5000})


def test_tool_catalog_supports_approval_override_for_tests(tmp_path) -> None:
    catalog = ToolCatalog(FileWorkspace(CaseStore(tmp_path / "cases")))
    spec = catalog.get("list_case_files")
    catalog._specs["list_case_files"] = replace(spec, approval_mode="always")  # noqa: SLF001

    assert catalog.get("list_case_files").approval_mode == "always"
    assert catalog.capability_metadata("list_case_files")["approval_mode"] == "always"


def test_report_tools_keep_timestamped_path_normalization(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    workspace = FileWorkspace(store)
    registry = ToolCatalog(workspace)
    run_state = type("RunState", (), {"started_at": "2026-05-29T23:59:01+00:00"})()

    write_result = registry.call(
        "write_case_file",
        "case_report",
        {"relative_path": "reports/final_report.md", "content": "# Report\n\nBody"},
        run_state=run_state,
    )
    render_result = registry.call(
        "render_pdf",
        "case_report",
        {"markdown_path": "reports/final_report.md", "pdf_path": "reports/final_report.pdf"},
        run_state=run_state,
    )

    assert write_result["relative_path"] == "reports/final_report_20260529_235901.md"
    assert render_result["markdown_path"] == "reports/final_report_20260529_235901.md"
    assert render_result["pdf_path"] == "reports/final_report_20260529_235901.pdf"


def test_report_tools_use_run_report_paths_as_single_source(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    workspace = FileWorkspace(store)
    registry = ToolCatalog(workspace)
    run_state = type("RunState", (), {"started_at": "2026-05-30T14:40:19+00:00"})()

    workspace.write_case_file("case_report_pair", "reports/final_report_20260530_144019.md", "# Report\n\nBody")
    render_result = registry.call(
        "render_pdf",
        "case_report_pair",
        {
            "markdown_path": "reports/final_report_19990101_000000.md",
            "pdf_path": "reports/final_report_20000101_000000.pdf",
        },
        run_state=run_state,
    )

    assert render_result["markdown_path"] == "reports/final_report_20260530_144019.md"
    assert render_result["pdf_path"] == "reports/final_report_20260530_144019.pdf"


def test_tool_runtime_records_capability_metadata_in_trace(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    llm = LlmClient()
    harness = HarnessRuntime(store)
    context = ContextManager(store, llm)
    runtime = ToolRuntime(ToolCatalog(FileWorkspace(store)), harness, context)
    state = harness.begin_run("case_trace_metadata", "list files")

    runtime.call(state, "list_case_files", {})

    call = state.tool_calls[0]
    assert call["capability"]["side_effect"] == "case_read"
    assert call["capability"]["idempotency"] == "safe"
    assert call["capability"]["context_exposure"] == "summary_only"
    assert call["capability"]["risk_level"] == "read"
    assert call["capability"]["approval_mode"] == "never"
    assert call["capability"]["internal_only"] is False
    assert "unsafe_workspace_path" in call["capability"]["error_taxonomy"]


def test_read_case_state_tool_runtime_records_without_summary_fallback_error(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    llm = LlmClient()
    harness = HarnessRuntime(store)
    context = ContextManager(store, llm)
    runtime = ToolRuntime(ToolCatalog(FileWorkspace(store)), harness, context)
    state = harness.begin_run("case_read_state_metadata", "read state")

    runtime.call(state, "read_case_state", {})

    assert state.tool_calls[0]["tool"] == "read_case_state"
    assert not state.tool_calls[0]["error"]
    assert state.observations[0]["name"] == "read_case_state"
