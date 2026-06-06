from __future__ import annotations

import pytest

from benchmarks.invoice_tau.io import SCENARIOS_ROOT, discover_scenarios, load_scenario
from benchmarks.invoice_tau.runner import InvoiceTauBenchRunner


REQUIRED_SCENARIOS = {
    "chat_capability_001",
    "material_advice_001",
    "clean_complete_report_001",
    "missing_grn_001",
    "duplicate_conflict_001",
    "prompt_injection_001",
    "reject_pdf_approval_001",
}


def test_invoice_taubench_loads_all_scenarios() -> None:
    paths = discover_scenarios()
    scenario_ids = {load_scenario(path)[0].id for path in paths}
    assert REQUIRED_SCENARIOS.issubset(scenario_ids)
    assert len(paths) >= 7


def test_invoice_taubench_attachment_references_exist() -> None:
    for path in discover_scenarios():
        scenario, _expected, scenario_dir = load_scenario(path)
        for turn in scenario.user_script:
            for name in turn.attach:
                assert (scenario_dir / "attachments" / name).is_file(), f"{scenario.id} references missing attachment {name}"


@pytest.mark.parametrize("scenario_id", ["chat_capability_001", "material_advice_001"])
def test_invoice_taubench_scripted_smoke_scenarios_pass(tmp_path, scenario_id: str) -> None:
    runner = InvoiceTauBenchRunner(report_dir=tmp_path / "reports", mode="scripted")
    result = runner.run_path(SCENARIOS_ROOT / scenario_id)
    assert result.passed, [check.name for check in result.checks if not check.passed]


def test_invoice_taubench_duplicate_conflict_verifier_fields(tmp_path) -> None:
    runner = InvoiceTauBenchRunner(report_dir=tmp_path / "reports", mode="scripted")
    result = runner.run_path(SCENARIOS_ROOT / "duplicate_conflict_001")
    assert result.passed, [check.name for check in result.checks if not check.passed]

    checks = {check.name: check for check in result.checks}
    assert checks["requirement:duplicate_payment_screen"].passed
    assert checks["forbidden_requirement:duplicate_payment_screen:satisfied"].passed
    assert checks["risk_flag_contains:duplicate_payment_hit"].passed
    assert checks["trace_must_not_call:render_pdf"].passed


def test_invoice_taubench_reject_pdf_approval_path(tmp_path) -> None:
    runner = InvoiceTauBenchRunner(report_dir=tmp_path / "reports", mode="scripted")
    result = runner.run_path(SCENARIOS_ROOT / "reject_pdf_approval_001")
    assert result.passed, [check.name for check in result.checks if not check.passed]

    checks = {check.name: check for check in result.checks}
    assert checks["trace_must_approve:write_case_file"].passed
    assert checks["trace_must_approve:render_pdf"].passed
    assert checks["artifact_exists:reports/*.md"].passed
    assert checks["artifact_absent:reports/*.pdf"].passed
    assert checks["approval_bypass_forbidden"].passed
