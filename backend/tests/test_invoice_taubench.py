from __future__ import annotations

import sys

import pytest

from app.config import Settings
from benchmarks.invoice_tau.io import SCENARIOS_ROOT, discover_scenarios, load_scenario, validate_clean_text
from benchmarks.invoice_tau.judge import JudgeOutput, _judge_result_from_output, _judge_settings, run_llm_judge
from benchmarks.invoice_tau.models import BenchmarkSummary, ExpectedSpec, LlmJudgeResult, REPO_ROOT, ScenarioRunResult, UserTurnSpec
from benchmarks.invoice_tau.profiles import MATERIAL_REVIEW_SCENARIOS, get_profile
from benchmarks.invoice_tau.reporting import render_markdown, summarize_results
from benchmarks.invoice_tau.runner import InvoiceTauBenchRunner, _attachments_for_turn
from benchmarks.invoice_tau import score_report
from benchmarks.invoice_tau.verifiers import verify_run


REQUIRED_SCENARIOS = {
    "chat_capability_001",
    "create_case_001",
    "clean_complete_review_001",
    "material_advice_001",
    "clean_complete_report_001",
    "missing_grn_001",
    "duplicate_conflict_001",
    "prompt_injection_001",
    "reject_pdf_approval_001",
}
GOLDEN_SCENARIOS_ROOT = REPO_ROOT / "golden_cases" / "session_invoice_cases_v1"
LIVE_ACCEPTANCE_ROOT = REPO_ROOT / "benchmarks" / "invoice_tau" / "live_acceptance"
LIVE_ACCEPTANCE_SCENARIOS = {
    "amount_conflict_live_canary",
    "partial_receipt_live_canary",
    "duplicate_reversal_live_canary",
    "vendor_identity_active_live_canary",
}
REQUIRED_GOLDEN_SCENARIOS = {
    "golden_case_01_clean_jpg",
    "golden_case_02_amount_conflict",
    "golden_case_03_duplicate_pressure",
    "golden_case_04_bank_change",
    "golden_case_05_clean_pdf_report",
    "golden_case_06_duplicate_pdf",
}


def test_invoice_taubench_loads_all_scenarios() -> None:
    paths = discover_scenarios()
    scenario_ids = {load_scenario(path)[0].id for path in paths}
    assert REQUIRED_SCENARIOS.issubset(scenario_ids)
    assert len(paths) >= 9


def test_invoice_taubench_profiles_expand_expected_scenarios() -> None:
    smoke = get_profile("chain_live_smoke")
    core = get_profile("chain_live_core")
    full = get_profile("chain_live_full")
    material = get_profile("material_live_regression")
    scripted = get_profile("scripted_full")

    assert smoke.mode == "live"
    assert smoke.scenarios == ("chat_capability_001", "material_advice_001", "create_case_001")
    assert smoke.llm_judge is False
    assert core.mode == "live"
    assert core.llm_judge is True
    assert set(core.scenarios) == {
        "chat_capability_001",
        "material_advice_001",
        "create_case_001",
        "clean_complete_review_001",
        "missing_grn_001",
        "duplicate_conflict_001",
    }
    assert full.mode == "live"
    assert full.scenarios == ()
    assert full.llm_judge is True
    assert material.mode == "live"
    assert material.llm_judge is True
    assert set(material.scenarios) == MATERIAL_REVIEW_SCENARIOS
    assert len([item for item in material.scenarios if item in MATERIAL_REVIEW_SCENARIOS]) == 3
    assert scripted.mode == "scripted"
    assert scripted.scenarios == ()
    assert scripted.llm_judge is False


def test_invoice_taubench_attachment_references_exist() -> None:
    for path in discover_scenarios():
        scenario, _expected, scenario_dir = load_scenario(path)
        for turn in scenario.user_script:
            assert len(_attachments_for_turn(turn, scenario_dir)) == len(turn.attach)


def test_live_acceptance_scenarios_and_attachments_load() -> None:
    loaded = [load_scenario(path) for path in discover_scenarios(root=LIVE_ACCEPTANCE_ROOT)]

    assert {scenario.id for scenario, _expected, _scenario_dir in loaded} == LIVE_ACCEPTANCE_SCENARIOS
    for scenario, _expected, scenario_dir in loaded:
        for turn in scenario.user_script:
            assert len(_attachments_for_turn(turn, scenario_dir)) == len(turn.attach)


def test_golden_handtest_scenarios_are_complete() -> None:
    paths = discover_scenarios(root=GOLDEN_SCENARIOS_ROOT)
    loaded = [load_scenario(path) for path in paths]

    assert {scenario.id for scenario, _expected, _scenario_dir in loaded} == REQUIRED_GOLDEN_SCENARIOS
    for scenario, expected, scenario_dir in loaded:
        assert expected.case_status
        assert expected.requirements
        for turn in scenario.user_script:
            assert len(_attachments_for_turn(turn, scenario_dir)) == len(turn.attach)


def test_invoice_taubench_attachment_paths_cannot_escape_scenario(tmp_path) -> None:
    scenario_dir = tmp_path / "scenario"
    scenario_dir.mkdir()
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="inside the scenario directory"):
        _attachments_for_turn(UserTurnSpec(message="review", attach=["../outside.txt"]), scenario_dir)


def test_invoice_taubench_scenarios_have_clean_utf8_text() -> None:
    for path in discover_scenarios():
        scenario, expected, scenario_dir = load_scenario(path)
        validate_clean_text(scenario.model_dump(mode="json"), path=scenario_dir / "scenario.json")
        validate_clean_text(expected.model_dump(mode="json"), path=scenario_dir / "expected.json")

    with pytest.raises(ValueError):
        validate_clean_text({"message": "??? undefined"}, path=SCENARIOS_ROOT / "bad.json")


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
    assert checks["requirement:no_active_duplicate"].passed
    assert checks["forbidden_requirement:duplicate_payment_screen:conflict"].passed
    assert checks["proof_status"].passed
    assert checks["proof_outcome"].passed
    assert checks["risk_flag_contains:duplicate_payment_hit"].passed
    assert checks["trace_must_not_call:render_pdf"].passed


def test_invoice_taubench_create_case_scenario_passes_scripted(tmp_path) -> None:
    runner = InvoiceTauBenchRunner(report_dir=tmp_path / "reports", mode="scripted")
    result = runner.run_path(SCENARIOS_ROOT / "create_case_001")
    assert result.passed, [check.name for check in result.checks if not check.passed]
    assert result.category == "create_case"


def test_invoice_taubench_verifier_new_fields() -> None:
    result = ScenarioRunResult(
        scenario_id="verifier_fields",
        category="material_review",
        final_reply="材料完整，可以进入报告阶段。",
        case_state={
            "status": "ready_for_report",
            "requirements": [{"id": "invoice", "status": "satisfied"}],
            "evidence_items": [{"type": "invoice"}],
        },
        metrics={"events": [], "error": ""},
    )
    expected = ExpectedSpec(
        case_status="ready_for_report",
        min_evidence_count=1,
        max_evidence_count=1,
        reply_any_of=["报告", "ready_for_report"],
        requirements={"invoice": "satisfied"},
    )

    checks = {check.name: check for check in verify_run(result, expected, SCENARIOS_ROOT)}

    assert checks["case_status"].passed
    assert checks["min_evidence_count"].passed
    assert checks["max_evidence_count"].passed
    assert checks["reply_any_of"].passed
    assert checks["encoding_clean:undefined"].passed


def test_invoice_taubench_enforces_per_tool_call_limit() -> None:
    result = ScenarioRunResult(
        scenario_id="tool_call_limit",
        trace={"tool_calls": [{"tool": "render_pdf"}, {"tool": "render_pdf"}]},
    )
    expected = ExpectedSpec(max_tool_call_counts={"render_pdf": 1})

    checks = {check.name: check for check in verify_run(result, expected, SCENARIOS_ROOT)}

    assert not checks["trace_max_tool_calls:render_pdf"].passed
    assert checks["trace_max_tool_calls:render_pdf"].details == {"observed": 2, "max": 1}


def test_invoice_taubench_does_not_fallback_when_target_proof_is_missing() -> None:
    result = ScenarioRunResult(
        scenario_id="missing_target_proof",
        case_state={
            "compiled_proof": {
                "decision": {"requirement_id": "other", "proof_status": "PROVED"},
                "decisions": [{"requirement_id": "other", "proof_status": "PROVED"}],
            }
        },
    )
    expected = ExpectedSpec(proof_requirement_id="target", proof_status="PROVED")

    checks = {check.name: check for check in verify_run(result, expected, SCENARIOS_ROOT)}

    assert not checks["proof_status"].passed


def test_invoice_taubench_can_assert_internal_proof_checks() -> None:
    result = ScenarioRunResult(
        scenario_id="proof_checks",
        case_state={"compiled_proof": {"checks": [{"id": "CHK_TOLERANCE", "status": "DISPROVED"}]}},
    )
    expected = ExpectedSpec(proof_checks={"CHK_TOLERANCE": "DISPROVED", "CHK_SCOPE": "PROVED"})

    checks = {check.name: check for check in verify_run(result, expected, SCENARIOS_ROOT)}

    assert checks["proof_check:CHK_TOLERANCE"].passed
    assert not checks["proof_check:CHK_SCOPE"].passed


def test_invoice_taubench_proof_details_are_scoped_to_target_view() -> None:
    result = ScenarioRunResult(
        scenario_id="proof_view_scope",
        case_state={
            "compiled_proof": {
                "decisions": [
                    {
                        "requirement_id": "target",
                        "program_id": "target-view",
                        "proof_status": "INCOMPLETE",
                        "obligation_ids": [],
                    }
                ],
                "checks": [
                    {"id": "CHK_SCOPE", "program_id": "foreign-view", "status": "PROVED"}
                ],
                "obligations": [{"id": "OBL_FOREIGN"}],
            }
        },
    )
    expected = ExpectedSpec(
        proof_requirement_id="target",
        proof_checks={"CHK_SCOPE": "PROVED"},
        proof_obligation_ids=["OBL_FOREIGN"],
    )

    checks = {check.name: check for check in verify_run(result, expected, SCENARIOS_ROOT)}

    assert not checks["proof_check:CHK_SCOPE"].passed
    assert not checks["proof_obligation:OBL_FOREIGN"].passed


def test_invoice_taubench_risk_flag_aliases_accept_live_variants() -> None:
    result = ScenarioRunResult(
        scenario_id="risk_aliases",
        case_state={
            "risk_flags": [
                "duplicate_payment_unresolved",
                "source_pollution_quarantine",
            ]
        },
    )
    expected = ExpectedSpec(must_have_risk_flags=["duplicate_payment_hit", "prompt_injection_quarantine"])

    checks = {check.name: check for check in verify_run(result, expected, SCENARIOS_ROOT)}

    assert checks["risk_flag_contains:duplicate_payment_hit"].passed
    assert checks["risk_flag_contains:prompt_injection_quarantine"].passed


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


def test_invoice_taubench_summary_separates_contract_and_llm_quality() -> None:
    result = ScenarioRunResult(
        scenario_id="quality_gap",
        category="material_review",
        passed=True,
        score=1.0,
        llm_judge=LlmJudgeResult(
            enabled=True,
            passed=False,
            score=0.42,
            pass_threshold=0.75,
            rationale="Contract checks passed, but the answer was too vague.",
            dimensions={
                "state_correctness": 1.0,
                "evidence_grounding": 0.4,
                "tool_process": 0.7,
                "safety_approval": 1.0,
                "report_quality": 0.2,
                "user_communication": 0.3,
                "efficiency": 0.6,
            },
        ),
    )

    summary = summarize_results([result], mode="live", k=1, profile="chain_live_core")
    markdown = render_markdown(summary)

    assert summary.contract_pass_at_1 == 1.0
    assert summary.deterministic_score == 1.0
    assert summary.llm_quality_score == 0.42
    assert summary.judge_dimension_scores["evidence_grounding"] == 0.4
    assert "contract_pass@1" in markdown
    assert "llm_quality_score: 0.420" in markdown
    assert "Low LLM Quality Scores" in markdown


def test_invoice_taubench_judge_config_uses_independent_env(monkeypatch) -> None:
    settings = Settings(llm_model="app-model", llm_base_url="https://app.example/v1", llm_api_key="app-key")
    monkeypatch.setenv("INVOICE_TAUBENCH_JUDGE_MODEL", "judge-model")
    monkeypatch.setenv("INVOICE_TAUBENCH_JUDGE_BASE_URL", "https://judge.example/v1")
    monkeypatch.setenv("INVOICE_TAUBENCH_JUDGE_API_KEY", "judge-key")

    judge_settings = _judge_settings(settings)

    assert judge_settings.llm_model == "judge-model"
    assert judge_settings.llm_base_url == "https://judge.example/v1"
    assert judge_settings.llm_api_key == "judge-key"


def test_invoice_taubench_llm_judge_missing_key_is_error(monkeypatch) -> None:
    settings = Settings(llm_model="judge-model", llm_api_key=None)
    monkeypatch.delenv("INVOICE_TAUBENCH_JUDGE_API_KEY", raising=False)
    monkeypatch.setattr("benchmarks.invoice_tau.judge.get_settings", lambda: settings)

    judged = run_llm_judge(ScenarioRunResult(scenario_id="missing_key"), ExpectedSpec(), enabled=True)

    assert judged.enabled is True
    assert judged.passed is False
    assert judged.error
    assert "API_KEY" in judged.error


def test_invoice_taubench_judge_score_uses_dimension_mean_when_aggregate_missing() -> None:
    output = JudgeOutput(
        passed=False,
        score=0.0,
        state_correctness=1.0,
        evidence_grounding=0.9,
        tool_process=0.8,
        safety_approval=1.0,
        report_quality=0.7,
        user_communication=0.9,
        efficiency=0.8,
        rationale="Aggregate score omitted by provider, dimensions are valid.",
    )

    judged = _judge_result_from_output(output, pass_threshold=0.75, model="fake")

    assert judged.score == pytest.approx(0.8714)
    assert judged.passed is True
    assert judged.dimensions["report_quality"] == 0.7


def test_invoice_taubench_score_report_scores_saved_runs_without_agent_rerun(tmp_path, monkeypatch) -> None:
    report_dir = tmp_path / "saved_report"
    report_dir.mkdir()
    result = ScenarioRunResult(
        scenario_id="chat_capability_001",
        run_index=1,
        mode="live",
        passed=True,
        score=1.0,
        report_dir=str(report_dir),
    )
    summary = BenchmarkSummary(
        profile="chain_live_smoke",
        mode="live",
        scenario_count=1,
        total_runs=1,
        passed_runs=1,
        pass_at_1=1.0,
        contract_pass_at_1=1.0,
        average_score=1.0,
        deterministic_score=1.0,
        scenario_results=[result],
    )
    (report_dir / "latest.json").write_text(summary.model_dump_json(), encoding="utf-8")

    def fake_judge(_result: ScenarioRunResult, _expected: ExpectedSpec, *, enabled: bool = False) -> LlmJudgeResult:
        assert enabled is True
        return LlmJudgeResult(
            enabled=True,
            passed=True,
            score=0.81,
            dimensions={"state_correctness": 0.9, "evidence_grounding": 0.72},
            rationale="Good enough.",
            model="fake-judge",
        )

    monkeypatch.setattr(score_report, "run_llm_judge", fake_judge)
    monkeypatch.setattr(sys, "argv", ["score_report", "--report-dir", str(report_dir), "--write"])

    assert score_report.main() == 0
    updated = BenchmarkSummary.model_validate_json((report_dir / "latest.json").read_text(encoding="utf-8"))

    assert updated.judged_runs == 1
    assert updated.llm_quality_score == 0.81
    assert updated.scenario_results[0].llm_judge.model == "fake-judge"
    assert "llm_quality_score: 0.810" in (report_dir / "latest.md").read_text(encoding="utf-8")
