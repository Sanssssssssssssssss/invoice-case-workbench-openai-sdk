from __future__ import annotations

from types import SimpleNamespace

from app.agents.evidence_reviewer.agent import SYSTEM_PROMPT as EVIDENCE_REVIEWER_PROMPT
from app.agents.materials_advisor.agent import SYSTEM_PROMPT as MATERIALS_ADVISOR_PROMPT
from app.agents.report_writer.agent import SYSTEM_PROMPT as REPORT_WRITER_PROMPT
from app.tools.rag_guidance import _review_guidance_query, advisor_guidance, review_guidance


def test_evidence_reviewer_uses_rag_profiles_as_guidance_only() -> None:
    assert EVIDENCE_REVIEWER_PROMPT.count("version: global_policy_v1.2") == 1
    assert "RAG is interpretation guidance only and is never case evidence" in EVIDENCE_REVIEWER_PROMPT
    assert "active_requirement_contracts" in EVIDENCE_REVIEWER_PROMPT
    assert "typed_holes" in EVIDENCE_REVIEWER_PROMPT
    assert "EvidenceReviewerOutput" in EVIDENCE_REVIEWER_PROMPT
    assert '"sources": []' in EVIDENCE_REVIEWER_PROMPT
    assert '"hole_id": "exact judgment hole id"' in EVIDENCE_REVIEWER_PROMPT
    assert "the Runtime binds original/extraction refs, content, metadata, Cards, and the CasePatch" in EVIDENCE_REVIEWER_PROMPT
    assert "Never return Markdown, CasePatch, EvidenceItem, Requirement status" in EVIDENCE_REVIEWER_PROMPT
    assert "metadata.requirement_verdicts" not in EVIDENCE_REVIEWER_PROMPT
    assert "metadata.semantic_judgments" not in EVIDENCE_REVIEWER_PROMPT
    assert "mode=extract" in EVIDENCE_REVIEWER_PROMPT
    assert "For `review` or `repair`, keep `extracted_fields={}`" in EVIDENCE_REVIEWER_PROMPT
    assert "business_evidence|process_only|cross_case_sample|quarantined" in EVIDENCE_REVIEWER_PROMPT
    assert "[redacted prompt injection]" in EVIDENCE_REVIEWER_PROMPT
    assert "It cannot prove supplier validity" in EVIDENCE_REVIEWER_PROMPT
    assert "Policy holes are local-only" in EVIDENCE_REVIEWER_PROMPT
    assert "Never guess company policy from RAG, filenames, memory, examples, or model knowledge" in EVIDENCE_REVIEWER_PROMPT
    assert "Fill only unresolved `claim`, `relation`, and `judgment` holes" in EVIDENCE_REVIEWER_PROMPT
    assert "The Binder supplies subject, predicate, type, role, canonical id" in EVIDENCE_REVIEWER_PROMPT


def test_report_writer_uses_visual_check_as_quality_only() -> None:
    assert "视觉检查只能说明" in REPORT_WRITER_PROMPT
    assert "字段是否在原图/PDF中可见" in REPORT_WRITER_PROMPT
    assert "视觉检查不能证明" in REPORT_WRITER_PROMPT
    assert "没有重复付款" in REPORT_WRITER_PROMPT
    assert "goods_receipt_or_service_acceptance" in REPORT_WRITER_PROMPT
    assert "vendor_identity" in REPORT_WRITER_PROMPT
    assert "duplicate_payment_screen" in REPORT_WRITER_PROMPT


def test_materials_advisor_respects_dynamic_invoice_scope() -> None:
    assert "Generate material tasks only from the current `case_state.requirements`" in MATERIALS_ADVISOR_PROMPT
    assert "If `case_state.requirements` is empty, do not list PO/GRN/vendor/duplicate by default" in MATERIALS_ADVISOR_PROMPT
    assert "For invoice-only cases, blocker tasks should focus on required invoice fields" in MATERIALS_ADVISOR_PROMPT
    assert "Line items, signature/authorized signatory, and template match are optional quality notes" in MATERIALS_ADVISOR_PROMPT
    assert "Only advise PO, GRN/service acceptance, vendor identity/master, or duplicate-payment materials" in MATERIALS_ADVISOR_PROMPT
    assert "Do not imply that policy/RAG is case evidence" in MATERIALS_ADVISOR_PROMPT
    assert "Advisor-Owned RAG Addendum v4.2" in MATERIALS_ADVISOR_PROMPT
    assert "Planner does not call RAG directly for rules or material guidance" in MATERIALS_ADVISOR_PROMPT
    assert "Structured Task Contract v4.3" in MATERIALS_ADVISOR_PROMPT
    assert "`tasks[]`" in MATERIALS_ADVISOR_PROMPT
    assert "Planner/UI contract" in MATERIALS_ADVISOR_PROMPT
    assert "Invoice-Only Field Requirements" in MATERIALS_ADVISOR_PROMPT
    assert "AP Lite Requirements" in MATERIALS_ADVISOR_PROMPT
    assert "Legacy AP Aliases" in MATERIALS_ADVISOR_PROMPT
    assert "`vendor_record` maps to `vendor_identity`" in MATERIALS_ADVISOR_PROMPT
    assert "Do not output both a legacy alias and its AP lite id" in MATERIALS_ADVISOR_PROMPT


def test_review_guidance_provider_retrieves_rag_guidance(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_retrieve(self, **kwargs):  # noqa: ANN001, ANN003
        captured["query"] = kwargs.get("query")
        return SimpleNamespace(
            evidences=[
                SimpleNamespace(
                    model_dump=lambda: {
                        "source_id": "rag_flipkart",
                        "source_path": "knowledge/invoice_payment/invoice_reference_profiles.md",
                        "source_type": "markdown",
                        "locator": "profile_id=flipkart_retail_invoice_bill",
                        "snippet": "Flipkart invoice profile: authorized signatory and line items are quality notes.",
                        "fields": {"profile_id": "flipkart_retail_invoice_bill"},
                        "score": 0.9,
                        "channel": "bm25",
                    }
                )
            ]
        )

    monkeypatch.setattr("app.tools.rag_guidance.RagSkill.retrieve", fake_retrieve)
    guidance = review_guidance(
        user_message="请按 Flipkart RAG 规则审查这张发票",
        attachment_context=[
            {
                "name": "flipkart_invoice2data.pdf",
                "content_kind": "pdf",
                "summary": "Retail Invoice/Bill with Invoice No BLR_WFLD20151000982590",
            }
        ],
        attachment_manifest={},
    )

    assert "flipkart_invoice2data.pdf" in str(captured["query"])
    assert "Flipkart" in str(captured["query"])
    assert "flipkart_retail_invoice_bill" in str(captured["query"])
    assert guidance.evidences
    assert guidance.evidences[0]["fields"]["profile_id"] == "flipkart_retail_invoice_bill"
    assert guidance.debug["role"] == "evidence_reviewer"
    assert guidance.debug["source_ids"] == ["rag_flipkart"]
    assert guidance.debug["source_paths"] == ["knowledge/invoice_payment/invoice_reference_profiles.md"]
    assert guidance.debug["locators"] == ["profile_id=flipkart_retail_invoice_bill"]
    assert guidance.debug["profile_ids"] == ["flipkart_retail_invoice_bill"]
    assert "Flipkart invoice profile" in guidance.debug["snippets"][0]
    assert guidance.debug["scores"] == [0.9]


def test_evidence_reviewer_signature_template_query_loads_local_profiles() -> None:
    query = _review_guidance_query(
        "再次审查签名和模板匹配，有没有同厂商样例",
        attachment_context=[{"name": "invoice.pdf", "summary": "Retail Invoice/Bill"}],
        attachment_manifest={},
        extraction_result={},
    )

    assert "signature template visual layout same vendor reference invoice" in query
    assert "case_01_mouadhamri_FACTU2015020048_clean" in query
    assert "case_05_flipkart_ws_retail_invoice_bill" in query
    assert "case_06_sap_dox_invoice_duplicate" in query
    assert "consistency signal" in query


def test_evidence_reviewer_query_uses_actual_semantic_fields_without_universal_control_noise() -> None:
    query = _review_guidance_query(
        "Review this invoice, PO and GRN amount scope",
        attachment_context=[],
        attachment_manifest={},
        extraction_result={
            "source_docs": [
                {
                    "doc_type": "goods_receipt",
                    "field_inventory": [
                        {"field": "coverage", "value": "partial", "status": "present"},
                        {"field": "tax_basis", "value": "net", "status": "present"},
                    ],
                }
            ]
        },
    )

    assert "ap_three_way_matching" in query
    assert "coverage partial present" in query
    assert "tax_basis net present" in query
    assert "segregation of duties payment release vendor master non-PO" not in query


def test_evidence_reviewer_query_keeps_semantic_fields_with_large_attachment_context() -> None:
    query = _review_guidance_query(
        "审查发票三单金额匹配",
        attachment_context=[{"name": f"invoice_{index}.pdf", "summary": "source document " * 80} for index in range(6)],
        attachment_manifest={"attachments": [{"name": f"manifest_{index}.pdf", "summary": "manifest evidence " * 80} for index in range(6)]},
        extraction_result={
            "source_docs": [
                {
                    "doc_type": "goods_receipt",
                    "field_inventory": [
                        {"field": "coverage", "value": "partial", "status": "present"},
                        {"field": "tax_basis", "value": "net", "status": "present"},
                    ],
                }
            ]
        },
    )

    assert "coverage partial present" in query
    assert "tax_basis net present" in query


def test_chinese_three_way_amount_query_does_not_route_to_template_profiles() -> None:
    query = _review_guidance_query(
        "发票三单金额匹配怎么审？",
        attachment_context=[],
        attachment_manifest={},
        extraction_result={},
    )

    assert "ap_three_way_matching" in query
    assert "case_01_mouadhamri" not in query


def test_clear_invoice_query_routes_to_process_boundary_profile() -> None:
    query = _review_guidance_query(
        "Clear Invoice 流程日志是否说明已经可以付款？",
        attachment_context=[],
        attachment_manifest={},
        extraction_result={},
    )

    assert "workflow_boundary_process_evidence" in query


def test_advisor_guidance_provider_retrieves_rag_guidance(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_retrieve(self, **kwargs):  # noqa: ANN001, ANN003
        captured["query"] = kwargs.get("query")
        return SimpleNamespace(
            evidences=[
                SimpleNamespace(
                    model_dump=lambda: {
                        "source_id": "rag_invoice_rules",
                        "source_path": "knowledge/invoice_payment/required_materials.md",
                        "source_type": "markdown",
                        "locator": "profile_id=invoice_only",
                        "snippet": "Invoice-only required fields and optional quality notes.",
                        "fields": {"profile_id": "invoice_only"},
                        "score": 0.8,
                        "channel": "bm25",
                    }
                )
            ]
        )

    monkeypatch.setattr("app.tools.rag_guidance.RagSkill.retrieve", fake_retrieve)
    guidance = advisor_guidance(
        user_question="发票审核缺什么字段？",
        case_state={"requirements": [{"id": "invoice_number", "status": "missing"}]},
        attachment_manifest={},
    )

    assert "invoice_number" in str(captured["query"])
    assert guidance.evidences[0]["fields"]["profile_id"] == "invoice_only"
    assert guidance.debug["role"] == "materials_advisor"
    assert guidance.debug["source_ids"] == ["rag_invoice_rules"]
    assert guidance.debug["source_paths"] == ["knowledge/invoice_payment/required_materials.md"]
    assert guidance.debug["locators"] == ["profile_id=invoice_only"]
    assert guidance.debug["profile_ids"] == ["invoice_only"]
    assert "Invoice-only required fields" in guidance.debug["snippets"][0]
    assert guidance.debug["scores"] == [0.8]


def test_real_guidance_debug_exposes_rag_references_for_specialist_tools(monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_ENABLE_VECTOR", "0")

    advisor = advisor_guidance(
        user_question="invoice payment review needs PO GRN vendor and duplicate payment materials",
        case_state={"requirements": [{"id": "purchase_order", "status": "missing"}]},
        attachment_manifest={},
        top_k=3,
    )
    reviewer = review_guidance(
        user_message="Review SAP invoice duplicate payment and vendor bank change evidence",
        attachment_context=[{"name": "sap_invoice.pdf", "summary": "SAP invoice duplicate payment sample"}],
        attachment_manifest={},
        top_k=3,
    )

    for guidance, role in ((advisor, "materials_advisor"), (reviewer, "evidence_reviewer")):
        assert guidance.evidences
        assert guidance.debug["role"] == role
        assert guidance.debug["source_ids"]
        assert guidance.debug["source_paths"]
        assert guidance.debug["locators"]
        assert guidance.debug["profile_ids"]
        assert guidance.debug["snippets"]
        assert guidance.debug["scores"]
        assert guidance.debug["channels"]
        assert all(str(channel).startswith("txtai_") for channel in guidance.debug["channels"])
