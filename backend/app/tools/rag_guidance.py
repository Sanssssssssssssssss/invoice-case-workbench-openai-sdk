from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.tools.rag_search import RagSkill


@dataclass
class RagGuidance:
    evidences: list[dict[str, Any]]
    debug: dict[str, Any]


def advisor_guidance(
    user_question: str,
    case_state: dict[str, Any],
    attachment_manifest: dict[str, Any],
    *,
    top_k: int = 5,
) -> RagGuidance:
    query = _advisor_guidance_query(user_question, case_state, attachment_manifest)
    return _retrieve(query, role="materials_advisor", top_k=top_k)


def review_guidance(
    user_message: str,
    attachment_context: list[dict[str, Any]],
    attachment_manifest: dict[str, Any],
    extraction_result: dict[str, Any] | None = None,
    *,
    top_k: int = 5,
) -> RagGuidance:
    query = _review_guidance_query(user_message, attachment_context, attachment_manifest, extraction_result or {})
    return _retrieve(query, role="evidence_reviewer", top_k=top_k)


def _retrieve(query: str, *, role: str, top_k: int) -> RagGuidance:
    if not query:
        return RagGuidance([], {"role": role, "status": "skipped", "query": "", "source_ids": [], "top_k": top_k})
    try:
        result = RagSkill().retrieve(query=query, intent="policy_qa", top_k=top_k)
        evidences = [item.model_dump() for item in result.evidences]
        return RagGuidance(
            evidences,
            {
                "role": role,
                "status": "success",
                "query": query,
                "source_ids": [str(item.get("source_id") or "") for item in evidences],
                "source_paths": [str(item.get("source_path") or "") for item in evidences],
                "locators": [str(item.get("locator") or "") for item in evidences],
                "profile_ids": [str((item.get("fields") or {}).get("profile_id") or "") for item in evidences],
                "snippets": [_snippet_preview(str(item.get("snippet") or "")) for item in evidences],
                "scores": [float(item.get("score") or 0.0) for item in evidences],
                "channels": [str(item.get("channel") or "") for item in evidences],
                "top_k": top_k,
                "evidence_count": len(evidences),
            },
        )
    except Exception as exc:
        return RagGuidance(
            [],
            {
                "role": role,
                "status": "error",
                "query": query,
                "source_ids": [],
                "top_k": top_k,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


def _advisor_guidance_query(user_question: str, case_state: dict[str, Any], attachment_manifest: dict[str, Any]) -> str:
    requirements = case_state.get("requirements") if isinstance(case_state, dict) else []
    requirement_terms: list[str] = []
    if isinstance(requirements, list):
        for item in requirements[:20]:
            if isinstance(item, dict):
                requirement_terms.append(" ".join(str(item.get(key) or "") for key in ("id", "label", "status", "guidance")))
    rows = attachment_manifest.get("attachments") if isinstance(attachment_manifest, dict) else []
    attachment_terms: list[str] = []
    if isinstance(rows, list):
        for item in rows[:8]:
            if isinstance(item, dict):
                attachment_terms.append(" ".join(str(item.get(key) or "") for key in ("name", "content_kind", "status", "summary")))
    text = "\n".join([str(user_question or ""), *requirement_terms, *attachment_terms]).strip()
    if not text:
        return ""
    return (
        "invoice review material requirements advisor guidance invoice-only AP lite "
        "field completeness tax amount duplicate payment bank change approval matrix segregation of duties "
        "payment release vendor onboarding non-PO contract invoice GL coding exception hold audit trail OCR process boundary "
        f"{text[:1200]}"
    )


def _review_guidance_query(
    user_message: str,
    attachment_context: list[dict[str, Any]],
    attachment_manifest: dict[str, Any],
    extraction_result: dict[str, Any],
) -> str:
    text = (
        f"{user_message}\n"
        f"{_compact_attachment_terms(attachment_context)}\n"
        f"{_compact_manifest_terms(attachment_manifest)}\n"
        f"{_compact_extraction_terms(extraction_result)}"
    ).strip()
    lower = text.lower()
    markers = (
        "invoice",
        "bill",
        "flipkart",
        "sap",
        "duplicate",
        "bank",
        "ap review",
        "payment review",
        "three-way",
        "purchase order",
        "goods receipt",
        "po",
        "grn",
        "clear invoice",
        "prompt injection",
        "signature",
        "signatory",
        "authorized",
        "template",
        "layout",
        "same vendor",
        "reference invoice",
        "visual match",
        "profile match",
        "factu",
        "mouadhamri",
        "ws retail",
        "retail invoice/bill",
        "签名",
        "签章",
        "授权",
        "模板",
        "版式",
        "同厂商",
        "匹配",
        "样例",
        "发票",
        "账单",
        "规则",
        "发票付款审查",
        "付款审查",
        "三单匹配",
        "重复付款",
        "银行变更",
        "注入",
        "审批",
        "审批矩阵",
        "授权审批",
        "职责分离",
        "权限冲突",
        "付款释放",
        "供应商入驻",
        "主数据",
        "非 po",
        "合同发票",
        "成本中心",
        "总账",
        "税码",
        "容差",
        "例外审批",
        "审计留痕",
        "approval matrix",
        "approval authority",
        "segregation of duties",
        "sod",
        "payment release",
        "payment run",
        "vendor onboarding",
        "vendor master",
        "non-po",
        "contract invoice",
        "gl coding",
        "cost center",
        "tax treatment",
        "matching hold",
        "tolerance",
        "audit trail",
    )
    if not any(marker in lower for marker in markers):
        return ""
    profile_terms: list[str] = ["invoice_field_completeness", "invoice_only_material_profile"]
    if any(term in lower for term in ("ap review", "payment review", "three-way", "purchase order", "goods receipt", "po", "grn", "发票付款审查", "付款审查", "三单匹配")):
        profile_terms.append("ap_lite_payment_review_material_profile case_playbook_aurora invoice payment review AP lite PO GRN Aurora")
    if "flipkart" in lower or "ws retail" in lower:
        profile_terms.append("case_05_flipkart_ws_retail_invoice_bill flipkart_retail_invoice_bill Flipkart WS Retail invoice authorized signatory retail invoice bill")
    if "sap" in lower:
        profile_terms.append("case_06_sap_dox_invoice_duplicate sap_dox_invoice_validation_sample SAP invoice supplier total currency")
    if any(term in lower for term in ("factu", "mouadhamri", "hamri")):
        profile_terms.append(
            "case_01_mouadhamri_FACTU2015020048_clean "
            "case_02_mouadhamri_FACTU2015040047_amount_conflict "
            "case_03_mouadhamri_FACTU2015050046_duplicate_hit "
            "case_04_mouadhamri_FACTU2015060039_bank_change "
            "mouadhamri_invoice_dataset_FACTU scanned invoice template family"
        )
    if any(term in lower for term in ("signature", "signatory", "authorized", "template", "layout", "same vendor", "reference invoice", "visual match", "profile match", "签名", "签章", "授权", "模板", "版式", "同厂商", "匹配", "样例")):
        profile_terms.append(
            "signature template visual layout same vendor reference invoice "
            "case_01_mouadhamri_FACTU2015020048_clean "
            "case_02_mouadhamri_FACTU2015040047_amount_conflict "
            "case_03_mouadhamri_FACTU2015050046_duplicate_hit "
            "case_04_mouadhamri_FACTU2015060039_bank_change "
            "case_05_flipkart_ws_retail_invoice_bill "
            "case_06_sap_dox_invoice_duplicate "
            "matched_profile same_vendor_reference matched_layout_clues boundary consistency signal"
        )
    if "duplicate" in lower or "重复付款" in lower:
        profile_terms.append("duplicate_payment_control duplicate payment historical clearing")
    if "bank" in lower or "银行变更" in lower:
        profile_terms.append("vendor_master_bank_change_control bank change supplier master")
    if "prompt injection" in lower or "注入" in lower:
        profile_terms.append("source_quality_prompt_injection prompt injection attachment boundary")
    if any(term in lower for term in ("approval matrix", "approval authority", "delegation", "审批矩阵", "授权审批", "审批权限")):
        profile_terms.append("approval_authority_matrix_control approval limit delegation workflow approval")
    if any(term in lower for term in ("segregation of duties", "sod", "same user", "职责分离", "权限冲突")):
        profile_terms.append("segregation_of_duties_ap_control same user creates vendor approves releases payment")
    if any(term in lower for term in ("payment release", "payment run", "ach", "wire", "付款释放", "电汇")):
        profile_terms.append("payment_release_disbursement_control payment run ACH wire bank account vendor master")
    if any(term in lower for term in ("vendor onboarding", "vendor master", "供应商入驻", "主数据", "重复供应商")):
        profile_terms.append("vendor_onboarding_master_data_governance vendor master change log duplicate vendor")
    if any(term in lower for term in ("non-po", "contract invoice", "sow", "非 po", "无 po", "合同发票", "服务费")):
        profile_terms.append("non_po_contract_invoice_control contract invoice service acceptance recurring service")
    if any(term in lower for term in ("gl coding", "cost center", "tax treatment", "vat", "gst", "总账", "成本中心", "税码")):
        profile_terms.append("tax_gl_coding_cost_center_control GL coding tax treatment cost center")
    if any(term in lower for term in ("matching hold", "payment hold", "tolerance", "hold release", "容差", "例外审批")):
        profile_terms.append("exception_hold_tolerance_control matching hold tolerance exceeded discrepancy approval")
    if "audit trail" in lower or "审计留痕" in lower or "claim-to-evidence" in lower:
        profile_terms.append("audit_trail_retention_control audit trail claim-to-evidence source locator")
    return (
        f"{' '.join(profile_terms)} "
        "invoice evidence review rules template profile expected fields visual layout "
        "defects duplicate payment bank change approval authority segregation of duties payment release vendor master non-PO tax GL hold audit trail prompt injection OCR boundary "
        f"{text[:900]}"
    ).strip()


def _compact_extraction_terms(extraction_result: dict[str, Any]) -> str:
    if not isinstance(extraction_result, dict):
        return ""
    pieces: list[str] = []
    for key in ("source_doc_id", "document_type", "doc_type", "visual_summary", "extracted_summary"):
        value = extraction_result.get(key)
        if value:
            pieces.append(str(value))
    source_docs = extraction_result.get("source_docs") or extraction_result.get("documents") or []
    if isinstance(source_docs, list):
        for item in source_docs[:4]:
            if isinstance(item, dict):
                pieces.append(" ".join(str(item.get(key) or "") for key in ("doc_type", "title", "visual_summary", "extracted_summary")))
    return "\n".join(piece for piece in pieces if piece)[:900]


def _compact_attachment_terms(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items[:6]:
        if not isinstance(item, dict):
            continue
        fields = [item.get("name"), item.get("content_kind"), item.get("extraction_method"), item.get("summary")]
        parts.append(" ".join(str(value) for value in fields if value))
    return "\n".join(parts)


def _snippet_preview(value: str, max_chars: int = 360) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _compact_manifest_terms(manifest: dict[str, Any]) -> str:
    rows = manifest.get("attachments") if isinstance(manifest, dict) else []
    if not isinstance(rows, list):
        return ""
    parts: list[str] = []
    for item in rows[:6]:
        if not isinstance(item, dict):
            continue
        fields = [item.get("name"), item.get("content_kind"), item.get("status"), item.get("summary")]
        parts.append(" ".join(str(value) for value in fields if value))
    return "\n".join(parts)
