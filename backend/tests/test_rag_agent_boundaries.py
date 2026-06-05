from __future__ import annotations

from types import SimpleNamespace

from app.agents.evidence_reviewer.agent import SYSTEM_PROMPT as EVIDENCE_REVIEWER_PROMPT
from app.agents.materials_advisor.agent import SYSTEM_PROMPT as MATERIALS_ADVISOR_PROMPT
from app.agents.report_writer.agent import SYSTEM_PROMPT as REPORT_WRITER_PROMPT
from app.tools.rag_guidance import _review_guidance_query, advisor_guidance, review_guidance


def test_evidence_reviewer_uses_rag_profiles_as_guidance_only() -> None:
    assert "RAG/profile snippets are review guidance, never submitted case evidence" in EVIDENCE_REVIEWER_PROMPT
    assert "Use them to compare required fields, visual/layout clues" in EVIDENCE_REVIEWER_PROMPT
    assert "Do not mark a requirement satisfied because RAG says the template requires it" in EVIDENCE_REVIEWER_PROMPT
    assert "Default invoice-only blocker requirements" in EVIDENCE_REVIEWER_PROMPT
    assert "`required=true`" in EVIDENCE_REVIEWER_PROMPT
    assert "Default invoice-only optional quality requirements" in EVIDENCE_REVIEWER_PROMPT
    assert "`required=false`" in EVIDENCE_REVIEWER_PROMPT
    assert "AP lite profile" in EVIDENCE_REVIEWER_PROMPT
    assert "goods_receipt_or_service_acceptance" in EVIDENCE_REVIEWER_PROMPT
    assert "also bind the current invoice evidence to those proposed requirement ids" in EVIDENCE_REVIEWER_PROMPT
    assert "Do not leave supports empty merely because the requirements are being created in the same patch" in EVIDENCE_REVIEWER_PROMPT
    assert "Do not put PO/GRN/vendor/duplicate-payment gaps" in EVIDENCE_REVIEWER_PROMPT
    assert "next_questions should ask for PO, GRN" not in EVIDENCE_REVIEWER_PROMPT
    assert "source-traceable invoice file and missing required invoice fields" in EVIDENCE_REVIEWER_PROMPT
    assert "do not ask for PO, GRN/service acceptance, vendor identity/master, or duplicate-payment screening unless those AP requirements are already active" in EVIDENCE_REVIEWER_PROMPT
    assert "treat it as an unresolved conflict for the AP lite requirement `duplicate_payment_screen`" in EVIDENCE_REVIEWER_PROMPT
    assert "do not put missing/weak optional fields such as signature" in EVIDENCE_REVIEWER_PROMPT
    assert "optional quality enhancements unless there is a real conflict" in EVIDENCE_REVIEWER_PROMPT
    assert "do not add \"if you need AP review...\" expansion prompts" in EVIDENCE_REVIEWER_PROMPT
    assert "Do not repeat those terms even as exclusions" in EVIDENCE_REVIEWER_PROMPT
    assert "Do not classify a new invoice as cross-case merely because it differs from or resembles a RAG/profile/playbook example" in EVIDENCE_REVIEWER_PROMPT
    assert "the first submitted invoice is the current case source" in EVIDENCE_REVIEWER_PROMPT
    assert "Missing optional signature/template/line item fields are not risk flags" in EVIDENCE_REVIEWER_PROMPT
    assert "Do not calculate a new amount from subtotal/tax/total fields" in EVIDENCE_REVIEWER_PROMPT
    assert "Reviewer Mode Contract v5.0" in EVIDENCE_REVIEWER_PROMPT
    assert "mode=extract" in EVIDENCE_REVIEWER_PROMPT
    assert "mode=review" in EVIDENCE_REVIEWER_PROMPT
    assert "mode=repair" in EVIDENCE_REVIEWER_PROMPT
    assert "Every strong claim must bind to `source_doc_id`, source quote, source locator" in EVIDENCE_REVIEWER_PROMPT
    assert "Visual Check Contract v5.1" in EVIDENCE_REVIEWER_PROMPT
    assert "visual check proves the supplier really exists" in EVIDENCE_REVIEWER_PROMPT
    assert "Do not say visual check proves there is no duplicate payment" in EVIDENCE_REVIEWER_PROMPT
    assert "`extraction_result.visual_check` is required" in EVIDENCE_REVIEWER_PROMPT
    assert "`metadata.visual_check`" in EVIDENCE_REVIEWER_PROMPT
    assert "Signature and Template Consistency v1.0" in EVIDENCE_REVIEWER_PROMPT
    assert "same_vendor_reference" in EVIDENCE_REVIEWER_PROMPT
    assert "Template matching is a consistency signal" in EVIDENCE_REVIEWER_PROMPT
    assert "cannot prove supplier existence" in EVIDENCE_REVIEWER_PROMPT


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
