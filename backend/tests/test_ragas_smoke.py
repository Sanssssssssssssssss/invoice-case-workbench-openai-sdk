from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from app.tools.rag_search import RagSkill


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
    monkeypatch.setenv("INVOICE_AGENT_ENABLE_VECTOR", "0")

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
    assert dataset.samples[0].retrieved_contexts == contexts
    assert len(dataset.samples[0].retrieved_contexts) >= 2
    joined = "\n".join(dataset.samples[0].retrieved_contexts).lower()
    assert "duplicate" in joined
    assert "supplier" in joined or "clearing" in joined
