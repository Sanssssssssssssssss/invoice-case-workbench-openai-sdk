from __future__ import annotations

import hashlib
import json

from app.state.attachment_manifest import load_attachment_manifest, save_attachment_manifest
from app.state.case_store import CaseStore
from app.tools.catalog import ToolCatalog
from app.tools.file_workspace import FileWorkspace


def test_case_385104_superseded_png_repair_releases_amount_and_source_traceability(tmp_path) -> None:
    store = CaseStore(tmp_path)
    case_id = "case_20260530_172740_385104_regression"
    store.apply_patch(
        case_id,
        {
            "patch_type": "add_evidence",
            "case_updates": {
                "requirements": [
                    {"id": "amount_total", "label": "总金额", "required": True},
                    {"id": "source_traceability", "label": "来源可追溯性", "required": True},
                ],
                "add_evidence": [
                    {
                        "id": "ev_001",
                        "type": "invoice",
                        "credibility": "medium",
                        "summary": "Weak PNG OCR misread amount and traceability.",
                        "review_result": {"should_accept": True, "evidence_type": "invoice"},
                        "supports": [
                            {"requirement": "amount_total", "support_level": "partial", "quoted_text": "$6067"},
                            {"requirement": "source_traceability", "support_level": "partial", "quoted_text": "PNG screenshot"},
                        ],
                        "conflicts": [
                            {
                                "conflict_type": "ocr_amount_mismatch",
                                "requirement": "amount_total",
                                "description": "Weak PNG OCR conflicts with PDF total.",
                            },
                            {
                                "conflict_type": "weak_source",
                                "requirement": "source_traceability",
                                "description": "PNG should not be the active source when PDF is available.",
                            },
                        ],
                        "metadata": {"review_stage": "reviewed", "source_doc_id": "png_upload"},
                    }
                ],
            },
            "audit_note": "seed manual weak PNG conflict",
        },
    )

    original_ref = "attachments/originals/invoice.pdf"
    source_path = store.resolve_case_path(case_id, original_ref)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"%PDF-1.4 regression fixture")
    extraction_ref = "evidence/extractions/att_corrected_invoice.json"
    extraction_path = store.resolve_case_path(case_id, extraction_ref)
    extraction_path.parent.mkdir(parents=True, exist_ok=True)
    extraction_path.write_text(json.dumps({
        "attachment_id": "att_corrected_invoice",
        "full_text": "Grand Total 319.00",
    }), encoding="utf-8")
    save_attachment_manifest(store, case_id, {"attachments": [{
        "attachment_id": "att_corrected_invoice",
        "name": "invoice.pdf",
        "original_ref": original_ref,
        "status": "active",
        "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "extraction_ref": extraction_ref,
        "extraction_sha256": hashlib.sha256(extraction_path.read_bytes()).hexdigest(),
    }]})

    updated = store.apply_patch(
        case_id,
        {
            "patch_type": "add_evidence",
            "case_updates": {
                "add_evidence": [
                    {
                        "id": "ev_004",
                        "type": "invoice",
                        "source": "attachment",
                        "credibility": "high",
                        "summary": "PDF repair confirms amount and original source.",
                        "review_result": {"should_accept": True, "evidence_type": "invoice"},
                        "supports": [
                            {"requirement": "amount_total", "support_level": "full", "quoted_text": "Grand Total 319.00"},
                            {
                                "requirement": "source_traceability",
                                "support_level": "full",
                                "quoted_text": "original_ref: attachments/originals/invoice.pdf",
                            },
                        ],
                        "conflicts": [],
                        "metadata": {
                            "classification": "business_evidence",
                            "review_stage": "corrected",
                            "supersedes_evidence_id": "ev_001",
                            "source_doc_id": "pdf_upload",
                            "original_ref": original_ref,
                        },
                    }
                ],
            },
            "audit_note": "repair: use source PDF and ignore weak PNG blocker",
        },
    )

    statuses = {item.id: item.status for item in updated.requirements}
    old_evidence = next(item for item in updated.evidence_items if item.id == "ev_001")
    assert statuses == {"amount_total": "satisfied", "source_traceability": "satisfied"}
    assert updated.conflict_materials == []
    assert updated.status == "ready_for_report"
    assert old_evidence.metadata["review_stage"] == "superseded"
    assert old_evidence.metadata["superseded_by_evidence_id"] == "ev_004"
    _assert_timestamped_report_generated(store, case_id, started_at="2026-05-30T17:27:40+00:00")


def test_case_486266_ap_supplements_bind_manifest_and_keep_real_conflicts(tmp_path) -> None:
    store = CaseStore(tmp_path)
    case_id = "case_20260530_144019_486266_regression"
    save_attachment_manifest(
        store,
        case_id,
        {
            "attachments": [
                {
                    "attachment_id": "att_invoice",
                    "name": "01_invoice.pdf",
                    "original_ref": "attachments/originals/01_invoice.pdf",
                    "status": "active",
                    "evidence_ids": [],
                },
                {
                    "attachment_id": "att_po",
                    "name": "02_purchase_order.md",
                    "original_ref": "attachments/originals/02_purchase_order.md",
                    "status": "active",
                    "evidence_ids": [],
                },
                {
                    "attachment_id": "att_grn",
                    "name": "03_goods_receipt.md",
                    "original_ref": "attachments/originals/03_goods_receipt.md",
                    "status": "active",
                    "evidence_ids": [],
                },
                {
                    "attachment_id": "att_vendor",
                    "name": "04_vendor_record.md",
                    "original_ref": "attachments/originals/04_vendor_record.md",
                    "status": "active",
                    "evidence_ids": [],
                },
                {
                    "attachment_id": "att_duplicate",
                    "name": "05_duplicate_payment_check.md",
                    "original_ref": "attachments/originals/05_duplicate_payment_check.md",
                    "status": "active",
                    "evidence_ids": [],
                },
            ]
        },
    )

    updated = store.apply_patch(
        case_id,
        {
            "patch_type": "add_evidence",
            "case_updates": {
                "requirements": [
                    {"id": "invoice", "label": "发票", "required": True},
                    {"id": "purchase_order", "label": "采购订单", "required": True},
                    {"id": "goods_receipt_or_service_acceptance", "label": "收货或服务验收", "required": True},
                    {"id": "vendor_identity", "label": "供应商身份", "required": True},
                    {"id": "duplicate_payment_screen", "label": "重复付款筛查", "required": True},
                ],
                "add_evidence": [
                    {
                        "id": "ev_001",
                        "type": "invoice",
                        "credibility": "medium",
                        "summary": "SAP invoice PDF.",
                        "source": "attachment",
                        "review_result": {"should_accept": True, "evidence_type": "invoice"},
                        "supports": [{"requirement": "invoice", "support_level": "partial", "quoted_text": "Invoice 5435569865439"}],
                        "metadata": {"original_ref": "attachments/originals/01_invoice.pdf"},
                    },
                    {
                        "id": "ev_002",
                        "type": "purchase_order",
                        "credibility": "high",
                        "summary": "Purchase order matches invoice supplier and amount.",
                        "source": "attachment",
                        "review_result": {"should_accept": True, "evidence_type": "purchase_order"},
                        "supports": [{"requirement": "purchase_order", "support_level": "full", "quoted_text": "PO-CR-5435569865439"}],
                        "metadata": {"original_ref": "attachments/originals/02_purchase_order.md"},
                    },
                    {
                        "id": "ev_003",
                        "type": "goods_receipt",
                        "credibility": "high",
                        "summary": "Goods receipt date is after invoice date.",
                        "source": "attachment",
                        "review_result": {"should_accept": True, "evidence_type": "goods_receipt"},
                        "supports": [
                            {
                                "requirement": "goods_receipt_or_service_acceptance",
                                "support_level": "partial",
                                "quoted_text": "GRN received after invoice date",
                            }
                        ],
                        "conflicts": [
                            {
                                "conflict_type": "grn_after_invoice",
                                "requirement": "goods_receipt_or_service_acceptance",
                                "description": "Receipt date follows invoice date and needs business explanation.",
                            }
                        ],
                        "metadata": {"original_ref": "attachments/originals/03_goods_receipt.md"},
                    },
                    {
                        "id": "ev_004",
                        "type": "vendor_record",
                        "credibility": "high",
                        "summary": "Vendor record confirms supplier identity.",
                        "source": "attachment",
                        "review_result": {"should_accept": True, "evidence_type": "vendor_record"},
                        "supports": [{"requirement": "vendor_identity", "support_level": "full", "quoted_text": "Electric Installations - Urs Schmid"}],
                        "metadata": {"original_ref": "attachments/originals/04_vendor_record.md"},
                    },
                    {
                        "id": "ev_005",
                        "type": "duplicate_payment_check",
                        "credibility": "high",
                        "summary": "Duplicate screen finds historical clearing reference.",
                        "source": "attachment",
                        "review_result": {"should_accept": True, "evidence_type": "duplicate_payment_check"},
                        "supports": [
                            {
                                "requirement": "duplicate_payment_screen",
                                "support_level": "partial",
                                "quoted_text": "AY-5435569865439-HIST",
                            }
                        ],
                        "conflicts": [
                            {
                                "conflict_type": "duplicate_payment_hit",
                                "requirement": "duplicate_payment_screen",
                                "description": "Same supplier and invoice reference have a historical clearing record.",
                            }
                        ],
                        "metadata": {"original_ref": "attachments/originals/05_duplicate_payment_check.md"},
                    },
                ],
            },
            "audit_note": "manual AP supplement regression",
        },
    )

    manifest = load_attachment_manifest(store, case_id)
    evidence_by_ref = {item["original_ref"]: item.get("evidence_ids") for item in manifest["attachments"]}
    assert evidence_by_ref["attachments/originals/02_purchase_order.md"] == ["ev_002"]
    assert evidence_by_ref["attachments/originals/03_goods_receipt.md"] == ["ev_003"]
    assert evidence_by_ref["attachments/originals/04_vendor_record.md"] == ["ev_004"]
    assert evidence_by_ref["attachments/originals/05_duplicate_payment_check.md"] == ["ev_005"]
    assert "purchase_order" in updated.satisfied_materials
    assert "vendor_identity" in updated.satisfied_materials
    assert "goods_receipt_or_service_acceptance" in updated.conflict_materials
    assert "duplicate_payment_screen" in updated.conflict_materials
    _assert_timestamped_report_generated(store, case_id, started_at="2026-05-30T14:40:19+00:00")


def _assert_timestamped_report_generated(store: CaseStore, case_id: str, *, started_at: str) -> None:
    registry = ToolCatalog(FileWorkspace(store))
    run_state = type("RunState", (), {"started_at": started_at})()
    markdown = (
        "# 发票付款材料审查报告\n\n"
        "## 目录\n\n"
        "## 第一章 概览\n\n"
        "### 本轮请求与审查范围\n"
        "本报告用于回归验证：case 状态、证据绑定、风险和限制均应能进入可渲染报告。\n\n"
        "### 风险速览\n"
        "- 以 case_state 中的 conflict_materials 为准，不用旧证据覆盖新 repair 结论。\n\n"
        "### 摘要结论\n"
        "报告文件必须默认生成 Markdown 源文件和 PDF 留档文件。\n\n"
        "## 第二章 证据审核\n\n"
        "| claim_id | claim | evidence_ids | sources | support_levels | conflicts | confidence | limitation |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| C-001 | 回归 case 已保存证据并刷新状态 | ev_001 | case_state | partial/full | 见状态桶 | medium | 合成 fixture |\n\n"
        "## 第三章 证据图录\n\n"
        "字段截图和原始附件截图由 PDF renderer 根据 evidence metadata 自动补充。\n"
    )
    write_result = registry.call(
        "write_case_file",
        case_id,
        {"relative_path": "reports/final_report.md", "content": markdown},
        run_state=run_state,
    )
    render_result = registry.call(
        "render_pdf",
        case_id,
        {"markdown_path": "reports/final_report.md", "pdf_path": "reports/final_report.pdf"},
        run_state=run_state,
    )

    expected_stem = started_at.replace("-", "").replace(":", "")[:8] + "_" + started_at[11:19].replace(":", "")
    assert write_result["relative_path"] == f"reports/final_report_{expected_stem}.md"
    assert render_result["markdown_path"] == f"reports/final_report_{expected_stem}.md"
    assert render_result["pdf_path"] == f"reports/final_report_{expected_stem}.pdf"
    assert store.resolve_case_path(case_id, render_result["markdown_path"]).exists()
    assert store.resolve_case_path(case_id, render_result["pdf_path"]).exists()
    assert int(render_result.get("page_count") or 0) >= 1
