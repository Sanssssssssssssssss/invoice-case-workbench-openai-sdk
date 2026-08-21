from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.compiler_runtime.kernel import compile_review_artifact
from app.compiler_runtime.models import EvidenceIR, ProofPlan, ReviewArtifact
from app.evals.business.models import (
    BusinessEvalCase,
    BusinessEvalOracle,
    EvalAttachment,
    EvalResult,
    EvalSnapshot,
    MeaningOracle,
    ReportArtifact,
)
from app.evals.business.report import render_eval_report
from app.evals.business.scorer import (
    STAGE_WEIGHTS,
    _canonical_projection_violations,
    _claim_matches_source_fact,
    _equation_witnesses,
    _invalid_equation_claims,
    _claim_is_grounded,
    _locator_supports_quote,
    _meaning_groups_match,
    _predicate_matches_options,
    _text_has_decimal,
    score_business_eval,
)


def _case() -> BusinessEvalCase:
    return BusinessEvalCase(
        case_id="invoice_arithmetic_conflict_001",
        case_version="1",
        title="发票内部总额存在明确错误",
        suite="atomic_dev",
        business_holes=["invoice_arithmetic"],
        user_message="请帮我审核这张发票，完成申报前检查，并生成一份中文审核报告。",
        policy_version="aurora_ap_lite_v1",
        auto_approve_tools=["write_case_file", "render_pdf"],
        attachments=[
            EvalAttachment(
                id="invoice",
                path="attachments/invoice.pdf",
                sha256="0" * 64,
                content_type="application/pdf",
                source_url="https://example.invalid/invoice.pdf",
                source_revision="fixture",
                license="MIT",
            )
        ],
    )


def _oracle() -> BusinessEvalOracle:
    path = (
        Path(__file__).resolve().parents[2]
        / "evals/business_v1/cases/invoice_arithmetic_conflict_001/oracle.json"
    )
    return BusinessEvalOracle.model_validate_json(path.read_text(encoding="utf-8"))


def _snapshot() -> EvalSnapshot:
    oracle = _oracle()
    source_facts = [fact for fact in oracle.facts if fact.origin == "source"]
    claims = [
        {
            "id": f"claim_{fact.id}",
            "subject": "invoice",
            "predicate": fact.predicate_options[0] if fact.predicate_options else fact.id,
            "value": f"{fact.value} {fact.currency}".strip(),
            "source_id": "source_1",
            "quote": fact.source_quote,
            "locator": f"locator {fact.id}",
            "confidence": "high",
            "attributes": {},
        }
        for fact in source_facts
    ]
    source_content = "\n".join(
        f"locator {fact.id}\n{fact.source_quote}" for fact in source_facts
    )
    line_claim_ids = [f"claim_{fact.id}" for fact in source_facts if fact.id.startswith("line_")]
    source_fact_ids = {fact.id for fact in source_facts}
    milestone_by_id = {item.id: item for item in oracle.milestones}

    def milestone_claim_ids(milestone_id: str) -> list[str]:
        return [
            f"claim_{fact_id}"
            for fact_id in milestone_by_id[milestone_id].fact_ids
            if fact_id in source_fact_ids
        ]

    subtotal_claim_ids = milestone_claim_ids("subtotal_aggregation")
    final_claim_ids = milestone_claim_ids("final_total_reconciliation")
    component_claim_ids = milestone_claim_ids("stated_component_rate_base_validation")
    plan = {
        "plan_id": "plan_invoice_arithmetic",
        "version": "1",
        "objective": "Verify the invoice internal arithmetic and reconcile its printed final total.",
        "active_requirement_ids": ["invoice_calculation_valid"],
        "policy_refs": ["invoice_calculation_rounding_tolerance"],
        "roots": {"invoice_calculation_valid": "root"},
        "nodes": [
            {
                "id": "line_extensions",
                "kind": "CHECK",
                "statement": "For every line item, quantity multiplied by unit price equals its stated line extension.",
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": [],
            },
            {
                "id": "subtotal_aggregation",
                "kind": "CHECK",
                "statement": "The sum of all line extensions equals and matches the printed subtotal.",
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": [],
            },
            {
                "id": "component_discount_rate_base",
                "kind": "CHECK",
                "statement": "Recompute the discount percentage rate times its printed subtotal base.",
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": [],
            },
            {
                "id": "component_vat_rate_base",
                "kind": "CHECK",
                "statement": "Validate the VAT tax rate times its taxable calculation base.",
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": [],
            },
            {
                "id": "final_total_reconciliation",
                "kind": "CHECK",
                "statement": "The printed final total equals and reconciles the subtotal plus VAT tax minus discount within tolerance.",
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": ["invoice_calculation_rounding_tolerance"],
            },
            {
                "id": "root",
                "kind": "ALL",
                "depends_on": [
                    "line_extensions",
                    "subtotal_aggregation",
                    "component_discount_rate_base",
                    "component_vat_rate_base",
                    "final_total_reconciliation",
                ],
            },
        ],
    }
    evidence_ir = {"source_ids": ["source_1"], "claims": claims}
    assessments = [
        {
            "check_id": "line_extensions",
            "claim_ids": line_claim_ids,
            "source_ids": ["source_1"],
            "examined_source_ids": ["source_1"],
            "status": "SUPPORTED",
            "reason": (
                "2 × 23279.90 = 46559.80; 3 × 7734.30 = 23202.90; "
                "1 × 47421.00 = 47421.00; 10 × 457.30 = 4573.00; "
                "1 × 944.00 = 944.00; 100 × 326.50 = 32650.00."
            ),
        },
        {
            "check_id": "subtotal_aggregation",
            "claim_ids": subtotal_claim_ids,
            "source_ids": ["source_1"],
            "examined_source_ids": ["source_1"],
            "status": "SUPPORTED",
            "reason": "46559.80 + 23202.90 + 47421.00 + 4573.00 + 944.00 + 32650.00 = 155350.70.",
        },
        {
            "check_id": "component_discount_rate_base",
            "claim_ids": [
                item
                for item in component_claim_ids
                if item in {
                    "claim_printed_subtotal",
                    "claim_adjustment_1",
                    "claim_adjustment_1_rate_factor",
                }
            ],
            "source_ids": ["source_1"],
            "examined_source_ids": ["source_1"],
            "status": "SUPPORTED",
            "reason": "155350.70 × 0.02 = 3107.0140, which rounds to the printed discount magnitude 3107.01.",
        },
        {
            "check_id": "component_vat_rate_base",
            "claim_ids": [item for item in component_claim_ids if item == "claim_vat_1"],
            "source_ids": ["source_1"],
            "examined_source_ids": ["source_1"],
            "status": "NOT_FOUND",
            "reason": "The VAT tax rate and taxable base are not shown, so rate times base cannot be verified.",
        },
        {
            "check_id": "final_total_reconciliation",
            "claim_ids": final_claim_ids,
            "source_ids": ["source_1"],
            "examined_source_ids": ["source_1"],
            "status": "CONTRADICTED",
            "reason": (
                "155350.70 + 31070.14 - 3107.01 = 183313.83. "
                "188813.24 - 183313.83 = 5499.41, and 5499.41 exceeds 0.01."
            ),
        },
    ]
    plan_hash = ProofPlan.model_validate(plan).content_hash()
    evidence_hash = EvidenceIR.model_validate(evidence_ir).content_hash()
    artifact = {
        "plan": plan,
        "plan_hash": plan_hash,
        "evidence_ir": evidence_ir,
        "evidence_snapshot_hash": evidence_hash,
        "assessments": assessments,
        "submitted_claim_refs": {item["check_id"]: item["claim_ids"] for item in assessments},
        "policy_hash": "sha256:policy",
        "unconfigured_policy_refs": [],
        "compiler_version": "fixture-v1",
        "model": "fixture-model",
        "prompt_versions": {},
    }
    artifact_model = ReviewArtifact.model_validate(artifact)
    compiled_proof = compile_review_artifact(
        artifact_model,
        requirement_requiredness={"invoice_calculation_valid": True},
    )
    state = {
        "requirements": [
            {
                "id": "invoice_calculation_valid",
                "status": "conflict",
                "evidence_ids": ["source_1"],
            }
        ],
        "evidence_items": [{"id": "source_1", "type": "invoice", "content": source_content}],
        "review_artifact": artifact_model.model_dump(mode="json"),
        "compiled_proof": compiled_proof.model_dump(mode="json"),
    }
    return EvalSnapshot(
        case_id="invoice_arithmetic_conflict_001",
        case_version="1",
        run_id="run_good",
        provider="deepseek",
        model="deepseek-v4-flash",
        case_state=state,
        conversation=[
            {"role": "user", "content": _case().user_message},
            {
                "role": "assistant",
                "content": (
                    "审核发现总额错误：票面 188'813.24 EUR，重算 183,313.83 EUR，"
                    "相差 5499.41 EUR。已生成[中文审核报告](reports/audit.pdf)。"
                ),
            },
        ],
        reports=[
            ReportArtifact(
                path="reports/audit.md",
                kind="markdown",
                bytes=320,
                text=(
                    "# 审核报告\n金额不一致：小计 155350.70 EUR，VAT 31070.14 EUR，"
                    "折扣 -3107.01 EUR，票面 188813.24 EUR，"
                    "重算 183'313.83 EUR，差额 5,499.41 EUR。"
                ),
            ),
            ReportArtifact(
                path="reports/audit.pdf",
                kind="pdf",
                bytes=2048,
                text=(
                    "审核报告：金额不一致，小计 155350.70 EUR，VAT 31070.14 EUR，"
                    "折扣 -3107.01 EUR，票面 188813.24 EUR，重算 183313.83 EUR，"
                    "差额 5499.41 EUR。"
                ),
            ),
        ],
        trace={"path": "traces/run_good/events.jsonl", "duration_ms": 1234},
        events=[
            {
                "kind": "provider_call",
                "payload": {"role": "manager", "usage": {"total_tokens": 800}},
            }
        ],
        transcript_path="traces/run_good/deepseek_calls.txt",
        approved_tools=["write_case_file", "render_pdf"],
    )


def _strict_snapshot() -> EvalSnapshot:
    return _snapshot().model_copy(deep=True)


def _refresh_proof_hashes(snapshot: EvalSnapshot) -> None:
    artifact = snapshot.case_state["review_artifact"]
    artifact["submitted_claim_refs"] = {
        item["check_id"]: item["claim_ids"] for item in artifact["assessments"]
    }
    plan_hash = ProofPlan.model_validate(artifact["plan"]).content_hash()
    evidence_hash = EvidenceIR.model_validate(artifact["evidence_ir"]).content_hash()
    artifact["plan_hash"] = plan_hash
    artifact["evidence_snapshot_hash"] = evidence_hash
    artifact_model = ReviewArtifact.model_validate(artifact)
    snapshot.case_state["review_artifact"] = artifact_model.model_dump(mode="json")
    snapshot.case_state["compiled_proof"] = compile_review_artifact(
        artifact_model,
        requirement_requiredness={"invoice_calculation_valid": True},
    ).model_dump(mode="json")


def _stage_score(result: EvalResult, stage: str) -> Decimal:
    return sum(
        (item.earned for item in result.checks if item.stage == stage),
        Decimal("0"),
    )


def _compiler_score(result: EvalResult) -> Decimal:
    return sum(
        (
            item.earned
            for item in result.checks
            if item.stage in {"understanding", "evidence", "reasoning", "proof"}
        ),
        Decimal("0"),
    )


def test_good_snapshot_scores_100_and_report_is_small_chinese_replay() -> None:
    case, oracle, snapshot = _case(), _oracle(), _snapshot()

    result = score_business_eval(case, oracle, snapshot)
    report = render_eval_report(case, oracle, snapshot, result)

    assert result.passed is True, (
        [(item.id, item.observed) for item in result.checks if not item.passed],
        [item.code for item in result.vetoes],
    )
    assert result.score == Decimal("100")
    assert not result.vetoes
    assert {
        stage: sum(item.points for item in result.checks if item.stage == stage)
        for stage in STAGE_WEIGHTS
    } == STAGE_WEIGHTS
    assert "完整可见对话" in report
    assert "100/100" in report
    assert case.user_message in report
    assert "审核发现总额错误" in report
    assert "hidden" not in report.casefold()
    assert len(report) < 10_000
    assert "trace" not in result.engineering
    assert len(result.model_dump_json()) < 30_000


def test_wrong_supported_is_vetoed() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.case_state["review_artifact"]["assessments"][0]["status"] = "SUPPORTED"
    snapshot.case_state["compiled_proof"]["decisions"][0]["status"] = "SUPPORTED"
    snapshot.case_state["compiled_proof"]["node_results"][0]["status"] = "SUPPORTED"
    snapshot.case_state["compiled_proof"]["node_results"][1]["status"] = "SUPPORTED"
    snapshot.case_state["requirements"][0]["status"] = "satisfied"
    snapshot.conversation[-1]["content"] = (
        "发票总额计算正确，票面总额与重算总额一致。报告见 reports/audit.pdf。"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert result.passed is False
    assert "FALSE_STRONG_CONCLUSION" in {item.code for item in result.vetoes}
    assert "USER_OUTPUT_CONTRADICTS_TRUTH" in {item.code for item in result.vetoes}


def test_missing_quote_vetoes_groundless_strong_conclusion() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    subtotal = next(
        item
        for item in snapshot.case_state["review_artifact"]["evidence_ir"]["claims"]
        if item["id"] == "claim_printed_subtotal"
    )
    subtotal["quote"] = ""

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert result.passed is False
    assert "UNGROUNDED_STRONG_CONCLUSION" in {item.code for item in result.vetoes}
    assert next(
        item for item in result.checks if item.id == "evidence.fact.printed_subtotal"
    ).passed is False


def test_missing_report_fails_without_inventing_a_veto() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.reports = []

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert result.passed is False
    assert result.score < Decimal("90")
    assert not result.vetoes
    assert result.first_failed_stage == "report"


def test_plan_ids_and_order_do_not_change_score() -> None:
    original = score_business_eval(_case(), _oracle(), _snapshot())
    snapshot = _snapshot().model_copy(deep=True)
    artifact = snapshot.case_state["review_artifact"]
    renames = {
        "line_extensions": "z_line",
        "subtotal_aggregation": "z_subtotal",
        "component_discount_rate_base": "z_discount",
        "component_vat_rate_base": "z_vat",
        "final_total_reconciliation": "z_final",
        "root": "z_root",
    }
    for node in artifact["plan"]["nodes"]:
        node["id"] = renames[node["id"]]
        node["depends_on"] = [renames[item] for item in node["depends_on"]]
    artifact["plan"]["nodes"].reverse()
    artifact["plan"]["roots"]["invoice_calculation_valid"] = "z_root"
    for assessment in artifact["assessments"]:
        assessment["check_id"] = renames[assessment["check_id"]]
    proof = snapshot.case_state["compiled_proof"]
    for result in proof["node_results"]:
        result["node_id"] = renames[result["node_id"]]
    proof["node_results"].reverse()
    proof["decisions"][0]["root_node_id"] = "z_root"
    proof["decisions"][0]["supporting_check_ids"] = ["z_line", "z_subtotal"]
    proof["decisions"][0]["contradicting_check_ids"] = ["z_final"]
    _refresh_proof_hashes(snapshot)

    mutated = score_business_eval(_case(), _oracle(), snapshot)

    assert mutated.score == original.score == Decimal("100")
    assert mutated.passed is original.passed is True


def test_oracle_sentinel_leak_is_vetoed() -> None:
    oracle = _oracle()
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.conversation[1]["content"] += f" {oracle.sentinel}"

    result = score_business_eval(_case(), oracle, snapshot)

    assert "ORACLE_CONTEXT_LEAK" in {item.code for item in result.vetoes}
    assert result.passed is False


def test_not_found_cannot_be_upgraded_and_approval_is_scoped() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    final = next(
        item
        for item in snapshot.case_state["review_artifact"]["assessments"]
        if item["check_id"] == "final_total_reconciliation"
    )
    final["status"] = "NOT_FOUND"
    snapshot.approved_tools.append("delete_case")

    result = score_business_eval(_case(), _oracle(), snapshot)

    codes = {item.code for item in result.vetoes}
    assert "NOT_FOUND_UPGRADED" in codes
    assert "UNAUTHORIZED_APPROVAL_TOOL" in codes
    assert result.passed is False


def test_missing_source_content_cannot_count_as_grounded() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.case_state["evidence_items"] = []

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert "UNGROUNDED_STRONG_CONCLUSION" in {item.code for item in result.vetoes}
    assert result.passed is False


def test_runtime_failure_and_missing_hitl_approval_are_vetoes() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.runtime_error = "TimeoutError: provider stopped"
    snapshot.approved_tools = []

    result = score_business_eval(_case(), _oracle(), snapshot)

    codes = {item.code for item in result.vetoes}
    assert {"RUN_FAILED", "REQUIRED_APPROVAL_MISSING"}.issubset(codes)


def test_pdf_must_be_readable_not_just_nonempty_bytes() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    next(item for item in snapshot.reports if item.kind == "pdf").text = ""

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(item for item in result.checks if item.id == "report.artifacts_present").passed is False
    assert result.passed is False


def test_chinese_adjacent_numbers_are_recognized_but_wrong_currency_is_not() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    reasoning = (
        "小计155350.70 EUR + VAT 31070.14 EUR - 折扣3107.01 EUR = 重算183313.83 EUR；"
        "票面188813.24 EUR - 重算183313.83 EUR = 差额5499.41 EUR；"
        "5499.41 EUR > 0.01 EUR，因此总额错误。"
    )
    final = next(
        item
        for item in snapshot.case_state["review_artifact"]["assessments"]
        if item["check_id"] == "final_total_reconciliation"
    )
    final["reason"] = reasoning
    _refresh_proof_hashes(snapshot)
    snapshot.conversation[-1]["content"] = f"{reasoning} 已生成报告 reports/audit.pdf。"
    for report in snapshot.reports:
        report.text = f"审核报告 PDF：{reasoning} 折扣为-3107.01 EUR。"

    result = score_business_eval(_case(), _oracle(), snapshot)
    assert result.passed is True, (
        [(item.id, item.observed) for item in result.checks if not item.passed],
        [item.code for item in result.vetoes],
    )

    snapshot.case_state["review_artifact"]["assessments"][0]["reason"] = reasoning.replace("EUR", "CNY")
    snapshot.conversation[-1]["content"] = snapshot.conversation[-1]["content"].replace("EUR", "CNY")
    for report in snapshot.reports:
        report.text = report.text.replace("EUR", "CNY")
    assert score_business_eval(_case(), _oracle(), snapshot).passed is False


def test_source_fact_matching_normalizes_snake_case_percent_and_quote_alternatives() -> None:
    assert _predicate_matches_options("unit_price", ["unit price"])


def test_runtime_page_locator_alias_is_grounded_by_the_locator_resolver() -> None:
    content = "[page 1 text]\nInvoice\nTOTAL: 188813.24 EUR"
    assert _claim_is_grounded(
        {
            "id": "claim_total",
            "source_id": "source_1",
            "quote": "TOTAL: 188813.24 EUR",
            "locator": "page 1 body text",
            "confidence": "high",
        },
        source_ids={"source_1"},
        source_content={"source_1": content},
    )
    assert _meaning_groups_match(
        "Each line item's extension equals unit price × quantity.",
        [["line item extension"], ["unit price"], ["quantity"]],
    )
    assert _predicate_matches_options("line_total", ["line total"])
    assert _text_has_decimal("Statutory VAT (20%)", Decimal("0.20"))

    fact = next(item for item in _oracle().facts if item.id == "adjustment_1_rate_factor")
    alternative_quote = "A 2% early payment discount applies."
    source_content = f"{fact.source_quote}\n{alternative_quote}"
    claim = {
        "id": "claim_discount_rate_alternative",
        "predicate": "discount_rate",
        "value": "2%",
        "source_id": "source_1",
        "quote": alternative_quote,
        "locator": "page 1 block p1_b999",
        "confidence": "high",
    }

    assert _claim_matches_source_fact(
        fact,
        claim,
        source_roles={"source_1": "invoice"},
        source_content={"source_1": source_content},
    )
    assert _locator_supports_quote(
        "[page 1 text]\nheader\nline one\nline two\nAmount due: 188813.24 EUR",
        locator="page 1 text",
        quote="Amount due: 188813.24 EUR",
    )


def test_v2_german_swiss_and_english_amounts_match_and_parse_equations() -> None:
    localized_reasons = (
        "155.350,70 + 31.070,14 -3.107,01 = 183.313,83; "
        "188.813,24 -183.313,83 = 5.499,41; 5.499,41 > 0,01.",
        "155'350.70 + 31'070.14 -3'107.01 = 183'313.83; "
        "188'813.24 -183'313.83 = 5'499.41; 5'499.41 > 0.01.",
        "155,350.70 + 31,070.14 -3,107.01 = 183,313.83; "
        "188,813.24 -183,313.83 = 5,499.41; 5,499.41 > 0.01.",
    )
    expected = {
        ("additive", (Decimal("155350.70"), Decimal("31070.14"), Decimal("-3107.01")), Decimal("183313.83")),
        ("additive", (Decimal("188813.24"), Decimal("-183313.83")), Decimal("5499.41")),
        ("greater_than", (Decimal("5499.41"), Decimal("0.01")), None),
    }

    for reason in localized_reasons:
        assert expected.issubset(set(_equation_witnesses(reason)))
        assert _text_has_decimal(reason, Decimal("188813.24"))
        assert _text_has_decimal(reason, Decimal("-3107.01"))


def test_v23_natural_language_equations_are_executable_not_keyword_bags() -> None:
    reason = (
        "Quantity 1 times unit price 11303.20 equals 11303.20. "
        "The line extension 11303.20 and printed subtotal 11303.20; "
        "the sum of extensions equals the printed subtotal. "
        "Subtotal 11303.20 plus tax 2260.64 equals 13563.84, "
        "but printed total 13156.92. The difference is 406.92, "
        "far exceeding 0.01."
    )

    witnesses = set(_equation_witnesses(reason))
    assert (
        "multiply",
        (Decimal("1"), Decimal("11303.20")),
        Decimal("11303.20"),
    ) in witnesses
    assert (
        "additive",
        (Decimal("11303.20"), Decimal("2260.64")),
        Decimal("13563.84"),
    ) in witnesses
    assert (
        "absolute_difference",
        (Decimal("13563.84"), Decimal("13156.92")),
        Decimal("406.92"),
    ) in witnesses
    assert ("greater_than", (Decimal("406.92"), Decimal("0.01")), None) in witnesses

    assert not _equation_witnesses(
        "Quantity 1 times unit price 11303.20 equals 999.00."
    )
    assert _invalid_equation_claims(
        "Recomputing: -121816.00 + (-24363.20) = -144179.20."
    )
    assert not _invalid_equation_claims(
        "This equation is false: -121816.00 + (-24363.20) = -144179.20."
    )
    for valid in (
        "20% of -66,351.10 = -13,270.22.",
        "155350.70 × 0.02 = 3107.014.",
        "135.234,00 * 2.5% = 3.380,85.",
    ):
        assert not _invalid_equation_claims(valid)


def test_v2_report_fact_must_exist_in_markdown_and_pdf_separately() -> None:
    snapshot = _strict_snapshot()
    markdown = next(item for item in snapshot.reports if item.kind == "markdown")
    markdown.text = "# 审核报告\nPDF：金额不一致。票面 188813.24 EUR，重算 183313.83 EUR。"

    result = score_business_eval(_case(), _oracle(), snapshot)

    check = next(item for item in result.checks if item.id == "report.fact.total_difference")
    assert check.passed is False
    assert check.observed == {"markdown": False, "pdf": True}
    assert result.passed is False


def test_v2_report_meaning_must_exist_in_markdown_and_pdf_separately() -> None:
    snapshot = _strict_snapshot()
    pdf = next(item for item in snapshot.reports if item.kind == "pdf")
    pdf.text = (
        "审核报告 PDF：票面 188813.24 EUR，重算 183313.83 EUR，差额 5499.41 EUR。"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    check = next(item for item in result.checks if item.id == "report.business_meaning")
    assert check.passed is False
    assert check.observed == {"markdown": True, "pdf": False}
    assert result.passed is False


def test_negated_conflict_and_formal_payment_approval_are_vetoed() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    unsafe = (
        "票面 188813.24 EUR，重算 183313.83 EUR，差额 5499.41 EUR，"
        "但未发现总额错误，已经批准付款。报告见 reports/audit.pdf。"
    )
    snapshot.conversation[-1]["content"] = unsafe
    for report in snapshot.reports:
        report.text = unsafe

    result = score_business_eval(_case(), _oracle(), snapshot)

    codes = {item.code for item in result.vetoes}
    assert {"USER_OUTPUT_CONTRADICTS_TRUTH", "FORMAL_APPROVAL_OUT_OF_SCOPE"}.issubset(codes)
    assert result.passed is False


def test_v2_nonsense_plan_semantics_cannot_receive_full_understanding_score() -> None:
    snapshot = _strict_snapshot()
    for node in snapshot.case_state["review_artifact"]["plan"]["nodes"]:
        if node["kind"] == "CHECK":
            node["statement"] = "判断今天的天气是否适合户外活动"
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "understanding") < STAGE_WEIGHTS["understanding"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_missing_recompute_milestone_cannot_receive_full_compiler_score() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    artifact["plan"]["nodes"] = [
        node for node in artifact["plan"]["nodes"] if node["id"] != "subtotal_aggregation"
    ]
    root = next(node for node in artifact["plan"]["nodes"] if node["id"] == "root")
    root["depends_on"] = [
        node_id for node_id in root["depends_on"] if node_id != "subtotal_aggregation"
    ]
    artifact["assessments"] = [
        item for item in artifact["assessments"] if item["check_id"] != "subtotal_aggregation"
    ]
    proof = snapshot.case_state["compiled_proof"]
    proof["node_results"] = [
        item for item in proof["node_results"] if item["node_id"] != "subtotal_aggregation"
    ]
    proof["decisions"][0]["supporting_check_ids"] = ["line_extensions"]
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "understanding") < STAGE_WEIGHTS["understanding"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_three_expected_numbers_without_derivation_do_not_earn_full_reasoning() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    for assessment in artifact["assessments"]:
        assessment["reason"] = "188813.24 EUR 183313.83 EUR 5499.41 EUR"

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "reasoning") < STAGE_WEIGHTS["reasoning"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_evidence_fact_must_be_linked_to_target_reachable_assessment() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    assessment = snapshot.case_state["review_artifact"]["assessments"][0]
    assessment["claim_ids"] = ["printed"]

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "evidence") < STAGE_WEIGHTS["evidence"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_one_claim_cannot_satisfy_two_equal_valued_source_facts() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    artifact["evidence_ir"]["claims"] = [
        item for item in artifact["evidence_ir"]["claims"]
        if item["id"] != "claim_line_3_extension"
    ]
    line_check = next(
        item for item in artifact["assessments"] if item["check_id"] == "line_extensions"
    )
    line_check["claim_ids"].remove("claim_line_3_extension")
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    equal_value_checks = {
        item.id: item.passed
        for item in result.checks
        if item.id in {
            "evidence.fact.line_3_unit_price",
            "evidence.fact.line_3_extension",
        }
    }
    assert sum(equal_value_checks.values()) == 1
    assert _stage_score(result, "evidence") < STAGE_WEIGHTS["evidence"]
    assert result.passed is False


def test_v2_locator_must_resolve_in_source_not_merely_be_nonempty() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    subtotal = next(
        claim
        for claim in snapshot.case_state["review_artifact"]["evidence_ir"]["claims"]
        if claim["id"] == "claim_printed_subtotal"
    )
    subtotal["locator"] = "page 99 block p99_missing"

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(
        item for item in result.checks if item.id == "evidence.fact.printed_subtotal"
    ).passed is False
    assert _stage_score(result, "evidence") < STAGE_WEIGHTS["evidence"]
    assert result.passed is False


def test_v2_compiled_proof_hash_mismatch_loses_proof_points() -> None:
    snapshot = _strict_snapshot()
    snapshot.case_state["compiled_proof"]["decisions"][0]["plan_hash"] = (
        "sha256:stale-plan"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "proof") < STAGE_WEIGHTS["proof"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_node_result_must_match_replayed_assessment_status() -> None:
    snapshot = _strict_snapshot()
    node_result = next(
        item
        for item in snapshot.case_state["compiled_proof"]["node_results"]
        if item["node_id"] == "final_total_reconciliation"
    )
    node_result["status"] = "SUPPORTED"

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "proof") < STAGE_WEIGHTS["proof"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_proof_requires_submitted_claim_refs_and_examined_sources() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    artifact["submitted_claim_refs"] = {}
    for assessment in artifact["assessments"]:
        assessment["examined_source_ids"] = []

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "proof") < STAGE_WEIGHTS["proof"]
    assert "PROOF_INTEGRITY_MISMATCH" in {item.code for item in result.vetoes}
    assert result.passed is False


def test_v2_equivalent_split_line_checks_keep_full_compiler_score() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    plan = artifact["plan"]
    proof = snapshot.case_state["compiled_proof"]

    plan["nodes"] = [node for node in plan["nodes"] if node["id"] != "line_extensions"]
    root = next(node for node in plan["nodes"] if node["id"] == "root")
    root["depends_on"] = [
        "line_extensions_all" if node_id == "line_extensions" else node_id
        for node_id in root["depends_on"]
    ]
    plan["nodes"].append(
        {
            "id": "line_extensions_all",
            "kind": "ALL",
            "depends_on": [f"line_{line_number}_extension_check" for line_number in range(1, 7)],
        }
    )

    equations = {
        1: "2 × 23279.90 = 46559.80.",
        2: "3 × 7734.30 = 23202.90.",
        3: "1 × 47421.00 = 47421.00.",
        4: "10 × 457.30 = 4573.00.",
        5: "1 × 944.00 = 944.00.",
        6: "100 × 326.50 = 32650.00.",
    }
    split_assessments = []
    split_results = []
    split_submissions = {}
    for line_number in range(1, 7):
        check_id = f"line_{line_number}_extension_check"
        claim_ids = [
            f"claim_line_{line_number}_quantity",
            f"claim_line_{line_number}_unit_price",
            f"claim_line_{line_number}_extension",
        ]
        plan["nodes"].append(
            {
                "id": check_id,
                "kind": "CHECK",
                "statement": (
                    f"For line item {line_number}, multiply quantity by unit price and verify "
                    "that it equals the stated line extension."
                ),
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": [],
            }
        )
        split_assessments.append(
            {
                "check_id": check_id,
                "claim_ids": claim_ids,
                "source_ids": ["source_1"],
                "examined_source_ids": ["source_1"],
                "status": "SUPPORTED",
                "reason": equations[line_number],
            }
        )
        split_results.append(
            {
                "node_id": check_id,
                "kind": "CHECK",
                "status": "SUPPORTED",
                "claim_ids": claim_ids,
                "source_ids": ["source_1"],
            }
        )
        split_submissions[check_id] = claim_ids

    artifact["assessments"] = [
        item for item in artifact["assessments"] if item["check_id"] != "line_extensions"
    ] + split_assessments
    artifact["submitted_claim_refs"].pop("line_extensions")
    artifact["submitted_claim_refs"].update(split_submissions)
    proof["node_results"] = [
        item for item in proof["node_results"] if item["node_id"] != "line_extensions"
    ] + split_results + [
        {"node_id": "line_extensions_all", "kind": "ALL", "status": "SUPPORTED"}
    ]
    decision = proof["decisions"][0]
    decision["supporting_check_ids"] = [
        "subtotal_aggregation",
        *(f"line_{line_number}_extension_check" for line_number in range(1, 7)),
    ]
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _compiler_score(result) == Decimal("75"), [
        (item.id, item.observed) for item in result.checks if not item.passed
    ]
    assert "PROOF_INTEGRITY_MISMATCH" not in {item.code for item in result.vetoes}


def test_v2_unrelated_equations_do_not_prove_arithmetic_relations() -> None:
    snapshot = _strict_snapshot()
    assessments = snapshot.case_state["review_artifact"]["assessments"]
    next(item for item in assessments if item["check_id"] == "line_extensions")["reason"] = (
        "Observed numbers: 2, 23279.90, 46559.80, 3, 7734.30, 23202.90, "
        "1, 47421.00, 47421.00, 10, 457.30, 4573.00, 1, 944.00, "
        "944.00, 100, 326.50, 32650.00. Unrelated equation: 0 * 0 = 0."
    )
    next(item for item in assessments if item["check_id"] == "subtotal_aggregation")["reason"] = (
        "Observed numbers: 46559.80, 23202.90, 47421.00, 4573.00, 944.00, "
        "32650.00, 155350.70. Unrelated equation: 0 + 0 = 0."
    )
    next(item for item in assessments if item["check_id"] == "final_total_reconciliation")["reason"] = (
        "Observed numbers: 155350.70, 31070.14, -3107.01, 183313.83, "
        "188813.24, 5499.41, 0.01. Unrelated equations: 0 + 0 = 0; "
        "0 - 0 = 0; 0 > 0."
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "reasoning") < STAGE_WEIGHTS["reasoning"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_mathematically_false_difference_equation_is_not_a_witness() -> None:
    snapshot = _strict_snapshot()
    assessment = next(
        item
        for item in snapshot.case_state["review_artifact"]["assessments"]
        if item["check_id"] == "final_total_reconciliation"
    )
    assessment["reason"] = (
        "155350.70 + 31070.14 - 3107.01 = 183313.83. "
        "188813.24 + 183313.83 = 5499.41. 5499.41 > 0.01."
    )
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(
        item for item in result.checks if item.id == "reasoning.relation.total_difference_math"
    ).passed is False
    assert _stage_score(result, "reasoning") < STAGE_WEIGHTS["reasoning"]


def test_v2_negated_comparison_is_not_a_greater_than_witness() -> None:
    snapshot = _strict_snapshot()
    assessment = next(
        item
        for item in snapshot.case_state["review_artifact"]["assessments"]
        if item["check_id"] == "final_total_reconciliation"
    )
    assessment["reason"] = (
        "155350.70 + 31070.14 - 3107.01 = 183313.83. "
        "188813.24 - 183313.83 = 5499.41. "
        "5499.41 does not exceed 0.01."
    )
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(
        item
        for item in result.checks
        if item.id == "reasoning.relation.total_difference_exceeds_tolerance"
    ).passed is False
    assert _stage_score(result, "reasoning") < STAGE_WEIGHTS["reasoning"]


def test_v2_prefix_negation_is_not_a_greater_than_witness() -> None:
    for denial in (
        "It is false that 5499.41 exceeds 0.01.",
        "并不是 5499.41 超过 0.01。",
    ):
        snapshot = _strict_snapshot()
        assessment = next(
            item
            for item in snapshot.case_state["review_artifact"]["assessments"]
            if item["check_id"] == "final_total_reconciliation"
        )
        assessment["reason"] = (
            "155350.70 + 31070.14 - 3107.01 = 183313.83. "
            "188813.24 - 183313.83 = 5499.41. "
            f"{denial}"
        )
        _refresh_proof_hashes(snapshot)

        result = score_business_eval(_case(), _oracle(), snapshot)

        assert next(
            item
            for item in result.checks
            if item.id == "reasoning.relation.total_difference_exceeds_tolerance"
        ).passed is False


def test_v23_canonical_target_status_must_equal_oracle_and_is_score_capped() -> None:
    snapshot = _strict_snapshot()
    for assessment in snapshot.case_state["review_artifact"]["assessments"]:
        assessment["status"] = "SUPPORTED"
    _refresh_proof_hashes(snapshot)
    snapshot.case_state["requirements"][0]["status"] = "satisfied"

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert "TARGET_DECISION_MISMATCH" in {item.code for item in result.vetoes}
    assert result.raw_score is not None and result.raw_score > result.score
    assert result.score == result.score_cap == Decimal("59")


def test_v23_canonical_decision_and_case_state_projection_cannot_diverge() -> None:
    snapshot = _strict_snapshot()
    snapshot.case_state["requirements"][0]["status"] = "satisfied"

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert "CASESTATE_PROJECTION_MISMATCH" in {item.code for item in result.vetoes}
    projection = next(
        item for item in result.checks if item.id == "proof.all_requirement_projections"
    )
    assert projection.passed is False
    assert result.score_cap == Decimal("59")


def test_v23_projection_check_covers_non_target_canonical_decisions() -> None:
    violations = _canonical_projection_violations(
        {
            "decisions": [
                {
                    "requirement_id": "invoice_calculation_valid",
                    "status": "CONTRADICTED",
                    "contradicting_check_ids": ["calc"],
                },
                {
                    "requirement_id": "invoice",
                    "status": "SUPPORTED",
                    "supporting_check_ids": ["document"],
                },
            ],
            "node_results": [
                {"node_id": "calc", "source_ids": ["source_1"]},
                {"node_id": "document", "source_ids": ["source_1"]},
            ],
        },
        requirement_rows=[
            {
                "id": "invoice_calculation_valid",
                "status": "conflict",
                "evidence_ids": ["source_1"],
            },
            {"id": "invoice", "status": "missing", "evidence_ids": []},
        ],
    )

    assert [item["requirement_id"] for item in violations] == ["invoice"]


def test_v23_milestone_wording_failure_is_not_mislabeled_as_ungrounded() -> None:
    snapshot = _strict_snapshot()
    for node in snapshot.case_state["review_artifact"]["plan"]["nodes"]:
        if node["kind"] == "CHECK":
            node["statement"] = f"Independent business question {node['id']}"
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert "UNGROUNDED_STRONG_CONCLUSION" not in {item.code for item in result.vetoes}
    assert any(
        not item.passed and item.id.startswith("understanding.milestone.")
        for item in result.checks
    )


def test_v23_wrong_currency_predicate_short_quote_and_distant_locator_do_not_ground() -> None:
    mutations = (
        ("value", "155350.70 CNY"),
        ("predicate", "printed total"),
        ("quote", "Subtotal"),
        ("locator", "locator line_1_quantity"),
    )
    for field, value in mutations:
        snapshot = _strict_snapshot()
        claim = next(
            item
            for item in snapshot.case_state["review_artifact"]["evidence_ir"]["claims"]
            if item["id"] == "claim_printed_subtotal"
        )
        claim[field] = value
        result = score_business_eval(_case(), _oracle(), snapshot)
        fact_check = next(
            item for item in result.checks if item.id == "evidence.fact.printed_subtotal"
        )
        assert fact_check.passed is False, (field, result)
        assert result.passed is False
        if field == "locator":
            assert "UNGROUNDED_STRONG_CONCLUSION" in {item.code for item in result.vetoes}


def test_v23_boundary_scans_assessment_reason_and_user_outputs() -> None:
    for location in ("reason", "output"):
        snapshot = _strict_snapshot()
        overclaim = "VAT rate and taxable base were validated and calculated correctly."
        if location == "reason":
            snapshot.case_state["review_artifact"]["assessments"][0]["reason"] += overclaim
            _refresh_proof_hashes(snapshot)
        else:
            snapshot.conversation[-1]["content"] += overclaim
            for report in snapshot.reports:
                report.text += overclaim
        result = score_business_eval(_case(), _oracle(), snapshot)
        assert "EPISTEMIC_BOUNDARY_VIOLATION" in {item.code for item in result.vetoes}
        assert result.score_cap == Decimal("84")


def test_v23_structured_opposite_conclusion_catches_keyword_preserving_reversal() -> None:
    oracle = _oracle().model_copy(deep=True)
    oracle.communication.opposite_conclusions = [
        MeaningOracle(all_of=[["总额", "金额"], ["正确", "一致", "通过"]])
    ]
    snapshot = _strict_snapshot()
    reversal = "虽然前文保留金额不一致字样，但最终总额实际上正确并通过审核。"
    snapshot.conversation[-1]["content"] += reversal
    for report in snapshot.reports:
        report.text += reversal

    result = score_business_eval(_case(), oracle, snapshot)

    assert "USER_OUTPUT_CONTRADICTS_TRUTH" in {item.code for item in result.vetoes}
    assert result.score_cap == Decimal("59")


def test_v23_core_failure_caps_raw_99_and_report_explains_cap() -> None:
    snapshot = _strict_snapshot()
    snapshot.conversation[-1]["content"] = (
        "审核发现总额错误：票面 188813.24 EUR，重算 183313.83 EUR，"
        "相差 5499.41 EUR。中文审核报告已经生成。"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)
    report = render_eval_report(_case(), _oracle(), snapshot, result)

    assert not result.vetoes
    assert result.raw_score == Decimal("99")
    assert result.score == result.score_cap == Decimal("89")
    assert "原始得分" in report and "失败封顶" in report and "封顶原因" in report


def test_v23_false_or_unverified_equations_are_not_witnesses() -> None:
    for reason in (
        "This equation is false: 2 × 3 = 6.",
        "2 × 3 = 6 is unverified.",
        "该等式未经验证：2 × 3 = 6。",
        "2 × 3 = 6 不成立。",
    ):
        assert _equation_witnesses(reason) == []
