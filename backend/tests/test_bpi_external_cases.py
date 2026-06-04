from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = PROJECT_ROOT / "samples" / "cases" / "bpi_p2p_external_cases" / "cases.json"

EXPECTED_DISTRIBUTION = {
    "three_way_normal": 8,
    "invoice_before_gr": 8,
    "two_way_match": 7,
    "consignment": 7,
    "clear_invoice_trap": 5,
    "amount_variation_partial": 5,
    "reversal_cancellation_block": 5,
    "multi_turn_credibility": 5,
}

REQUIRED_GOLD_FIELDS = {
    "expected_planner_routes",
    "expected_evidence_classification",
    "expected_credibility",
    "expected_evidence_type",
    "expected_match_type",
    "expected_risks",
    "expected_missing_materials",
    "expected_case_state_after_turns",
    "report_must_include",
    "forbidden_claims",
    "scoring_layers",
}

REQUIRED_SCORING_LAYERS = {
    "planner_route",
    "evidence_review",
    "case_memory",
    "risk_explanation",
    "report_quality",
}

REQUIRED_ROUTE_MARKERS = {
    "write_case_patch",
    "call_role:materials_advisor",
    "call_role:evidence_reviewer",
    "call_role:case_patch_writer",
    "call_role:report_writer",
    "call_tool:write_case_file",
}


def test_bpi_external_case_suite_shape_and_distribution() -> None:
    cases = _load_cases()

    assert len(cases) == 50
    assert Counter(case["category"] for case in cases) == EXPECTED_DISTRIBUTION
    assert len({case["case_id"] for case in cases}) == 50
    assert [case["case_id"] for case in cases][:3] == ["INV-BPI-001", "INV-BPI-002", "INV-BPI-003"]


def test_each_case_marks_planner_route_gold_and_five_layer_scoring() -> None:
    for case in _load_cases():
        gold = case["gold"]

        assert REQUIRED_GOLD_FIELDS <= set(gold)
        assert REQUIRED_SCORING_LAYERS == set(gold["scoring_layers"])
        assert REQUIRED_ROUTE_MARKERS <= set(gold["expected_planner_routes"])
        assert case["turns"][0]["expected_planner_action"] == "write_case_patch"
        assert "materials_advisor" in case["turns"][1]["expected_planner_action"]
        assert "evidence_reviewer" in " ".join(turn["expected_planner_action"] for turn in case["turns"])
        assert "report_writer" in case["turns"][-1]["expected_planner_action"]


def test_bpi_packets_are_supporting_process_evidence_not_approval_material() -> None:
    for case in _load_cases():
        packets = case["evidence_packets"]
        process_packets = [packet for packet in packets if packet["type"] == "process_log"]
        source_alignment = case["source_alignment"]

        assert process_packets, case["case_id"]
        assert source_alignment["boundary"].startswith("compact derived fixture only")
        assert source_alignment["source_case_ids"]
        assert source_alignment["github_compact_sample_cases"].endswith("bpi2019_sample_cases.json")
        for packet in process_packets:
            content = packet["content"]
            assert content["case_id"] in source_alignment["source_case_ids"]
            assert "Clear Invoice is an event-log activity" in " ".join(content.get("notes", []))
            assert "no raw CSV row included" in packet["source"]
        forbidden = " ".join(case["gold"]["forbidden_claims"]).lower()
        assert "erp action executed" in forbidden
        assert "invoice paid by this agent" in forbidden


def test_category_specific_gold_boundaries() -> None:
    by_category = _by_category()

    for case in by_category["invoice_before_gr"]:
        gold = case["gold"]
        assert gold["expected_match_type"] == "three_way_invoice_before_gr"
        assert "invoice_before_goods_receipt" in gold["expected_risks"]
        assert _contains(gold["report_must_include"], "invoice before goods receipt")
        assert "policy_exception_or_business_explanation" in gold["expected_missing_materials"]

    for case in by_category["two_way_match"]:
        gold = case["gold"]
        assert gold["expected_match_type"] == "two_way_policy_basis_required"
        assert _contains(gold["report_must_include"], "2-way match requires policy basis")
        assert "goods_receipt" not in gold["expected_missing_materials"]

    for case in by_category["consignment"]:
        gold = case["gold"]
        assert gold["expected_match_type"] == "consignment_settlement"
        assert "consignment_requires_special_settlement_basis" in gold["expected_risks"]
        assert _contains(gold["report_must_include"], "do not apply ordinary 3-way")

    for case in by_category["clear_invoice_trap"]:
        gold = case["gold"]
        assert "clear_invoice_overclaim_risk" in gold["expected_risks"]
        assert _contains(gold["report_must_include"], "Clear Invoice is historical only")
        assert _contains(gold["forbidden_claims"], "BPI Clear Invoice")


def test_amount_reversal_and_multi_turn_cases_are_actionable() -> None:
    by_category = _by_category()

    for case in by_category["amount_variation_partial"]:
        process = _first_process_packet(case)
        values = process["content"]["cumulative_net_worth_values"]
        assert len(set(values)) >= 2
        assert "amount_reconciliation_required" in case["gold"]["expected_risks"]
        assert "invoice_line_reconciliation" in case["gold"]["expected_missing_materials"]
        assert _contains(case["gold"]["report_must_include"], "cumulative net worth")

    for case in by_category["reversal_cancellation_block"]:
        activities = [event["activity"].lower() for event in _first_process_packet(case)["content"]["events"]]
        assert any("reverse" in activity or "cancel" in activity for activity in activities)
        assert any("block" in activity for activity in activities)
        assert {"reversal_or_cancellation", "payment_block"} <= set(case["gold"]["expected_risks"])
        assert _contains(case["gold"]["report_must_include"], "do not approve-style memo")

    for case in by_category["multi_turn_credibility"]:
        assert len(case["turns"]) == 6
        classification = case["gold"]["expected_evidence_classification"]
        credibility = case["gold"]["expected_credibility"]
        assert "user_statement" in classification.values()
        user_statement_ids = [evidence_id for evidence_id, kind in classification.items() if kind == "user_statement"]
        assert user_statement_ids
        assert all(credibility[evidence_id] == "low" for evidence_id in user_statement_ids)
        assert case["gold"]["expected_case_state_after_turns"]["status"] == "collecting_materials"


def _load_cases() -> list[dict[str, Any]]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _by_category() -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {category: [] for category in EXPECTED_DISTRIBUTION}
    for case in _load_cases():
        grouped[case["category"]].append(case)
    return grouped


def _first_process_packet(case: dict[str, Any]) -> dict[str, Any]:
    return next(packet for packet in case["evidence_packets"] if packet["type"] == "process_log")


def _contains(values: list[str], needle: str) -> bool:
    return any(needle.lower() in value.lower() for value in values)
