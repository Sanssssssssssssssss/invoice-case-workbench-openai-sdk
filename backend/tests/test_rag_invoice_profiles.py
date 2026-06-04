from __future__ import annotations

from pathlib import Path

from app.tools.rag_search import RagSkill, rag_search


def _skill(tmp_path, monkeypatch) -> RagSkill:
    monkeypatch.setenv("INVOICE_AGENT_ENABLE_VECTOR", "0")
    return RagSkill(knowledge_roots=[Path("knowledge")], index_root=tmp_path / "rag")


def test_invoice_reference_profiles_retrieve_flipkart_and_sap(tmp_path, monkeypatch) -> None:
    skill = _skill(tmp_path, monkeypatch)

    flipkart = skill.retrieve("Flipkart Retail Invoice Bill authorized signatory invoice number", top_k=5)
    sap = skill.retrieve("SAP invoice validation sample supplier total currency source locator", top_k=5)

    assert flipkart.evidences
    flipkart_joined = "\n".join(item.snippet for item in flipkart.evidences)
    assert "flipkart_retail_invoice_bill" in flipkart_joined
    assert "authorized signatory" in flipkart_joined.lower()
    assert "invoice number" in flipkart_joined.lower()

    assert sap.evidences
    sap_joined = "\n".join(item.snippet for item in sap.evidences)
    assert "sap_dox_invoice_validation_sample" in sap_joined
    assert "supplier" in sap_joined.lower()
    assert "currency" in sap_joined.lower()


def test_invoice_reference_profiles_retrieve_mouadhamri_and_multi_vendor_samples(tmp_path, monkeypatch) -> None:
    skill = _skill(tmp_path, monkeypatch)

    mouadhamri = skill.retrieve("Mouad Hamri FACTU scanned invoice TSV XML OCR annotation", top_k=5)
    multi_vendor = skill.retrieve("invoice2data AWS Azure Coolblue QualityHosting Saeco vendor invoice samples", top_k=5)

    assert mouadhamri.evidences
    assert "mouadhamri_invoice_dataset_FACTU" in "\n".join(item.snippet for item in mouadhamri.evidences)

    assert multi_vendor.evidences
    joined = "\n".join(item.snippet for item in multi_vendor.evidences)
    assert "invoice2data_multi_vendor_samples" in joined
    assert "AmazonWebServices" in joined
    assert "QualityHosting" in joined


def test_six_local_case_template_profiles_are_retrievable(tmp_path, monkeypatch) -> None:
    skill = _skill(tmp_path, monkeypatch)

    cases = {
        "FACTU2015020048 Marc Demo FA02/2015/020059 clean template": "case_01_mouadhamri_FACTU2015020048_clean",
        "FACTU2015040047 Deco Addict amount conflict template": "case_02_mouadhamri_FACTU2015040047_amount_conflict",
        "FACTU2015050046 The Jackson Group duplicate hit template": "case_03_mouadhamri_FACTU2015050046_duplicate_hit",
        "FACTU2015060039 Biotech bank change risk template": "case_04_mouadhamri_FACTU2015060039_bank_change",
        "Flipkart WS Retail Services BLR_WFLD20151000982590 authorized signatory": "case_05_flipkart_ws_retail_invoice_bill",
        "SAP DOX sap_5435569865439 duplicate Electric Installations Urs Schmid": "case_06_sap_dox_invoice_duplicate",
    }

    for query, profile_id in cases.items():
        result = skill.retrieve(query, top_k=5)
        assert result.evidences, query
        assert profile_id in "\n".join(item.snippet for item in result.evidences)


def test_signature_template_query_recalls_local_template_library(tmp_path, monkeypatch) -> None:
    skill = _skill(tmp_path, monkeypatch)

    result = skill.retrieve("again check signature template visual layout same vendor reference invoice", top_k=5)

    joined = "\n".join(item.snippet for item in result.evidences)
    assert "six_case_template_library_boundary" in joined
    assert "consistency signal" in joined
    assert "case_05_flipkart_ws_retail_invoice_bill" in joined
    assert "case_06_sap_dox_invoice_duplicate" in joined


def test_flipkart_and_sap_case_queries_prioritize_case_profiles(tmp_path, monkeypatch) -> None:
    skill = _skill(tmp_path, monkeypatch)

    flipkart = skill.retrieve("Flipkart WS Retail BLR_WFLD20151000982590 Authorized Signatory", top_k=3)
    sap = skill.retrieve("SAP DOX 5435569865439 Electric Installations Urs Schmid EUR", top_k=3)

    assert "case_05_flipkart_ws_retail_invoice_bill" in flipkart.evidences[0].snippet
    assert "case_06_sap_dox_invoice_duplicate" in sap.evidences[0].snippet


def test_generic_invoice_rubric_does_not_default_to_ap_materials(tmp_path, monkeypatch) -> None:
    skill = _skill(tmp_path, monkeypatch)

    result = skill.retrieve("generic single invoice rubric source traceability signature line items", top_k=3)

    assert result.evidences
    joined = "\n".join(item.snippet for item in result.evidences)
    assert "generic_single_invoice_rubric" in joined
    assert "ap_three_way_matching" not in result.evidences[0].snippet


def test_external_dataset_notes_recall_hugging_face_sources(tmp_path, monkeypatch) -> None:
    skill = _skill(tmp_path, monkeypatch)

    result = skill.retrieve("Hugging Face invoice dataset OCR markdown JSON visual verification benchmark", top_k=5)

    assert result.evidences
    joined = "\n".join(item.snippet for item in result.evidences)
    assert "external_invoice_dataset_notes" in joined
    assert "Lukaszl/clearocr-invoice-document-ai" in joined
    assert "jngb-labs/InvoiceBenchmark" in joined


def test_rag_search_ignores_extra_planner_keys(monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_ENABLE_VECTOR", "0")

    result = rag_search(query="Flipkart invoice profile", context="planner extra context", top_k=1)

    assert result["status"] in {"success", "partial", "not_found"}
