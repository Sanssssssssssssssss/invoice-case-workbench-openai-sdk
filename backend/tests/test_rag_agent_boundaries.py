from __future__ import annotations

from types import SimpleNamespace

from app.agents.materials_advisor.agent import SYSTEM_PROMPT as MATERIALS_ADVISOR_PROMPT
from app.agents.report_writer.agent import SYSTEM_PROMPT as REPORT_WRITER_PROMPT
from app.tools.rag_guidance import advisor_guidance


def test_report_writer_cannot_retrieve_rag_or_raw_attachment_fields() -> None:
    assert "canonical_consumer_packet" in REPORT_WRITER_PROMPT
    assert "附件 manifest" in REPORT_WRITER_PROMPT
    assert "field inventory" in REPORT_WRITER_PROMPT
    assert "bank details" in REPORT_WRITER_PROMPT
    assert "RAG" in REPORT_WRITER_PROMPT
    assert "它们不会出现在输入中" in REPORT_WRITER_PROMPT
    assert "PDF renderer 会在 canonical 正文后追加带免责声明的“原始材料附录”" in REPORT_WRITER_PROMPT
    assert "仅供人工" in REPORT_WRITER_PROMPT
    assert "不构成系统结论" in REPORT_WRITER_PROMPT


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


def test_real_guidance_debug_exposes_rag_references_for_advisor(monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_ENABLE_VECTOR", "0")

    advisor = advisor_guidance(
        user_question="invoice payment review needs PO GRN vendor and duplicate payment materials",
        case_state={"requirements": [{"id": "purchase_order", "status": "missing"}]},
        attachment_manifest={},
        top_k=3,
    )
    assert advisor.evidences
    assert advisor.debug["role"] == "materials_advisor"
    assert advisor.debug["source_ids"]
    assert advisor.debug["source_paths"]
    assert advisor.debug["locators"]
    assert advisor.debug["profile_ids"]
    assert advisor.debug["snippets"]
    assert advisor.debug["scores"]
    assert advisor.debug["channels"]
    assert all(str(channel).startswith("txtai_") for channel in advisor.debug["channels"])
