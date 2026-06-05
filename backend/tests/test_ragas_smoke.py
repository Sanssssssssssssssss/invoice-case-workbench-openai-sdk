from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from app.tools.rag_search import RagSkill
from scripts.run_ragas_eval import BACKEND_DIR, MIN_SAMPLES, build_dynamic_ragas_records


def _load_ragas_schema():
    if importlib.util.find_spec("ragas") is None:
        pytest.skip("Install backend/requirements-ragas.txt to run optional RAGAS smoke tests.")

    # RAGAS 0.3.x imports a deprecated VertexAI path at package import time.
    # The smoke only needs RAGAS dataset schemas, not a VertexAI evaluator.
    compat_module = "lang" + "chain_community.chat_models.vertexai"
    module = types.ModuleType(compat_module)
    module.ChatVertexAI = object
    sys.modules.setdefault(compat_module, module)

    from ragas import EvaluationDataset, SingleTurnSample

    return EvaluationDataset, SingleTurnSample


def test_ragas_single_turn_sample_can_wrap_txtai_retrieval(tmp_path, monkeypatch) -> None:
    EvaluationDataset, SingleTurnSample = _load_ragas_schema()
    monkeypatch.delenv("INVOICE_AGENT_ENABLE_VECTOR", raising=False)

    result = RagSkill(knowledge_roots=[Path("knowledge/invoice_payment")], index_root=tmp_path / "rag").retrieve(
        "invoice payment duplicate payment same supplier same amount clearing evidence",
        top_k=3,
    )

    contexts = [item.snippet for item in result.evidences]
    sample = SingleTurnSample(
        user_input="What duplicate-payment controls should the invoice reviewer apply?",
        retrieved_contexts=contexts,
        response=result.answer_context,
        reference="Duplicate-payment review should compare supplier, amount, invoice references, and clearing history.",
    )
    dataset = EvaluationDataset(samples=[sample])

    assert result.status == "success"
    assert {item.channel for item in result.evidences} == {"txtai_hybrid"}
    assert dataset.samples[0].retrieved_contexts == contexts
    assert len(dataset.samples[0].retrieved_contexts) >= 2
    joined = "\n".join(dataset.samples[0].retrieved_contexts).lower()
    assert "duplicate" in joined
    assert "supplier" in joined or "clearing" in joined


def test_dynamic_ragas_dataset_reads_database_and_uses_hybrid_rag() -> None:
    session_db_path = BACKEND_DIR / "storage" / "sessions.sqlite"
    if not session_db_path.exists():
        pytest.skip(f"RAGAS dynamic database is missing: {session_db_path}")

    records = build_dynamic_ragas_records(
        max_samples=8,
        response_source="session",
        top_k=3,
        session_db_path=session_db_path,
    )

    assert len(records) >= MIN_SAMPLES
    assert any(record.source == "database" for record in records)
    assert all(record.user_input for record in records)
    assert all(record.reference for record in records)
    assert all(record.retrieved_contexts for record in records)
    assert all(record.retrieved_context_ids for record in records)
    assert all(record.source_paths for record in records)
    assert all(record.locators for record in records)
    assert all(record.profile_ids for record in records)
    assert all(record.retrieved_evidence for record in records)
    assert all(set(record.channels) == {"txtai_hybrid"} for record in records)
    assert {"duplicate_payment", "bank_change", "materials_required"} & {record.intent for record in records}


def test_dynamic_ragas_dataset_templates_cover_enterprise_control_intents() -> None:
    records = build_dynamic_ragas_records(max_samples=16, top_k=3, session_db_path=BACKEND_DIR / "storage" / "missing.sqlite")
    intents = {record.intent for record in records}

    assert {
        "approval_authority",
        "segregation_of_duties",
        "payment_release",
        "vendor_master_governance",
        "non_po_contract_invoice",
        "tax_gl_coding",
        "exception_hold_tolerance",
    }.issubset(intents)
    enterprise_records = [
        record
        for record in records
        if record.intent
        in {
            "approval_authority",
            "segregation_of_duties",
            "payment_release",
            "vendor_master_governance",
            "non_po_contract_invoice",
            "tax_gl_coding",
            "exception_hold_tolerance",
        }
    ]
    assert enterprise_records
    assert all(record.source == "intent_template" for record in enterprise_records)
    assert all(item.get("locator") for record in records for item in record.retrieved_evidence)
    assert all("snippet" in item for record in records for item in record.retrieved_evidence)
