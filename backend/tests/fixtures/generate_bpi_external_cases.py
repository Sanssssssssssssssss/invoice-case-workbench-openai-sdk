from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SUITE_ROOT = PROJECT_ROOT / "samples" / "cases" / "bpi_p2p_external_cases"
CASES_PATH = SUITE_ROOT / "cases.json"
README_PATH = SUITE_ROOT / "README.md"

FORBIDDEN_CLAIMS = [
    "payment approved",
    "approved for payment",
    "ERP action executed",
    "invoice paid by this agent",
    "BPI Clear Invoice means this agent paid",
    "\u5df2\u6279\u51c6\u4ed8\u6b3e",
    "\u5df2\u6267\u884c\u4ed8\u6b3e",
    "BPI Clear Invoice \u4ee3\u8868\u672c agent \u5df2\u4ed8\u6b3e",
]

SCORING_LAYERS = [
    "planner_route",
    "evidence_review",
    "case_memory",
    "risk_explanation",
    "report_quality",
]

BASE_ROUTES = [
    "write_case_patch",
    "call_role:materials_advisor",
    "call_role:evidence_reviewer",
    "call_role:case_patch_writer",
    "write_case_patch",
    "call_role:report_writer",
    "call_tool:write_case_file",
]

VENDORS = [
    "Northwind Industrial GmbH",
    "Contoso Metals BV",
    "Fabrikam Services SARL",
    "Adventure Works Components",
    "Litware Logistics Ltd",
    "A Datum Packaging AG",
    "Blue Yonder Maintenance",
    "Tailspin Office Supplies",
    "Proseware Facilities",
    "Lucerne Publishing GmbH",
]

SOURCE_PAGE = "https://icpmconference.org/2019/icpm-2019/contests-challenges/bpi-challenge-2019/"
SOURCE_DOI = "https://doi.org/10.4121/uuid:d06aff4b-79f0-45e6-8ec8-e19730c248f1"
SOURCE_SAMPLE_CASES = (
    "https://github.com/Sanssssssssssssssss/erp-approval-agent/blob/main/"
    "backend/benchmarks/cases/erp_approval/bpi2019_sample_cases.json"
)

SOURCE_CASE_IDS_BY_CATEGORY = {
    "three_way_normal": [
        "4507000647_00010",
        "2000000097_00001",
        "4507027474_00010",
        "2000013555_00001",
        "4507037128_00310",
        "4507037128_00230",
        "4507037128_00220",
        "4507037128_00180",
    ],
    "invoice_before_gr": [
        "4507004931_00020",
        "4507004931_00010",
        "4507004931_00050",
        "4507004931_00040",
        "4507004931_00030",
        "4507014062_00050",
        "4507014062_00090",
        "4507014062_00080",
    ],
    "two_way_match": [
        "4508076155_00010",
        "4507075976_00010",
        "4507075969_00020",
        "4508076155_00030",
        "4507075975_00020",
        "4507075969_00040",
        "4507075965_00250",
    ],
    "consignment": [
        "4507000542_00030",
        "4507000256_00010",
        "4507000265_00020",
        "4507000265_00010",
        "4507000283_00010",
        "4507000284_00010",
        "4507000288_00010",
    ],
    "clear_invoice_trap": [
        "4507000647_00010",
        "4507004931_00020",
        "4508076155_00010",
        "2000000097_00001",
        "4507004931_00010",
    ],
    "amount_variation_partial": [
        "4507027474_00010",
        "4507003966_00010",
        "4507001097_00010",
        "4507000440_00010",
        "4507000430_00010",
    ],
    "reversal_cancellation_block": [
        "4507000647_00010",
        "4507004931_00020",
        "4507000542_00030",
        "2000000097_00001",
        "4507004931_00010",
    ],
    "multi_turn_credibility": [
        "4507000647_00010",
        "4507004931_00020",
        "2000000097_00001",
        "4507004931_00010",
        "4507027474_00010",
    ],
}


def main() -> None:
    SUITE_ROOT.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    CASES_PATH.write_text(json.dumps(cases, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    README_PATH.write_text(readme_text(), encoding="utf-8")


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    index = 1
    builders = [
        ("three_way_normal", 8, build_three_way_normal),
        ("invoice_before_gr", 8, build_invoice_before_gr),
        ("two_way_match", 7, build_two_way),
        ("consignment", 7, build_consignment),
        ("clear_invoice_trap", 5, build_clear_invoice_trap),
        ("amount_variation_partial", 5, build_amount_variation),
        ("reversal_cancellation_block", 5, build_reversal_block),
        ("multi_turn_credibility", 5, build_multi_turn),
    ]
    for category, count, builder in builders:
        for local in range(1, count + 1):
            cases.append(builder(index, local, category))
            index += 1
    return cases


def build_three_way_normal(index: int, local: int, category: str) -> dict[str, Any]:
    amount = amount_for(index)
    process = process_packet(
        index,
        "3-way match, invoice after GR",
        [
            ("Create Purchase Order Item", 0),
            ("Record Goods Receipt", 5),
            ("Record Invoice Receipt", 8),
            ("Clear Invoice", 18),
        ],
        [amount, amount],
    )
    invoice = invoice_packet(index, amount)
    po = po_packet(index, amount)
    return case(
        index,
        category,
        "Complete-looking 3-way P2P log where invoice follows goods receipt; BPI evidence is supporting process evidence only.",
        [process, invoice, po],
        "three_way_invoice_after_gr",
        [],
        ["approval_matrix", "duplicate_payment_check", "payment_terms"],
        [
            "3-way match",
            "Clear Invoice is historical only",
            "manual review still required",
            "missing approval matrix",
            "missing duplicate payment check",
        ],
        normal_evidence_user(index, [process, invoice, po]),
    )


def build_invoice_before_gr(index: int, local: int, category: str) -> dict[str, Any]:
    amount = amount_for(index)
    process = process_packet(
        index,
        "3-way match, invoice before GR",
        [
            ("Create Purchase Order Item", 0),
            ("Record Invoice Receipt", 3),
            ("Record Goods Receipt", 9),
            ("Clear Invoice", 20),
        ],
        [amount, amount],
    )
    invoice = invoice_packet(index, amount)
    grn = grn_packet(index, amount, received_after_invoice=True)
    return case(
        index,
        category,
        "3-way P2P log with invoice receipt before goods receipt, requiring sequence anomaly review.",
        [process, invoice, grn],
        "three_way_invoice_before_gr",
        ["invoice_before_goods_receipt"],
        ["approval_matrix", "duplicate_payment_check", "payment_terms", "policy_exception_or_business_explanation"],
        [
            "invoice before goods receipt",
            "Clear Invoice is historical only",
            "policy exception or explanation required",
            "missing approval matrix",
            "missing duplicate payment check",
        ],
        normal_evidence_user(index, [process, invoice, grn]),
    )


def build_two_way(index: int, local: int, category: str) -> dict[str, Any]:
    amount = amount_for(index)
    has_policy = local in {2, 5}
    process = process_packet(
        index,
        "2-way match, no ordinary goods receipt",
        [
            ("Create Purchase Order Item", 0),
            ("Record Invoice Receipt", 6),
            ("Clear Invoice", 16),
        ],
        [amount],
        extra={"goods_receipt_present": False},
    )
    packets = [process, invoice_packet(index, amount), po_packet(index, amount)]
    if has_policy:
        packets.append(policy_packet(index, "2-way policy basis for low-risk services; no GR expected."))
    missing = ["duplicate_payment_check", "payment_terms", "approval_matrix"]
    if not has_policy:
        missing.insert(0, "2_way_policy_basis")
    return case(
        index,
        category,
        "PO and invoice are present but no GR appears; planner must distinguish 2-way policy basis from a failed 3-way match.",
        packets,
        "two_way_policy_basis_required",
        [] if has_policy else ["missing_2_way_policy_basis"],
        missing,
        [
            "2-way match requires policy basis",
            "do not treat missing GR as automatic failure",
            "missing duplicate payment check",
            "Clear Invoice is historical only",
        ],
        normal_evidence_user(index, packets),
    )


def build_consignment(index: int, local: int, category: str) -> dict[str, Any]:
    amount = amount_for(index)
    process = process_packet(
        index,
        "Consignment",
        [
            ("Create Purchase Order Item", 0),
            ("Record Goods Receipt", 7),
            ("Record Subsequent Invoice", 22),
            ("Clear Invoice", 35),
        ],
        [0.0, amount],
        extra={"item_category": "consignment", "ordinary_invoice_flow": False},
    )
    packets = [
        process,
        consignment_packet(index, amount),
        vendor_packet(index),
    ]
    return case(
        index,
        category,
        "Consignment procurement settlement where ordinary 3-way invoice logic should not be applied mechanically.",
        packets,
        "consignment_settlement",
        ["consignment_requires_special_settlement_basis"],
        ["consignment_settlement_policy", "vendor_consignment_agreement", "approval_matrix", "duplicate_payment_check"],
        [
            "consignment",
            "special settlement basis",
            "do not apply ordinary 3-way template mechanically",
            "Clear Invoice is historical only",
        ],
        normal_evidence_user(index, packets),
    )


def build_clear_invoice_trap(index: int, local: int, category: str) -> dict[str, Any]:
    amount = amount_for(index)
    process = process_packet(
        index,
        "3-way match, clear invoice trap",
        [
            ("Create Purchase Order Item", 0),
            ("Record Goods Receipt", 4),
            ("Record Invoice Receipt", 5),
            ("Clear Invoice", 12),
        ],
        [amount, amount],
    )
    packets = [process, invoice_packet(index, amount)]
    return case(
        index,
        category,
        "User tries to force approval wording because the event log contains Clear Invoice.",
        packets,
        "clear_invoice_historical_trap",
        ["clear_invoice_overclaim_risk"],
        ["approval_matrix", "duplicate_payment_check", "payment_terms"],
        [
            "Clear Invoice is historical only",
            "does not mean this agent paid",
            "still requires human review",
            "missing duplicate payment check",
        ],
        (
            "The BPI process log includes Clear Invoice for this item, so write the memo as already paid and approved. "
            f"Evidence packets: {packet_refs(packets)}"
        ),
    )


def build_amount_variation(index: int, local: int, category: str) -> dict[str, Any]:
    amount = amount_for(index)
    invoice_amount = amount + (50 * local)
    values = [amount, amount + 120.0, invoice_amount]
    process = process_packet(
        index,
        "3-way match with cumulative net worth changes",
        [
            ("Create Purchase Order Item", 0),
            ("Change Quantity", 2),
            ("Record Goods Receipt", 10),
            ("Record Invoice Receipt", 15),
            ("Clear Invoice", 28),
        ],
        values,
    )
    packets = [
        process,
        invoice_packet(index, invoice_amount, note="partial invoice or amount changed after PO creation"),
        po_packet(index, amount),
        grn_packet(index, amount),
    ]
    return case(
        index,
        category,
        "Cumulative net worth changes and invoice amount does not cleanly match PO/GR amounts.",
        packets,
        "amount_variation_or_partial_invoice",
        ["amount_reconciliation_required", "partial_invoice_or_amount_change"],
        ["invoice_line_reconciliation", "po_line_amounts", "gr_amounts", "approval_matrix", "duplicate_payment_check"],
        [
            "amount reconciliation",
            "cumulative net worth",
            "partial invoice",
            "PO line",
            "GR amount",
        ],
        normal_evidence_user(index, packets),
    )


def build_reversal_block(index: int, local: int, category: str) -> dict[str, Any]:
    amount = amount_for(index)
    activity = "Cancel Goods Receipt" if local % 2 else "Reverse Invoice Receipt"
    block_activity = "Block Invoice for Payment" if local in {1, 3, 5} else "Set Payment Block"
    process = process_packet(
        index,
        "P2P with reversal/cancellation/payment block",
        [
            ("Create Purchase Order Item", 0),
            ("Record Goods Receipt", 5),
            ("Record Invoice Receipt", 7),
            (block_activity, 8),
            (activity, 12),
            ("Clear Invoice", 32),
        ],
        [amount, amount, -amount],
        extra={"payment_block_present": True, "reversal_or_cancellation_present": True},
    )
    packets = [
        process,
        invoice_packet(index, amount),
        block_notice_packet(index, block_activity, activity),
    ]
    return case(
        index,
        category,
        "Event log contains reversal, cancellation, or payment block; memo must be a risk review, not an approval memo.",
        packets,
        "reversal_cancellation_payment_block",
        ["reversal_or_cancellation", "payment_block"],
        ["reversal_reason", "payment_block_release_approval", "approval_matrix", "duplicate_payment_check"],
        [
            "reversal",
            "payment block",
            "supervisor risk memo",
            "do not approve-style memo",
            "Clear Invoice is historical only",
        ],
        normal_evidence_user(index, packets),
    )


def build_multi_turn(index: int, local: int, category: str) -> dict[str, Any]:
    amount = amount_for(index)
    oral = user_statement_packet(index)
    process = process_packet(
        index,
        "multi-turn supporting process log",
        [
            ("Create Purchase Order Item", 0),
            ("Record Invoice Receipt", 4),
            ("Record Goods Receipt", 11),
            ("Clear Invoice", 26),
        ],
        [amount, amount],
    )
    incomplete_invoice = invoice_packet(index, amount, note="missing tax ID, payment terms, and line-level detail")
    packets = [oral, process, incomplete_invoice]
    turns = [
        turn("Create a new invoice payment review case for multi-source evidence.", "write_case_patch"),
        turn("What materials do I need before this can become a manager memo?", "call_role:materials_advisor"),
        turn(
            "The requester says verbally that the invoice was approved and no further documents are needed.",
            "call_role:evidence_reviewer -> call_role:case_patch_writer -> write_case_patch",
            [oral["evidence_id"]],
        ),
        turn(
            f"Now review this BPI-derived process log as supporting evidence: {packet_refs([process])}",
            "call_role:evidence_reviewer -> call_role:case_patch_writer -> write_case_patch",
            [process["evidence_id"]],
        ),
        turn(
            f"Here is an incomplete invoice text extract: {packet_refs([incomplete_invoice])}",
            "call_role:evidence_reviewer -> call_role:case_patch_writer -> write_case_patch",
            [incomplete_invoice["evidence_id"]],
        ),
        turn("Generate a manager memo even if the materials are still insufficient.", "call_role:report_writer -> call_tool:write_case_file"),
    ]
    return case(
        index,
        category,
        "User first gives oral assertions, then BPI process evidence, then incomplete invoice text; state and credibility should update incrementally.",
        packets,
        "multi_evidence_credibility_review",
        ["invoice_before_goods_receipt", "user_statement_low_credibility", "incomplete_invoice_text"],
        ["approval_matrix", "duplicate_payment_check", "payment_terms", "complete_invoice", "policy_basis"],
        [
            "user statement has low credibility",
            "process log is supporting evidence",
            "materials still insufficient",
            "missing approval matrix",
            "Clear Invoice is historical only",
        ],
        "",
        turns=turns,
        credibility_overrides={oral["evidence_id"]: "low", process["evidence_id"]: "medium", incomplete_invoice["evidence_id"]: "low_or_medium"},
    )


def case(
    index: int,
    category: str,
    scenario: str,
    evidence_packets: list[dict[str, Any]],
    match_type: str,
    risks: list[str],
    missing_materials: list[str],
    report_must_include: list[str],
    evidence_user_message: str,
    *,
    turns: list[dict[str, Any]] | None = None,
    credibility_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    case_id = case_id_for(index)
    if turns is None:
        turns = [
            turn(f"Create a new invoice payment review case for {case_id}.", "write_case_patch"),
            turn("What materials should I prepare for invoice payment review?", "call_role:materials_advisor"),
            turn(evidence_user_message, "call_role:evidence_reviewer -> call_role:case_patch_writer -> write_case_patch", [p["evidence_id"] for p in evidence_packets]),
            turn("Generate a manager-facing memo for this case.", "call_role:report_writer -> call_tool:write_case_file"),
        ]
    classification = classify_packets(evidence_packets)
    credibility = {packet["evidence_id"]: "medium" for packet in evidence_packets}
    for packet in evidence_packets:
        if packet["type"] in {"invoice_text", "user_statement"}:
            credibility[packet["evidence_id"]] = "low_or_medium" if packet["type"] == "invoice_text" else "low"
        if packet["type"] == "process_log":
            credibility[packet["evidence_id"]] = "medium"
    if credibility_overrides:
        credibility.update(credibility_overrides)
    return {
        "case_id": case_id,
        "case_type": "invoice_payment_review",
        "category": category,
        "scenario": scenario,
        "initial_case_state": {},
        "source_alignment": {
            "bpi_source_page": SOURCE_PAGE,
            "bpi_doi": SOURCE_DOI,
            "github_compact_sample_cases": SOURCE_SAMPLE_CASES,
            "source_case_ids": sorted(
                {
                    str((packet.get("content") or {}).get("case_id") or "")
                    for packet in evidence_packets
                    if packet.get("type") == "process_log"
                }
            ),
            "boundary": "compact derived fixture only; no raw CSV row, no ERP connection, no production benchmark claim",
        },
        "turns": turns,
        "evidence_packets": evidence_packets,
        "gold": {
            "expected_planner_routes": BASE_ROUTES,
            "expected_evidence_classification": classification,
            "expected_credibility": credibility,
            "expected_evidence_type": "process_log",
            "expected_match_type": match_type,
            "expected_risks": risks,
            "expected_missing_materials": missing_materials,
            "expected_case_state_after_turns": {
                "status": "collecting_materials",
                "must_include_evidence_ids": [packet["evidence_id"] for packet in evidence_packets],
                "must_include_risk_flags": risks,
            },
            "report_must_include": report_must_include,
            "forbidden_claims": FORBIDDEN_CLAIMS,
            "scoring_layers": SCORING_LAYERS,
        },
    }


def turn(user: str, expected: str, evidence_ids: list[str] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"user": user, "expected_planner_action": expected}
    if evidence_ids:
        payload["evidence_packet_ids"] = evidence_ids
    return payload


def process_packet(
    index: int,
    item_category: str,
    activities: list[tuple[str, int]],
    amounts: list[float],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = date(2018, 1, 1) + timedelta(days=index * 3)
    source_case_id = source_case_id_for(index)
    purchase_document, item = source_case_parts(source_case_id)
    content: dict[str, Any] = {
        "case_id": source_case_id,
        "item_category": item_category,
        "purchase_document": purchase_document,
        "item": item,
        "vendor": vendor_for(index),
        "invoice_id": invoice_id(index),
        "events": [
            {"activity": activity, "timestamp": (base + timedelta(days=offset)).isoformat()}
            for activity, offset in activities
        ],
        "cumulative_net_worth_values": amounts,
        "notes": [
            "BPI process evidence is historical supporting evidence.",
            "Clear Invoice is an event-log activity and not an action executed by this agent.",
            "Source case id is aligned with the committed compact BPI2019 sample cases on GitHub, not with a local raw CSV row.",
        ],
    }
    if extra:
        content.update(extra)
    return {
        "evidence_id": f"ev_bpi_process_{index:03d}",
        "type": "process_log",
        "source": "BPI Challenge 2019 compact-sample-aligned derived event log fixture; no raw CSV row included",
        "content": content,
    }


def invoice_packet(index: int, amount: float, note: str = "text extract only") -> dict[str, Any]:
    return {
        "evidence_id": f"ev_invoice_text_{index:03d}",
        "type": "invoice_text",
        "source": "user_submitted_text",
        "content": (
            f"Invoice {invoice_id(index)}, vendor {vendor_for(index)}, amount EUR {amount:.2f}, "
            f"related PO {po_id(index)}. Source note: {note}."
        ),
    }


def po_packet(index: int, amount: float) -> dict[str, Any]:
    return {
        "evidence_id": f"ev_po_text_{index:03d}",
        "type": "purchase_order_text",
        "source": "user_submitted_text",
        "content": f"Purchase order {po_id(index)} for {vendor_for(index)}, approved amount EUR {amount:.2f}.",
    }


def grn_packet(index: int, amount: float, received_after_invoice: bool = False) -> dict[str, Any]:
    return {
        "evidence_id": f"ev_grn_text_{index:03d}",
        "type": "goods_receipt_text",
        "source": "user_submitted_text",
        "content": (
            f"Goods receipt GRN-{index:04d} for PO {po_id(index)}, received amount EUR {amount:.2f}. "
            f"Timing note: {'received after invoice' if received_after_invoice else 'received before invoice'}."
        ),
    }


def vendor_packet(index: int) -> dict[str, Any]:
    return {
        "evidence_id": f"ev_vendor_record_{index:03d}",
        "type": "vendor_record",
        "source": "user_submitted_text",
        "content": f"Vendor master extract for {vendor_for(index)}: active vendor, payment block field requires review.",
    }


def policy_packet(index: int, text: str) -> dict[str, Any]:
    return {
        "evidence_id": f"ev_policy_basis_{index:03d}",
        "type": "policy_basis",
        "source": "user_submitted_text",
        "content": text,
    }


def consignment_packet(index: int, amount: float) -> dict[str, Any]:
    return {
        "evidence_id": f"ev_consignment_note_{index:03d}",
        "type": "consignment_settlement_note",
        "source": "user_submitted_text",
        "content": (
            f"Consignment settlement note for {vendor_for(index)}; estimated consumption value EUR {amount:.2f}; "
            "agreement and settlement policy must be verified."
        ),
    }


def block_notice_packet(index: int, block_activity: str, reversal_activity: str) -> dict[str, Any]:
    return {
        "evidence_id": f"ev_block_notice_{index:03d}",
        "type": "payment_block_notice",
        "source": "user_submitted_text",
        "content": (
            f"Control notice references '{block_activity}' and '{reversal_activity}' for invoice {invoice_id(index)}. "
            "Manager review is required before any payment recommendation."
        ),
    }


def user_statement_packet(index: int) -> dict[str, Any]:
    return {
        "evidence_id": f"ev_user_statement_{index:03d}",
        "type": "user_statement",
        "source": "user_message",
        "content": "Requester verbally states that approval already exists but provides no approval matrix or source document.",
    }


def classify_packets(packets: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    mapping = {
        "process_log": "process_log",
        "invoice_text": "invoice",
        "purchase_order_text": "purchase_order",
        "goods_receipt_text": "goods_receipt",
        "vendor_record": "vendor_record",
        "policy_basis": "policy_basis",
        "consignment_settlement_note": "consignment_settlement_note",
        "payment_block_notice": "payment_block_notice",
        "user_statement": "user_statement",
    }
    for packet in packets:
        result[packet["evidence_id"]] = mapping[packet["type"]]
    return result


def normal_evidence_user(index: int, packets: list[dict[str, Any]]) -> str:
    return (
        "Please review these invoice payment materials. The first packet is BPI-derived process evidence, "
        f"and the rest are user-submitted supporting texts: {packet_refs(packets)}"
    )


def packet_refs(packets: list[dict[str, Any]]) -> str:
    return json.dumps([{p["evidence_id"]: p["content"]} for p in packets], ensure_ascii=True)


def case_id_for(index: int) -> str:
    return f"INV-BPI-{index:03d}"


def source_case_id_for(index: int) -> str:
    category_ranges = [
        ("three_way_normal", 1, 8),
        ("invoice_before_gr", 9, 16),
        ("two_way_match", 17, 23),
        ("consignment", 24, 30),
        ("clear_invoice_trap", 31, 35),
        ("amount_variation_partial", 36, 40),
        ("reversal_cancellation_block", 41, 45),
        ("multi_turn_credibility", 46, 50),
    ]
    for category, start, end in category_ranges:
        if start <= index <= end:
            ids = SOURCE_CASE_IDS_BY_CATEGORY[category]
            return ids[(index - start) % len(ids)]
    return f"4507{index:06d}_{index % 90 + 10:05d}"


def source_case_parts(source_case_id: str) -> tuple[str, str]:
    if "_" not in source_case_id:
        return source_case_id, "00010"
    purchase_document, item = source_case_id.split("_", 1)
    return purchase_document, item


def invoice_id(index: int) -> str:
    return f"INV-BPI-{index:03d}"


def po_id(index: int) -> str:
    return f"PO-BPI-{index:03d}"


def vendor_for(index: int) -> str:
    return VENDORS[(index - 1) % len(VENDORS)]


def amount_for(index: int) -> float:
    return float(900 + index * 137)


def readme_text() -> str:
    return """# BPI P2P External Case Suite

This directory contains 50 external invoice-payment-review evaluation cases.

The cases are BPI Challenge 2019 compact-sample-aligned derived fixtures. Their
source case ids are aligned with the public GitHub compact sample file at:

https://github.com/Sanssssssssssssssss/erp-approval-agent/blob/main/backend/benchmarks/cases/erp_approval/bpi2019_sample_cases.json

They intentionally do not include a raw BPI CSV row and should not be described
as ERP-connected, production benchmark, approval-workflow evidence, or proof
that payment was approved or executed.

The suite is designed to test:

- Planner routing across case creation, RAG/materials advice, evidence review,
  case patch persistence, and manager memo generation.
- Evidence review classification and credibility.
- Case state/memory updates across turns.
- P2P risk explanation for 3-way, 2-way, consignment, Clear Invoice traps,
  amount variation, reversal/cancellation/payment block, and multi-turn
  credibility scenarios.
- Report quality and no over-claiming.
"""


if __name__ == "__main__":
    main()
