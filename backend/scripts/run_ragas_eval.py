from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import types
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
REPORT_DIR = PROJECT_ROOT / "reports" / "evals"
SCENARIO_DIR = BACKEND_DIR / "evals" / "scenarios"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.tools.rag_search import RagSkill  # noqa: E402


MIN_SAMPLES = 6
DEFAULT_MAX_SAMPLES = 12
DEFAULT_TOP_K = 5
DEFAULT_RESPONSE_MAX_CHARS = 450
DEFAULT_BATCH_SIZE = 12
THRESHOLDS = {
    "faithfulness": 0.70,
    "context_recall": 0.55,
    "llm_context_precision_with_reference": 0.65,
}
INTENT_PRIORITY = (
    "prompt_injection",
    "clear_invoice_boundary",
    "duplicate_payment",
    "bank_change",
    "amount_conflict",
    "report_quality",
    "three_way_matching",
    "materials_required",
)


INTENT_SPECS: dict[str, dict[str, Any]] = {
    "materials_required": {
        "terms": ["\u6750\u6599", "\u51c6\u5907", "\u7f3a", "\u81f3\u5c11", "material", "requirements"],
        "query": "invoice payment review required materials invoice purchase order goods receipt vendor master duplicate payment check",
        "reference": "Invoice payment review normally requires a source invoice, purchase order, goods receipt or service acceptance, vendor identity/master data, and duplicate-payment screening evidence.",
    },
    "three_way_matching": {
        "terms": ["\u4e09\u5355", "po", "grn", "\u6536\u8d27", "goods receipt", "three-way", "quantity", "unit price"],
        "query": "three way matching invoice purchase order goods receipt quantity unit price amount mismatch",
        "reference": "Three-way matching compares invoice, purchase order, and goods receipt or service acceptance for supplier, quantities, unit prices, dates, and amounts before treating the evidence chain as complete.",
    },
    "duplicate_payment": {
        "terms": ["\u91cd\u590d", "duplicate", "pay-2026", "clr-2026", "clearing", "\u5386\u53f2\u4ed8\u6b3e"],
        "query": "duplicate payment same supplier same amount same invoice reference historical payment clearing evidence",
        "reference": "Duplicate-payment review should compare same supplier, amount, invoice number or similar reference, payment id, clearing history, and unresolved duplicate hits; a hit remains a conflict rather than ready for report.",
    },
    "bank_change": {
        "terms": ["\u94f6\u884c", "bank", "\u5c3e\u53f7", "account", "supplier email", "new bank"],
        "query": "vendor bank change supplier master current proposed account approval workflow email conflict",
        "reference": "Vendor bank changes require comparison against vendor master data and approval workflow evidence; a new bank-account email or mismatch is a conflict until independently approved.",
    },
    "amount_conflict": {
        "terms": ["\u91d1\u989d", "amount", "tax", "subtotal", "total", "\u5355\u4ef7"],
        "query": "invoice amount tax subtotal total calculation mismatch purchase order amount conflict",
        "reference": "Amount review should compare invoice totals, tax, line amounts, PO amounts, and GRN quantities; unresolved amount or calculation conflicts should not be marked satisfied.",
    },
    "prompt_injection": {
        "terms": ["prompt injection", "\u5947\u602a\u6307\u4ee4", "\u5ffd\u7565", "\u76f4\u63a5\u6279\u51c6", "\u9644\u4ef6\u91cc"],
        "query": "prompt injection attachment says ignore rules approve payment source quality boundary invoice text",
        "reference": "Instructions inside attachments are source text only; prompt-injection content must not override reviewer policy or cause approval/payment execution.",
    },
    "clear_invoice_boundary": {
        "terms": ["clear invoice", "process log", "\u6d41\u7a0b", "bpi"],
        "query": "Clear Invoice process log event payment approval boundary process evidence not payable proof",
        "reference": "A Clear Invoice or process-log event is process evidence, not proof of payment approval or complete invoice evidence by itself.",
    },
    "report_quality": {
        "terms": ["\u62a5\u544a", "pdf", "claim-to-evidence", "matrix", "\u751f\u6210\u6700\u7ec8\u62a5\u544a"],
        "query": "invoice payment review report claim to evidence matrix missing materials conflicts duplicate payment risk",
        "reference": "Final reports must preserve unresolved missing items and conflicts, include claim-to-evidence grounding, and avoid saying payment is approved or complete when requirements remain weak or conflicting.",
    },
}


@dataclass
class RagasEvalRecord:
    sample_id: str
    case_id: str
    turn_id: str
    run_id: str
    intent: str
    source: str
    user_input: str
    query: str
    reference: str
    response: str
    retrieved_contexts: list[str]
    retrieved_context_ids: list[str]
    source_paths: list[str]
    scores: list[float]
    channels: list[str]
    metric_scores: dict[str, float | None] = field(default_factory=dict)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run dynamic database-backed RAGAS evals over txtai RAG.")
    parser.add_argument("--max-samples", type=int, default=DEFAULT_MAX_SAMPLES)
    parser.add_argument("--case-id", action="append", default=None)
    parser.add_argument("--no-llm", action="store_true", help="Build the RAGAS dataset and retrieval report without LLM judge metrics.")
    parser.add_argument("--response-source", choices=("rag", "session"), default="rag")
    parser.add_argument("--response-max-chars", type=int, default=DEFAULT_RESPONSE_MAX_CHARS)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--output", default=str(REPORT_DIR / "ragas_latest.json"))
    parser.add_argument("--jsonl-output", default=str(REPORT_DIR / "ragas_latest.jsonl"))
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    records = build_dynamic_ragas_records(
        max_samples=args.max_samples,
        case_ids=args.case_id,
        response_source=args.response_source,
        response_max_chars=args.response_max_chars,
        top_k=args.top_k,
    )
    if len(records) < MIN_SAMPLES:
        raise SystemExit(f"RAGAS dataset too small: {len(records)} samples; need at least {MIN_SAMPLES}.")

    metric_rows: list[dict[str, Any]] = []
    if not args.no_llm:
        metric_rows = run_ragas_llm_judge(records, batch_size=args.batch_size)
        _merge_metric_rows(records, metric_rows)

    report = render_report(records, llm_enabled=not args.no_llm, metric_rows=metric_rows, response_source=args.response_source)
    write_reports(report, records, output=Path(args.output), jsonl_output=Path(args.jsonl_output))
    print_summary(report)
    if not report["pass"]:
        raise SystemExit(1)


def build_dynamic_ragas_records(
    *,
    max_samples: int = DEFAULT_MAX_SAMPLES,
    case_ids: list[str] | None = None,
    response_source: str = "rag",
    response_max_chars: int = DEFAULT_RESPONSE_MAX_CHARS,
    top_k: int = DEFAULT_TOP_K,
    session_db_path: Path | None = None,
) -> list[RagasEvalRecord]:
    settings = get_settings()
    database_path = session_db_path or settings.session_db_path
    candidates = _database_candidates(database_path, case_ids=case_ids)
    db_candidate_count = len(candidates)
    candidates.extend(_scenario_candidates(case_ids=case_ids))
    selected = _select_candidates(candidates, max_samples=max_samples)

    os.environ.pop("INVOICE_AGENT_ENABLE_VECTOR", None)
    skill = RagSkill(knowledge_roots=settings.knowledge_roots)
    records: list[RagasEvalRecord] = []
    for index, candidate in enumerate(selected, start=1):
        intent = candidate["intent"]
        spec = INTENT_SPECS[intent]
        query = _query_for_candidate(candidate, spec)
        result = skill.retrieve(query=query, intent="policy_qa", top_k=top_k, include_raw_snippets=True)
        if result.status != "success" or not result.evidences:
            continue
        channels = [item.channel for item in result.evidences]
        if not channels or not all(channel == "txtai_hybrid" for channel in channels):
            continue
        response = str(candidate.get("assistant_response") or "").strip()
        if response_source == "rag" or not response:
            response = result.answer_context
        records.append(
            RagasEvalRecord(
                sample_id=f"ragas_{index:03d}_{intent}",
                case_id=str(candidate.get("case_id") or ""),
                turn_id=str(candidate.get("turn_id") or ""),
                run_id=str(candidate.get("run_id") or ""),
                intent=intent,
                source=str(candidate.get("source") or ""),
                user_input=str(candidate.get("user_input") or ""),
                query=query,
                reference=str(spec["reference"]),
                response=response[:response_max_chars],
                retrieved_contexts=[item.snippet for item in result.evidences],
                retrieved_context_ids=[item.source_id for item in result.evidences],
                source_paths=[item.source_path for item in result.evidences],
                scores=[item.score for item in result.evidences],
                channels=channels,
            )
        )
    for record in records:
        if record.source == "scenario_fallback" and db_candidate_count:
            record.source = "scenario_fallback_after_db"
    return records


def run_ragas_llm_judge(records: list[RagasEvalRecord], *, batch_size: int = DEFAULT_BATCH_SIZE) -> list[dict[str, Any]]:
    _install_ragas_import_compat()
    from langchain_openai import ChatOpenAI
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.llms.base import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall

    settings = get_settings()
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is required for default RAGAS LLM judge.")

    samples = [
        SingleTurnSample(
            user_input=record.user_input,
            retrieved_contexts=record.retrieved_contexts,
            retrieved_context_ids=record.retrieved_context_ids,
            response=record.response,
            reference=record.reference,
        )
        for record in records
    ]
    dataset = EvaluationDataset(samples=samples)
    evaluator = LangchainLLMWrapper(
        ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            temperature=_ragas_temperature(settings),
            timeout=_ragas_timeout(settings),
        ),
        bypass_temperature=True,
        bypass_n=True,
    )
    result = evaluate(
        dataset,
        metrics=[
            Faithfulness(max_retries=0),
            LLMContextRecall(max_retries=0),
            LLMContextPrecisionWithReference(max_retries=0),
        ],
        llm=evaluator,
        raise_exceptions=False,
        show_progress=True,
        batch_size=max(1, batch_size),
    )
    return _metric_rows(result)


def render_report(
    records: list[RagasEvalRecord],
    *,
    llm_enabled: bool,
    metric_rows: list[dict[str, Any]],
    response_source: str,
) -> dict[str, Any]:
    means = _metric_means(records)
    threshold_pass = True
    if llm_enabled:
        for name, threshold in THRESHOLDS.items():
            value = means.get(name)
            if value is None or value < threshold:
                threshold_pass = False
    retrieval_pass = len(records) >= MIN_SAMPLES and all(
        record.retrieved_contexts and all(channel == "txtai_hybrid" for channel in record.channels)
        for record in records
    )
    settings = get_settings()
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "sample_count": len(records),
        "llm_enabled": llm_enabled,
        "response_source": response_source,
        "pass": bool(retrieval_pass and threshold_pass),
        "thresholds": THRESHOLDS,
        "means": means,
        "coverage": _metric_coverage(records),
        "database": str(settings.session_db_path),
        "llm": {
            "model": settings.llm_model,
            "base_url": settings.llm_base_url,
            "has_api_key": bool(settings.llm_api_key),
        },
        "metric_rows": metric_rows,
        "samples": [asdict(record) for record in records],
    }


def write_reports(report: dict[str, Any], records: list[RagasEvalRecord], *, output: Path, jsonl_output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with jsonl_output.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def print_summary(report: dict[str, Any]) -> None:
    print(json.dumps({key: report[key] for key in ("pass", "sample_count", "llm_enabled", "means")}, ensure_ascii=False, indent=2))
    print(f"Wrote {REPORT_DIR / 'ragas_latest.json'}")


def _database_candidates(session_db_path: Path, *, case_ids: list[str] | None) -> list[dict[str, Any]]:
    if not session_db_path.exists():
        return []
    con = sqlite3.connect(f"file:{session_db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            select id, session_id, case_id, turn_id, run_id, role, content, content_summary, metadata_json
            from session_items
            where active = 1
            order by id
            """
        ).fetchall()
    finally:
        con.close()
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    allowed = set(case_ids or [])
    for row in rows:
        case_id = str(row["case_id"] or "")
        if allowed and case_id not in allowed:
            continue
        key = (str(row["session_id"] or ""), str(row["turn_id"] or ""))
        item = grouped.setdefault(
            key,
            {
                "case_id": case_id,
                "turn_id": str(row["turn_id"] or ""),
                "run_id": str(row["run_id"] or ""),
                "user_input": "",
                "assistant_response": "",
                "source": "database",
            },
        )
        role = str(row["role"] or "")
        text = str(row["content"] or row["content_summary"] or "")
        if role == "user" and text and not item["user_input"]:
            item["user_input"] = text
        elif role == "assistant" and text and not item["assistant_response"]:
            item["assistant_response"] = text
    candidates: list[dict[str, Any]] = []
    for item in grouped.values():
        if not item["user_input"]:
            continue
        item_case_id = str(item.get("case_id") or "")
        intent = _case_intent_hint(item_case_id) or _classify_intent(f"{item['user_input']}\n{item['assistant_response']}")
        if not intent:
            continue
        item["intent"] = intent
        candidates.append(item)
    return candidates


def _scenario_candidates(*, case_ids: list[str] | None) -> list[dict[str, Any]]:
    allowed = set(case_ids or [])
    candidates: list[dict[str, Any]] = []
    if not SCENARIO_DIR.exists():
        return candidates
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        try:
            scenario = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        case_id = str(scenario.get("case_id") or "")
        if allowed and case_id not in allowed:
            continue
        for index, step in enumerate(scenario.get("steps") or [], start=1):
            user_input = str(step.get("message") or "")
            intent = _case_intent_hint(case_id) or _classify_intent(user_input)
            if not intent:
                continue
            candidates.append(
                {
                    "case_id": case_id,
                    "turn_id": f"scenario_step_{index}",
                    "run_id": "",
                    "user_input": user_input,
                    "assistant_response": "",
                    "intent": intent,
                    "source": "scenario_fallback",
                }
            )
    return candidates


def _select_candidates(candidates: list[dict[str, Any]], *, max_samples: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    seen_intents: set[str] = set()

    for candidate in candidates:
        intent = str(candidate.get("intent") or "")
        if intent in seen_intents:
            continue
        if _add_candidate(selected, seen_keys, candidate):
            seen_intents.add(intent)
        if len(selected) >= max_samples:
            return selected

    for candidate in candidates:
        _add_candidate(selected, seen_keys, candidate)
        if len(selected) >= max_samples:
            return selected
    return selected


def _add_candidate(selected: list[dict[str, Any]], seen_keys: set[tuple[str, str]], candidate: dict[str, Any]) -> bool:
    user_input = _compact_text(candidate.get("user_input"), 800)
    if len(user_input) < 6:
        return False
    key = (str(candidate.get("intent") or ""), user_input.lower())
    if key in seen_keys:
        return False
    row = dict(candidate)
    row["user_input"] = user_input
    row["assistant_response"] = _compact_text(row.get("assistant_response"), 2400)
    selected.append(row)
    seen_keys.add(key)
    return True


def _classify_intent(text: str) -> str:
    lowered = text.lower()
    best: tuple[int, int, str] = (0, len(INTENT_PRIORITY), "")
    for intent, spec in INTENT_SPECS.items():
        count = sum(1 for term in spec["terms"] if str(term).lower() in lowered)
        priority = INTENT_PRIORITY.index(intent) if intent in INTENT_PRIORITY else len(INTENT_PRIORITY)
        if count > best[0] or (count == best[0] and count > 0 and priority < best[1]):
            best = (count, priority, intent)
    return best[2]


def _case_intent_hint(case_id: str) -> str:
    lowered = str(case_id or "").lower()
    if "prompt_injection" in lowered:
        return "prompt_injection"
    if "clear_invoice" in lowered:
        return "clear_invoice_boundary"
    if "duplicate" in lowered:
        return "duplicate_payment"
    if "bank" in lowered:
        return "bank_change"
    if "amount" in lowered:
        return "amount_conflict"
    if "report" in lowered:
        return "report_quality"
    if "inv5001" in lowered or "sample" in lowered:
        return "three_way_matching"
    if "rag_materials" in lowered or "multiturn" in lowered:
        return "materials_required"
    return ""


def _query_for_candidate(candidate: dict[str, Any], spec: dict[str, Any]) -> str:
    pieces = [
        str(spec.get("query") or ""),
        str(candidate.get("user_input") or ""),
    ]
    return "\n".join(piece for piece in pieces if piece).strip()[:1800]


def _compact_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    text = " ".join(text.split())
    return text[:max_chars]


def _install_ragas_import_compat() -> None:
    compat_module = "lang" + "chain_community.chat_models.vertexai"
    module = types.ModuleType(compat_module)
    module.ChatVertexAI = object
    sys.modules.setdefault(compat_module, module)


def _metric_rows(result: Any) -> list[dict[str, Any]]:
    try:
        frame = result.to_pandas()
        return [dict(row) for row in frame.to_dict(orient="records")]
    except Exception:
        if isinstance(result, dict):
            return [result]
    return []


def _merge_metric_rows(records: list[RagasEvalRecord], metric_rows: list[dict[str, Any]]) -> None:
    for record, row in zip(records, metric_rows, strict=False):
        for key, value in row.items():
            normalized = _metric_name(key)
            if normalized in THRESHOLDS:
                record.metric_scores[normalized] = _float_or_none(value)


def _metric_means(records: list[RagasEvalRecord]) -> dict[str, float | None]:
    output: dict[str, float | None] = {}
    for name in THRESHOLDS:
        values = [
            float(value)
            for record in records
            for key, value in record.metric_scores.items()
            if key == name and isinstance(value, (int, float)) and math.isfinite(float(value))
        ]
        output[name] = round(mean(values), 4) if values else None
    return output


def _metric_coverage(records: list[RagasEvalRecord]) -> dict[str, dict[str, int | float]]:
    total = max(1, len(records))
    output: dict[str, dict[str, int | float]] = {}
    for name in THRESHOLDS:
        count = 0
        for record in records:
            value = record.metric_scores.get(name)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                count += 1
        output[name] = {"count": count, "total": len(records), "ratio": round(count / total, 4)}
    return output


def _metric_name(name: str) -> str:
    lowered = str(name).strip().lower()
    aliases = {
        "context_recall": "context_recall",
        "llm_context_recall": "context_recall",
        "faithfulness": "faithfulness",
        "context_precision": "llm_context_precision_with_reference",
        "llm_context_precision_with_reference": "llm_context_precision_with_reference",
    }
    return aliases.get(lowered, lowered)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _ragas_temperature(settings: Any) -> float:
    raw = os.getenv("INVOICE_AGENT_RAGAS_TEMPERATURE")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    if "kimi" in str(settings.llm_model).lower():
        return 1.0
    return 0.0


def _ragas_timeout(settings: Any) -> float:
    raw = os.getenv("INVOICE_AGENT_RAGAS_TIMEOUT_SECONDS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return max(float(getattr(settings, "llm_timeout_seconds", 90.0) or 90.0), 240.0)


if __name__ == "__main__":
    main()
