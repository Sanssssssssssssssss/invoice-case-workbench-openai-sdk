from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.capabilities import role_input_keys
from app.compiler_runtime.consumer import derive_consumer_packet, finalize_consumer_report
from app.domain.invoice_requirements import (
    AUTO_DERIVED_COMPILER_REQUIREMENTS,
    REQUIREMENT_CATALOG_VERSION,
    REQUIREMENT_PROFILES,
)
from app.state.attachment_manifest import attachment_manifest_for_context, update_manifest_summaries
from app.state.artifact_store import ArtifactStore
from app.guards import enforce_case_state_consistency, enforce_no_execution_wording, enforce_report_proof_consistency
from app.llm import LlmClient
from app.memory_service import MemoryService
from app.runtime import policy_gate as route_policy
from app.runtime.context_partition import (
    PARTITION_MANIFEST_KEYS,
    PARTITION_TOP_LEVEL_KEYS,
    PARTITION_USAGE_KEYS,
    runtime_partition_metadata,
)
from app.prompt_loader import load_prompt, load_system_prompt
from app.state.session_repository import SessionRepository
from app.state.case_store import CaseStore
from app.tools.file_workspace import report_paths_for_run
from app.tools.rag_guidance import advisor_guidance


SUMMARY_THRESHOLD = 2000
SUMMARY_INPUT_STRING_LIMIT = 1400
SUMMARY_INPUT_HEAD_CHARS = 900
SUMMARY_INPUT_TAIL_CHARS = 360
SUMMARY_DEDUPE_MIN_CHARS = 500
LLM_SUMMARIZER_ARTIFACT_TYPES: set[str] = set()
PROMPT_INJECTION_DETAIL_RE = re.compile(
    r"[^。\n；;]{0,40}(?:提示注入|prompt injection|越权执行性指令|忽略规则|ignore previous rules|虚假声明|诱导)[^。\n；;]{0,160}",
    re.I,
)
class SummaryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = ""
    key_facts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_items: list[str] = Field(default_factory=list)
    next_action_hint: str = ""
    must_preserve_refs: list[str] = Field(default_factory=list)


SUMMARIZER_PROMPT = (
    load_system_prompt("agents/summarizer/prompt.md").rstrip()
    + "\n\n---\n\n"
    + load_prompt("agents/summarizer/checkpoint_skill.md").lstrip()
    + "\n\n---\n\n"
    + load_prompt("agents/summarizer/attachment_manifest_skill.md").lstrip()
)


class ContextManager:
    def __init__(
        self,
        store: CaseStore,
        llm: LlmClient,
        sessions: SessionRepository | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self.store = store
        self.llm = llm
        self.sessions = sessions or SessionRepository(store)
        self.memory = memory or MemoryService(store)
        self.artifacts = ArtifactStore(store)
        self._pending_rag_debug: dict[str, list[dict[str, Any]]] = {}

    def record_result(self, state: Any, *, kind: str, name: str, result: Any) -> dict[str, Any]:
        artifact_type = _artifact_type(kind, name)
        artifact_ref = self.artifacts.save(state.case_id, state.run_id, artifact_type, name, result)
        summary = self._summarize(artifact_type=artifact_type, name=name, result=result)
        if artifact_type == "attachment_batch" and isinstance(result, dict):
            state.observability["latest_attachment_batch_ref"] = artifact_ref
            refs = state.observability.setdefault("attachment_batch_refs", [])
            if isinstance(refs, list):
                refs.append(artifact_ref)
            update_manifest_summaries(
                self.store,
                state.case_id,
                artifact_ref=artifact_ref,
                summaries=self._attachment_manifest_summaries(result),
            )
        hint = route_policy.next_action_hint(kind, name, result)
        observation = {
            "kind": kind,
            "name": name,
            "summary": summary.summary,
            "key_facts": summary.key_facts,
            "risks": summary.risks,
            "missing_items": summary.missing_items,
            "next_action_hint": hint,
            "must_preserve_refs": summary.must_preserve_refs,
            "artifact_ref": artifact_ref,
        }
        if kind == "role" and name == "evidence_reviewer" and isinstance(result, dict):
            observation["reviewer_mode"] = str(result.get("mode") or "review")
        return observation

    def record_error(self, *, kind: str, name: str, exc: Exception) -> dict[str, Any]:
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        runtime_feedback = classify_runtime_error(kind=kind, name=name, error=error)
        return {
            "kind": kind,
            "name": name,
            "summary": f"{name} failed: {type(exc).__name__}: {exc}",
            "key_facts": [],
            "risks": [f"{type(exc).__name__}: {exc}"],
            "missing_items": [],
            "next_action_hint": str(runtime_feedback.get("recommended_action") or "retry_or_final_answer"),
            "must_preserve_refs": [],
            "error": error,
            "runtime_feedback": runtime_feedback,
        }

    def build_planner_context(
        self,
        *,
        state: Any,
        case_state: Any,
        session: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        observations = _planner_observations(state.observations)
        session_data = session or self.sessions.load_session(state.case_id)
        prior_turns = _prior_recent_turns(session_data.get("turns") or [], getattr(state, "turn_id", ""), limit=3)
        runtime_feedback = self.last_runtime_feedback(state)
        if not runtime_feedback and int(getattr(state, "max_steps", 0) or 0) - int(getattr(state, "step_count", 0) or 0) <= 1:
            runtime_feedback = step_budget_runtime_feedback()
        report_markdown_path, report_pdf_path = report_paths_for_run(getattr(state, "started_at", ""))
        consumer_packet = derive_consumer_packet(case_state)
        planner_context = {
            "case_brief": _case_brief(case_state),
            "requirement_catalog": {
                "version": REQUIREMENT_CATALOG_VERSION,
                "profiles": {
                    profile_id: [
                        str(item["id"])
                        for item in rows
                        if str(item["id"]) not in AUTO_DERIVED_COMPILER_REQUIREMENTS
                    ]
                    for profile_id, rows in REQUIREMENT_PROFILES.items()
                },
            },
            "case_profile": getattr(case_state, "case_profile", {}) or {},
            "case_next_action_hint": getattr(case_state, "next_action_hint", "") or "",
            "reply_brief": _planner_safe_text(getattr(case_state, "reply_brief", "") or "", max_chars=700),
            "session_summary": _planner_safe_text(session_data.get("session_summary") or "", max_chars=900),
            "recent_turns": _planner_recent_turns(prior_turns),
            "memory_hints": self._memory_hints_for_context(
                case_id=state.case_id,
                query=f"{state.user_message_for_planner} {getattr(case_state, 'summary', '')}",
                case_state=case_state,
                section="memory_hints",
                limit=3,
                max_total_chars=500,
                min_score=0.15,
            ),
            "current_goal": state.current_goal,
            "current_plan": _latest_plan(state),
            "attachments": [_attachment_meta(item) for item in attachments],
            "attachment_manifest": attachment_manifest_for_context(self.store, state.case_id),
            "recent_observations": observations,
            "open_questions": list(getattr(case_state, "next_questions", []) or []),
            "next_expected_action": str(runtime_feedback.get("recommended_action") or _latest_next_action_hint(observations)),
            "runtime_feedback": runtime_feedback,
            "report_paths": {"markdown_path": report_markdown_path, "pdf_path": report_pdf_path},
        }
        if consumer_packet.root_decisions:
            proof_observations = _proof_consumer_observations(observations)
            safe_feedback = _safe_runtime_feedback(planner_context["runtime_feedback"])
            planner_context = {
                "case_brief": planner_context["case_brief"],
                "requirement_catalog": planner_context["requirement_catalog"],
                "canonical_consumer_packet": consumer_packet.model_dump(mode="json"),
                "current_goal": planner_context["current_goal"],
                "attachments": [_safe_attachment_identity(item) for item in attachments],
                "attachment_manifest": _safe_attachment_manifest_identity(
                    planner_context["attachment_manifest"]
                ),
                "recent_observations": proof_observations,
                "next_expected_action": str(
                    safe_feedback.get("recommended_action")
                    or _latest_next_action_hint(proof_observations)
                ),
                "runtime_feedback": safe_feedback,
                "report_paths": planner_context["report_paths"],
            }
        return planner_context

    def build_role_context(
        self,
        *,
        role: str,
        state: Any,
        payload: dict[str, Any],
        user_message: str,
        case_state: Any,
    ) -> dict[str, Any]:
        hydrated = dict(payload)
        if role == "report_writer":
            packet = derive_consumer_packet(case_state)
            report_payload = {
                "canonical_consumer_packet": packet.model_dump(mode="json"),
                "user_request": user_message,
            }
            return _filter_role_payload(role, report_payload)
        hydrated["case_state"] = (
            _advisor_case_state(case_state)
            if role == "materials_advisor"
            else _sanitize_case_state(case_state)
        )
        attachment_manifest = attachment_manifest_for_context(self.store, state.case_id)
        hydrated["attachment_manifest"] = (
            _advisor_attachment_manifest(attachment_manifest)
            if role == "materials_advisor"
            else attachment_manifest
        )
        hydrated["memory_hints"] = self._memory_hints_for_context(
            case_id=state.case_id,
            query=f"{user_message} {getattr(case_state, 'summary', '')}",
            case_state=case_state,
            section="memory_hints",
            limit=5,
            max_total_chars=1200,
            min_score=0.10,
        )
        if role == "materials_advisor":
            hydrated.setdefault("user_question", hydrated.pop("question", user_message))
            if not hydrated.get("rag_context"):
                guidance = advisor_guidance(
                    str(hydrated.get("user_question") or user_message),
                    hydrated.get("case_state") if isinstance(hydrated.get("case_state"), dict) else {},
                    hydrated.get("attachment_manifest") if isinstance(hydrated.get("attachment_manifest"), dict) else {},
                )
                hydrated["rag_context"] = guidance.evidences
                self._queue_rag_debug(state, role, guidance.debug)
        elif role == "case_patch_writer":
            hydrated.setdefault("role_result", self.last_evidence_reviewer_result(state, mode=("review", "repair")) or self.last_role_result(state))
            hydrated.setdefault("user_message", user_message)
        return _filter_role_payload(role, hydrated)

    def pop_pending_rag_debug(self, state: Any, role: str) -> list[dict[str, Any]]:
        key = f"{getattr(state, 'run_id', '')}:{role}"
        return self._pending_rag_debug.pop(key, [])

    def _queue_rag_debug(self, state: Any, role: str, debug: dict[str, Any]) -> None:
        if not debug:
            return
        key = f"{getattr(state, 'run_id', '')}:{role}"
        self._pending_rag_debug.setdefault(key, []).append(debug)

    def user_message_for_role(self, state: Any, fallback: str) -> str:
        ref = getattr(state, "user_message_artifact_ref", "")
        if not ref:
            return fallback
        try:
            payload = self.artifacts.read(state.case_id, ref)
        except Exception:
            return fallback
        if isinstance(payload, dict):
            return str(payload.get("message") or fallback)
        return fallback

    def resolve_content_ref(self, case_id: str, state: Any, content_ref: str) -> str:
        ref = str(content_ref or "").strip()
        if not ref:
            return ""
        if ref.startswith("last_role:"):
            selector = ref.removeprefix("last_role:")
            role_name, _, field = selector.partition(".")
            payload = self._latest_payload(state, kind="role", name=role_name)
            if isinstance(payload, dict) and field:
                value = payload.get(field)
                if value is not None:
                    text = str(value)
                    if role_name == "report_writer" and field == "markdown":
                        case_state = self.store.load(case_id)
                        packet = derive_consumer_packet(case_state)
                        text = finalize_consumer_report(text, packet)
                        text = _apply_report_instruction_appendix(text, getattr(state, "user_message_for_planner", ""))
                        text = _sanitize_report_markdown_for_guards(text, case_state)
                        text = enforce_no_execution_wording(text)
                        text = enforce_report_proof_consistency(text, case_state)
                        text = enforce_case_state_consistency(text, case_state)
                    return text
        raise ValueError(f"Unsupported content_ref: {content_ref}")

    def write_context_manifest(
        self,
        state: Any,
        *,
        target: str,
        context_payload: Any,
        included: list[str],
        excluded: list[str],
        artifact_refs: list[str] | None = None,
        blocked_raw_content: bool = True,
        model: str = "",
        prompt_file: str = "",
        system_prompt: str = "",
        budget: dict[str, Any] | None = None,
        raw_leak_checks: list[str] | None = None,
        compact_triggered: bool = False,
        metadata: dict[str, Any] | None = None,
        partition_metadata: dict[str, Any] | None = None,
    ) -> str:
        step = max(int(getattr(state, "step_count", 0)), 0)
        safe_target = _safe_slug(target.replace(":", "_"))
        relative_path = f"traces/{state.run_id}/context_manifest_{step:03d}_{safe_target}.json"
        target_path = self.store.resolve_case_path(state.case_id, relative_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(context_payload, ensure_ascii=False, default=str)
        partition = dict(partition_metadata or {})
        payload = {
            "target": target,
            "model": model,
            "prompt_file": prompt_file,
            "prompt_sha": _sha256(system_prompt),
            "payload_sha256": _sha256(text),
            "payload_preview": text[:1600],
            "payload": context_payload,
            "included": included,
            "excluded": excluded,
            "artifact_refs": artifact_refs or _artifact_refs_from_context(context_payload),
            "estimated_chars": len(text),
            "blocked_raw_content": blocked_raw_content,
            "budget": budget or {},
            "raw_leak_checks": raw_leak_checks or [],
            "compact_triggered": compact_triggered,
            "metadata": metadata or {},
            "prompt_partition": partition,
        }
        payload.update({key: partition.get(key) for key in PARTITION_TOP_LEVEL_KEYS if key in partition})
        payload.update({key: partition.get(key) for key in PARTITION_MANIFEST_KEYS if key in partition})
        target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        manifests = getattr(state, "observability", {}).setdefault("context_manifests", {})
        if isinstance(manifests, dict):
            manifests[target] = relative_path
        partitions = getattr(state, "observability", {}).setdefault("prompt_partitions", {})
        if isinstance(partitions, dict) and partition:
            partitions[target] = runtime_partition_metadata(partition)
        return relative_path

    def update_context_manifest_usage(self, state: Any, target: str, usage_metadata: dict[str, Any]) -> None:
        manifests = getattr(state, "observability", {}).get("context_manifests")
        ref = manifests.get(target) if isinstance(manifests, dict) else ""
        if not ref:
            return
        try:
            path = self.store.resolve_case_path(state.case_id, str(ref))
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        partition = payload.get("prompt_partition") if isinstance(payload.get("prompt_partition"), dict) else {}
        partition.update({key: usage_metadata.get(key) for key in PARTITION_USAGE_KEYS if key in usage_metadata})
        payload["prompt_partition"] = partition
        for key in PARTITION_USAGE_KEYS:
            if key in usage_metadata:
                payload[key] = usage_metadata[key]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        partitions = getattr(state, "observability", {}).setdefault("prompt_partitions", {})
        if isinstance(partitions, dict):
            current = partitions.get(target) if isinstance(partitions.get(target), dict) else {}
            current.update(runtime_partition_metadata(partition))
            partitions[target] = current

    def prompt_partition_metadata(self, state: Any, target: str) -> dict[str, Any]:
        partitions = getattr(state, "observability", {}).get("prompt_partitions")
        value = partitions.get(target) if isinstance(partitions, dict) else {}
        return runtime_partition_metadata(value) if isinstance(value, dict) else {}

    def last_role_result(self, state: Any, name: str | None = None) -> dict[str, Any]:
        result = self._latest_payload(state, kind="role", name=name)
        return result if isinstance(result, dict) else {}

    def _memory_hints_for_context(
        self,
        *,
        case_id: str,
        query: str,
        case_state: Any,
        section: str,
        limit: int,
        max_total_chars: int,
        min_score: float,
    ) -> list[dict[str, Any]]:
        candidates = self.memory.search(case_id=case_id, query=query, limit=max(limit * 4, 12))
        case_brief = _case_brief(case_state)
        by_source: dict[str, dict[str, Any]] = {}
        for raw in candidates:
            item = dict(raw)
            text = _planner_safe_text(item.get("text", ""), max_chars=700)
            if not text:
                continue
            if _memory_conflicts_case_state(text, case_state):
                continue
            if _memory_repeats_case_brief(text, case_brief):
                continue
            source_ref = str(item.get("source_ref") or "").strip()
            score = float(item.get("relevance_score") or item.get("score") or 0.0)
            if not source_ref:
                score *= 0.5
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            expires_at = str(metadata.get("expires_at") or "")
            freshness, freshness_multiplier = _memory_freshness(expires_at)
            score *= freshness_multiplier
            if score < min_score:
                continue
            record = {
                "section": section,
                "truth_status": "advisory",
                "memory_type": item.get("memory_type", ""),
                "text": text,
                "source_ref": source_ref,
                "relevance_score": round(score, 4),
                "confidence": round(min(1.0, max(0.0, score)), 4),
                "freshness": freshness,
                "expires_at": expires_at,
                "included_reason": _memory_included_reason(text, case_state),
                "max_chars": 160,
                "boundary": "memory_hint_only_not_case_truth",
                "score_terms": list(item.get("score_terms") or [])[:12],
                "score_reason": str(item.get("score_reason") or ""),
                "raw_score": item.get("raw_score", 0),
            }
            key = source_ref or f"memory:{item.get('id')}"
            current = by_source.get(key)
            if not current or float(current.get("relevance_score") or 0.0) < score:
                by_source[key] = record
        ordered = sorted(by_source.values(), key=lambda row: float(row.get("relevance_score") or 0.0), reverse=True)
        result: list[dict[str, Any]] = []
        remaining = max_total_chars
        for item in ordered:
            if len(result) >= limit or remaining <= 0:
                break
            max_chars = min(160, remaining)
            text = str(item.get("text") or "")[:max_chars]
            if not text:
                continue
            item = dict(item)
            item["text"] = text
            item["max_chars"] = max_chars
            remaining -= len(text)
            result.append(item)
        return result

    def last_evidence_reviewer_result(self, state: Any, mode: str | tuple[str, ...] | None = None) -> dict[str, Any]:
        modes = {mode} if isinstance(mode, str) else set(mode or [])
        for observation in reversed(getattr(state, "observations", []) or []):
            if observation.get("kind") != "role" or observation.get("name") != "evidence_reviewer":
                continue
            ref = observation.get("artifact_ref")
            if not ref:
                continue
            try:
                payload = self.artifacts.read(state.case_id, ref)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            payload_mode = str(payload.get("mode") or observation.get("reviewer_mode") or "review")
            if modes and payload_mode not in modes:
                continue
            return payload
        return {}

    def last_attachment_items(self, state: Any) -> list[dict[str, Any]]:
        result = self._latest_payload(state, kind="tool", name="read_attachment")
        if not isinstance(result, dict):
            return []
        attachments = result.get("attachments")
        if isinstance(attachments, list):
            return [item for item in attachments if isinstance(item, dict)][:12]
        return [result]

    def last_runtime_feedback(self, state: Any) -> dict[str, Any]:
        return _latest_runtime_feedback(getattr(state, "observations", []) or [])

    def _latest_payload(self, state: Any, *, kind: str, name: str | None = None) -> Any:
        for observation in reversed(state.observations):
            if observation.get("kind") != kind:
                continue
            if name and observation.get("name") != name:
                continue
            ref = observation.get("artifact_ref")
            if not ref:
                continue
            try:
                return self.artifacts.read(state.case_id, ref)
            except Exception:
                return None
        return None

    def _summarize(self, *, artifact_type: str, name: str, result: Any) -> SummaryResult:
        summary_input = _build_summary_input(artifact_type=artifact_type, name=name, result=result)
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) <= SUMMARY_THRESHOLD:
            return _heuristic_summary(artifact_type, name, result)
        if artifact_type not in LLM_SUMMARIZER_ARTIFACT_TYPES:
            return _heuristic_summary(artifact_type, name, result)
        payload = {
            "artifact_type": artifact_type,
            "name": name,
            "raw_preview": json.dumps(summary_input["summary_input"], ensure_ascii=False, default=str)[:8000],
            "summary_input": summary_input["summary_input"],
            "structured_preview": summary_input["structured_preview"],
            "source_refs": summary_input["source_refs"],
            "large_payload_notes": summary_input["large_payload_notes"],
            "content_hashes": summary_input["content_hashes"],
            "task_goal": "invoice payment review",
        }
        try:
            summary = self.llm.complete_structured(
                role="summarizer",
                system_prompt=SUMMARIZER_PROMPT,
                payload=payload,
                model_type=SummaryResult,
                prompt_version="summarizer_v4.4+global_policy_v1.0+checkpoint_skill_v1.0+attachment_manifest_skill_v1.0",
            )
            if not summary.summary:
                return _heuristic_summary(artifact_type, name, result)
            return _sanitize_summary_result(summary)
        except Exception as exc:
            fallback = _heuristic_summary(artifact_type, name, result)
            fallback.risks.append(f"summarizer_error: {type(exc).__name__}: {exc}")
            return fallback

    def _attachment_manifest_summaries(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        attachments = [
            item
            for item in result.get("attachments") or []
            if isinstance(item, dict) and item.get("attachment_id")
        ]
        summaries: list[dict[str, Any]] = []
        for item in attachments[:12]:
            if item.get("status") == "error":
                summary = SummaryResult(
                    summary=f"{item.get('name', 'attachment')}: read failed",
                    risks=[str(warning) for warning in item.get("warnings") or []][:6],
                )
            else:
                summary = _heuristic_summary(
                    "attachment_batch",
                    f"attachment:{item.get('name') or 'file'}",
                    {"attachment_count": 1, "attachments": [item]},
                )
            summaries.append(
                {
                    "attachment_id": item.get("attachment_id", ""),
                    "original_ref": item.get("original_ref", ""),
                    "name": item.get("name", ""),
                    "summary": summary.summary,
                    "key_facts": summary.key_facts,
                    "risks": summary.risks,
                    "missing_items": summary.missing_items,
                }
            )
        return summaries


def _artifact_type(kind: str, name: str) -> str:
    if kind == "tool" and name == "read_attachment":
        return "attachment_batch"
    if kind == "role" and name == "report_writer":
        return "report_markdown"
    if kind == "role":
        return "role_result"
    return f"{kind}_result"



def _heuristic_summary(artifact_type: str, name: str, result: Any) -> SummaryResult:
    if isinstance(result, dict):
        if artifact_type == "attachment_batch":
            attachments = result.get("attachments") or []
            names = [str(item.get("name") or "") for item in attachments if isinstance(item, dict)]
            return SummaryResult(
                summary=f"读取了 {len(names)} 个附件：" + ", ".join(names[:8]),
                key_facts=names[:12],
                next_action_hint=route_policy.reviewer_hint_after_attachment(result),
                must_preserve_refs=names[:12],
            )
        if name == "evidence_reviewer":
            mode = str(result.get("mode") or "review")
            if mode == "extract":
                extraction = result.get("extraction_result") or {}
                docs = extraction.get("source_docs") or extraction.get("documents") or []
                cards = result.get("evidence_cards") or []
                return SummaryResult(
                    summary=(
                        "证据抽取完成："
                        f"mode=extract; docs={len(docs) if isinstance(docs, list) else 0}; "
                        f"fields={len(result.get('extracted_fields') or {})}; cards={len(cards)}."
                    ),
                    key_facts=[
                        str(item.get("title") or item.get("doc_id") or item.get("source_doc_id") or "")[:180]
                        for item in (docs if isinstance(docs, list) else [])
                        if isinstance(item, dict)
                    ][:8],
                    risks=list(result.get("risk_flags") or [])[:6],
                    next_action_hint="call_role:evidence_reviewer_review",
                )
            suggested = result.get("suggested_patch") or {}
            contradicted = _unique_nonempty(
                list(result.get("risk_flags") or [])
                + list(suggested.get("risk_flags") or [])
            )
            return SummaryResult(
                summary=(
                    "证据审查完成："
                    f"mode={mode}; type={result.get('evidence_type', 'unknown')}; "
                    f"credibility={result.get('credibility', 'unknown')}; "
                    f"should_accept={result.get('should_accept')}; "
                    f"supports={len(result.get('supports') or [])}; "
                    f"conflicts={len(contradicted)}; "
                    f"cards={len(result.get('evidence_cards') or [])}; "
                    f"add_evidence={len(suggested.get('add_evidence') or [])}."
                ),
                key_facts=[
                    f"{item.get('requirement', '')}:{item.get('support_level', '')}"
                    for item in result.get("supports") or []
                    if isinstance(item, dict)
                ][:8],
                risks=[f"proof_contradiction:{item}" for item in contradicted[:6]],
                missing_items=list(suggested.get("missing_materials") or [])[:8],
                next_action_hint="call_role:case_patch_writer",
            )
        if name == "materials_advisor":
            tasks = [item for item in result.get("tasks") or [] if isinstance(item, dict)]
            task_facts = [
                " | ".join(
                    part
                    for part in [
                        str(item.get("requirement") or "").strip(),
                        str(item.get("current_status") or "").strip(),
                        str(item.get("task") or "").strip(),
                    ]
                    if part
                )[:240]
                for item in tasks[:8]
            ]
            answer = str(result.get("answer") or "材料建议已生成。")
            summary = answer[:1200]
            if tasks:
                summary = (summary + f"\n\nstructured_tasks: tasks={len(tasks)}; " + "; ".join(task_facts[:6])).strip()
            return SummaryResult(
                summary=summary[:1400],
                key_facts=task_facts + [str(item) for item in result.get("next_questions") or []][:5],
                missing_items=list(result.get("missing_materials") or [])[:8],
                next_action_hint="final_answer",
            )
        if name == "case_patch_writer":
            updates = result.get("case_updates") or {}
            return SummaryResult(
                summary=(
                    f"CasePatch {result.get('patch_type', 'unknown')} prepared; "
                    f"add_evidence={len(updates.get('add_evidence') or [])}; "
                    f"cards={len(updates.get('evidence_cards') or [])}."
                ),
                key_facts=[str(result.get("audit_note") or "")[:300]],
                missing_items=list(updates.get("missing_materials") or [])[:8],
                next_action_hint="write_case_patch",
            )
        if name == "write_case_patch":
            requirement_facts = [
                f"{item.get('id')}={item.get('status')}"
                for item in list(result.get("requirements") or [])
                if str(item.get("id") or "").strip() and str(item.get("status") or "").strip()
            ]
            return SummaryResult(
                summary=f"Case state updated: status={result.get('status')}, evidence={len(result.get('evidence_items') or [])}.",
                key_facts=requirement_facts[:12],
                missing_items=list(result.get("missing_materials") or [])[:8],
                next_action_hint="final_answer",
            )
        if name == "report_writer":
            markdown = str(result.get("markdown") or "")
            return SummaryResult(
                summary=f"Report markdown generated: title={result.get('title', 'final_report')}, chars={len(markdown)}.",
                key_facts=[str(result.get("title") or "final_report")],
                next_action_hint="call_tool:write_case_file",
            )
        if name == "list_case_files":
            files = [str(item) for item in result.get("files") or []]
            report_files = [item for item in files if item.startswith("reports/")]
            return SummaryResult(
                summary=f"Case workspace contains {len(files)} files; reports: {', '.join(report_files[:6]) or 'none'}.",
                key_facts=files[:20],
                next_action_hint="final_answer",
                must_preserve_refs=report_files[:6],
            )
        if name == "write_case_file":
            return SummaryResult(
                summary=f"File written: {result.get('relative_path') or result.get('path')}; bytes={result.get('bytes')}.",
                key_facts=[str(result.get("relative_path") or result.get("path") or "")],
                next_action_hint="call_tool:render_pdf",
            )
        if name == "render_pdf":
            return SummaryResult(
                summary=f"PDF rendered: {result.get('pdf_path') or result.get('path') or result.get('relative_path')}.",
                key_facts=[str(result.get("pdf_path") or result.get("path") or result.get("relative_path") or "")],
                next_action_hint="final_answer",
            )
    return SummaryResult(summary=f"{name} completed; artifact stored for raw details.", next_action_hint="")


SUMMARY_REF_KEYS = {
    "artifact_ref",
    "source_ref",
    "source_path",
    "source_locator",
    "original_ref",
    "preview_path",
    "preview_paths",
    "filename",
    "file_name",
    "name",
    "path",
    "relative_path",
    "id",
    "evidence_id",
    "document_id",
    "run_id",
    "turn_id",
}

SUMMARY_KIND_KEYS = {"artifact_type", "content_kind", "evidence_type", "type", "extraction_method", "mime_type"}
SUMMARY_KIND_SUFFIXES = {
    ".pdf": "pdf",
    ".csv": "csv",
    ".json": "json",
    ".log": "log",
    ".md": "markdown",
    ".txt": "text",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".tif": "image",
    ".tiff": "image",
    ".webp": "image",
}
AP_MISSING_MARKERS = (
    "purchase order",
    "po ",
    "po/",
    "po-",
    "goods receipt",
    "grn",
    "vendor master",
    "vendor record",
    "duplicate payment",
    "duplicate-payment",
    "bank record",
    "ap review",
    "three-way",
    "3-way",
    "采购订单",
    "收货单",
    "供应商主数据",
    "重复付款",
    "银行记录",
    "三单",
)
PROMPT_INJECTION_MARKERS = (
    "ignore previous rules",
    "ignore all previous",
    "disregard previous",
    "system prompt",
    "developer message",
    "approve payment",
    "submit erp",
    "prompt injection",
    "忽略规则",
    "忽略前文",
    "批准付款",
    "提交erp",
    "越权执行",
    "提示注入",
)
PROMPT_INJECTION_OUTPUT_SEGMENT_RE = re.compile(
    r"(?:ignore previous rules|ignore all previous|disregard previous|approve payment|submit erp|prompt injection|"
    r"system prompt|developer message|忽略规则|忽略前文|批准付款|提交erp|提交 ERP|越权执行|提示注入)[^。\n；;,.，]{0,120}",
    re.I,
)
PROMPT_INJECTION_NEUTRAL = "材料包含越权执行性指令，已按数据处理"


def _build_summary_input(*, artifact_type: str, name: str, result: Any) -> dict[str, Any]:
    result = _normalize_summary_source(artifact_type, result)
    seen_hashes: dict[str, str] = {}
    content_hashes: list[dict[str, Any]] = []
    source_refs: list[str] = [artifact_type, name]
    source_kinds: list[str] = [artifact_type]
    notes: list[str] = []
    warnings: list[str] = []
    compacted = _compact_summary_value(
        result,
        "$",
        seen_hashes=seen_hashes,
        content_hashes=content_hashes,
        source_refs=source_refs,
        source_kinds=source_kinds,
        notes=notes,
        warnings=warnings,
    )
    source_refs = _unique_nonempty(source_refs)[:80]
    source_kinds = _unique_nonempty(source_kinds)[:30]
    return {
        "summary_input": compacted,
        "structured_preview": {
            "artifact_type": artifact_type,
            "name": name,
            "detected_source_kinds": source_kinds,
            "source_ref_count": len(source_refs),
            "large_field_count": sum(1 for item in content_hashes if item.get("mode") == "truncated"),
            "duplicate_large_field_count": sum(1 for item in content_hashes if item.get("mode") == "duplicate"),
            "warnings": _unique_nonempty(warnings)[:20],
        },
        "source_refs": source_refs,
        "large_payload_notes": notes[:80],
        "content_hashes": content_hashes[:80],
    }


def _normalize_summary_source(artifact_type: str, result: Any) -> Any:
    if artifact_type != "attachment_batch" or not isinstance(result, dict):
        return result
    normalized = dict(result)
    normalized.pop("truncated", None)
    attachments: list[dict[str, Any]] = []
    for item in normalized.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row.pop("truncated", None)
        row["context_delivery"] = {
            "content_is_excerpt_for_model_context": True,
            "full_source_available_via_original_ref": bool(row.get("original_ref")),
            "full_extraction_available_via_extraction_ref": bool(row.get("extraction_ref")),
        }
        attachments.append(row)
    normalized["attachments"] = attachments
    return normalized


def _compact_summary_value(
    value: Any,
    path: str,
    *,
    seen_hashes: dict[str, str],
    content_hashes: list[dict[str, Any]],
    source_refs: list[str],
    source_kinds: list[str],
    notes: list[str],
    warnings: list[str],
) -> Any:
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path != "$" else f"$.{key_text}"
            _collect_summary_metadata(key_text, item, source_refs, source_kinds, warnings)
            compacted[key] = _compact_summary_value(
                item,
                child_path,
                seen_hashes=seen_hashes,
                content_hashes=content_hashes,
                source_refs=source_refs,
                source_kinds=source_kinds,
                notes=notes,
                warnings=warnings,
            )
        return compacted
    if isinstance(value, list):
        return [
            _compact_summary_value(
                item,
                f"{path}[{index}]",
                seen_hashes=seen_hashes,
                content_hashes=content_hashes,
                source_refs=source_refs,
                source_kinds=source_kinds,
                notes=notes,
                warnings=warnings,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        _collect_source_kind_from_string(value, source_kinds)
        if _has_prompt_injection_marker(value):
            warnings.append(PROMPT_INJECTION_NEUTRAL)
        if len(value) <= SUMMARY_INPUT_STRING_LIMIT:
            return value
        digest = _sha256(value)
        if len(value) >= SUMMARY_DEDUPE_MIN_CHARS and digest in seen_hashes:
            duplicate_of = seen_hashes[digest]
            notes.append(f"deduplicated {path} duplicate_of={duplicate_of} chars={len(value)} sha256={digest}")
            content_hashes.append(
                {"path": path, "chars": len(value), "sha256": digest, "mode": "duplicate", "duplicate_of": duplicate_of}
            )
            return {
                "__summary_compaction__": "duplicate_large_string",
                "path": path,
                "chars": len(value),
                "sha256": digest,
                "duplicate_of": duplicate_of,
            }
        seen_hashes.setdefault(digest, path)
        notes.append(f"truncated {path} chars={len(value)} sha256={digest}")
        content_hashes.append({"path": path, "chars": len(value), "sha256": digest, "mode": "truncated"})
        return {
            "__summary_compaction__": "large_string_head_tail",
            "path": path,
            "chars": len(value),
            "sha256": digest,
            "head": value[:SUMMARY_INPUT_HEAD_CHARS],
            "tail": value[-SUMMARY_INPUT_TAIL_CHARS:],
        }
    return value


def _collect_summary_metadata(
    key: str,
    value: Any,
    source_refs: list[str],
    source_kinds: list[str],
    warnings: list[str],
) -> None:
    key_lower = key.lower()
    if key_lower in SUMMARY_REF_KEYS:
        _collect_ref_value(value, source_refs)
    if key_lower in SUMMARY_KIND_KEYS:
        _collect_ref_value(value, source_kinds)
    if key_lower in {"warning", "warnings"}:
        _collect_ref_value(value, warnings)
    if key_lower in {"name", "filename", "file_name", "path", "source_path", "original_ref", "preview_path"}:
        _collect_source_kind_from_value(value, source_kinds)


def _collect_ref_value(value: Any, target: list[str]) -> None:
    if isinstance(value, str):
        if 0 < len(value) <= 260:
            target.append(value)
        return
    if isinstance(value, (int, float, bool)):
        target.append(str(value))
        return
    if isinstance(value, list):
        for item in value[:20]:
            _collect_ref_value(item, target)
        return
    if isinstance(value, dict):
        for key in ("artifact_ref", "source_path", "path", "name", "id", "document_id", "evidence_id"):
            if key in value:
                _collect_ref_value(value.get(key), target)


def _collect_source_kind_from_value(value: Any, source_kinds: list[str]) -> None:
    if isinstance(value, str):
        _collect_source_kind_from_string(value, source_kinds)
    elif isinstance(value, list):
        for item in value[:20]:
            _collect_source_kind_from_value(item, source_kinds)


def _collect_source_kind_from_string(value: str, source_kinds: list[str]) -> None:
    lower = value.lower()
    for suffix, kind in SUMMARY_KIND_SUFFIXES.items():
        if suffix in lower:
            source_kinds.append(kind)
    for marker, kind in (
        ("invoice", "invoice"),
        ("duplicate", "duplicate_check"),
        ("rag", "rag"),
        ("ocr", "ocr_text"),
        ("clear invoice", "process_log"),
        ("log", "log"),
    ):
        if marker in lower:
            source_kinds.append(kind)


def _sanitize_summary_result(summary: SummaryResult) -> SummaryResult:
    summary.next_action_hint = ""
    summary.summary = _neutralize_prompt_injection_risk(summary.summary)
    summary.key_facts = [_brief_text(_neutralize_prompt_injection_risk(item), 260) for item in summary.key_facts[:20]]
    summary.risks = [
        _brief_text(_neutralize_prompt_injection_risk(item), 260)
        for item in summary.risks[:12]
        if not _looks_like_context_delivery_note(item)
    ]
    summary.missing_items = [
        _brief_text(_neutralize_prompt_injection_risk(item), 220)
        for item in summary.missing_items[:12]
        if not _looks_like_ap_missing(item) and not _looks_like_context_delivery_note(item)
    ]
    summary.must_preserve_refs = _unique_nonempty([_brief_text(item, 260) for item in summary.must_preserve_refs])[:30]
    return summary


def _looks_like_context_delivery_note(value: Any) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "tool_response_excerpt_truncated",
            "content_is_excerpt_for_model_context",
            "attachment_text_was_truncated_in_tool_response",
            "content_preview_truncated",
            "context excerpt",
            "context window",
            "summary_compaction",
            "large_string_head_tail",
            "dossier keeps the full extracted text",
        )
    )


def _looks_like_ap_missing(value: Any) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in AP_MISSING_MARKERS)


def _neutralize_prompt_injection_risk(value: Any) -> str:
    text = str(value or "")
    placeholder = "__PROMPT_INJECTION_NEUTRAL__"
    protected = text.replace(PROMPT_INJECTION_NEUTRAL, placeholder)
    redacted = PROMPT_INJECTION_OUTPUT_SEGMENT_RE.sub(PROMPT_INJECTION_NEUTRAL, protected)
    redacted = redacted.replace(placeholder, PROMPT_INJECTION_NEUTRAL)
    redacted = re.sub(f"(?:{re.escape(PROMPT_INJECTION_NEUTRAL)}\\s*)+", PROMPT_INJECTION_NEUTRAL, redacted)
    return redacted


def _has_prompt_injection_marker(value: Any) -> bool:
    text = str(value or "").lower().replace(" ", "")
    spaced = str(value or "").lower()
    return any(marker.replace(" ", "") in text or marker in spaced for marker in PROMPT_INJECTION_MARKERS)


def _unique_nonempty(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _case_brief(case_state: Any) -> str:
    packet = derive_consumer_packet(case_state)
    if packet.root_decisions:
        decision_brief = ", ".join(
            f"{item.requirement_id}={item.status}"
            for item in packet.root_decisions
        )
        obligation_brief = ", ".join(
            f"{item.id}[missing_fact={item.missing_fact};"
            f"candidate_actions={'|'.join(item.candidate_actions)}]"
            for item in packet.obligations
            if item.blocking
        )
        return (
            f"case_id={packet.case_id}; execution_status={packet.execution_status}; "
            f"review_complete={str(packet.review_complete).lower()}; "
            f"decision_ready={str(packet.decision_ready).lower()}; "
            f"reportability={packet.reportability}; proof_decisions={decision_brief}; "
            f"blocking_obligations={obligation_brief}"
        )
    requirements = [
        f"{item.id}:{item.status}({len(item.evidence_ids)})"
        for item in getattr(case_state, "requirements", []) or []
    ]
    proof = getattr(case_state, "compiled_proof", None)
    proof_brief = ""
    if proof:
        obligations = sorted(proof.obligations, key=lambda item: item.id)
        obligation_rows: list[str] = []
        for item in (row for row in obligations if row.blocking):
            actions = "|".join(str(action) for action in item.candidate_actions) or "read_source"
            obligation_rows.append(
                f"{item.id}[missing_fact={item.missing_fact};candidate_actions={actions}]"
            )
        obligation_brief = ", ".join(obligation_rows[:5])
        decision_brief = ", ".join(
            f"{item.requirement_id}={item.status}"
            for item in proof.decisions
        )
        proof_brief = (
            f"proof_decisions={decision_brief}; "
            f"blocking_obligations={obligation_brief}; "
        )
    return (
        f"case_id={case_state.case_id}; status={case_state.status}; "
        f"profile={_brief_text(getattr(case_state, 'case_profile', {}) or {}, 180)}; "
        f"requirements={', '.join(requirements)}; "
        f"evidence_count={len(case_state.evidence_items)}; "
        f"missing={', '.join(case_state.missing_materials or [])}; "
        f"weak={', '.join(getattr(case_state, 'weak_materials', []) or [])}; "
        f"conflict={', '.join(getattr(case_state, 'conflict_materials', []) or [])}; "
        f"satisfied={', '.join(getattr(case_state, 'satisfied_materials', []) or [])}; "
        f"risk_count={len(case_state.risk_flags)}; "
        f"{proof_brief}"
        f"next_action_hint={getattr(case_state, 'next_action_hint', '') or ''}"
    )


def _memory_conflicts_case_state(text: str, case_state: Any) -> bool:
    lower = text.lower()
    requirements = getattr(case_state, "requirements", []) or []
    positive_terms = ("satisfied", "accepted", "complete", "已满足", "齐全", "通过")
    negative_terms = ("missing", "weak", "conflict", "rejected", "缺失", "不足", "冲突", "不一致")
    for requirement in requirements:
        req_id = str(getattr(requirement, "id", "") or "")
        if not req_id or req_id.lower() not in lower:
            continue
        status = str(getattr(requirement, "status", "") or "")
        if status in {"missing", "weak", "conflict", "rejected"} and any(term in lower for term in positive_terms):
            return True
        if status in {"satisfied", "accepted"} and any(term in lower for term in negative_terms):
            return True
    return False


def _memory_repeats_case_brief(text: str, case_brief: str) -> bool:
    lower = text.lower().strip()
    brief = case_brief.lower()
    if not lower:
        return True
    if lower in brief:
        return True
    tokens = [token for token in re.findall(r"[\w\u4e00-\u9fff]{3,}", lower) if len(token) >= 3]
    if len(tokens) < 4:
        return False
    overlap = sum(1 for token in tokens if token in brief)
    return overlap / max(1, len(tokens)) >= 0.85


def _memory_freshness(expires_at: Any) -> tuple[str, float]:
    value = str(expires_at or "").strip()
    if not value:
        return "case_current", 1.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "invalid_expires_at", 0.75
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed < datetime.now(timezone.utc):
        return "expired", 0.5
    return "case_current", 1.0


def _memory_included_reason(text: str, case_state: Any) -> str:
    lower = text.lower()
    for field in ("missing_materials", "weak_materials", "conflict_materials"):
        for item in getattr(case_state, field, []) or []:
            item_text = str(item or "")
            if item_text and item_text.lower() in lower:
                return f"matches {field}:{item_text}"
    profile = getattr(case_state, "case_profile", {}) or {}
    if isinstance(profile, dict):
        for key in ("supplier", "invoice_number", "amount_total"):
            value = str(profile.get(key) or "")
            if value and value.lower() in lower:
                return f"matches case_profile:{key}"
    return "matches user query tokens"


def _case_memory(case_state: Any) -> dict[str, Any]:
    return {
        "requirements": [
            {
                "id": item.id,
                "status": item.status,
                "evidence_count": len(item.evidence_ids),
            }
            for item in getattr(case_state, "requirements", []) or []
        ],
        "evidence": [
            {
                "id": item.id,
                "type": item.type,
                "credibility": item.credibility,
                "summary": _brief_text(item.summary, 180),
                "should_accept": item.review_result.get("should_accept") if item.review_result else None,
                "conflict_count": len(item.conflicts),
            }
            for item in list(getattr(case_state, "evidence_items", []) or [])[-8:]
        ],
        "risk_flags": [_brief_text(item, 220) for item in list(getattr(case_state, "risk_flags", []) or [])[-8:]],
        "missing_materials": list(getattr(case_state, "missing_materials", []) or []),
        "weak_materials": list(getattr(case_state, "weak_materials", []) or []),
        "conflict_materials": list(getattr(case_state, "conflict_materials", []) or []),
        "satisfied_materials": list(getattr(case_state, "satisfied_materials", []) or []),
    }


def _brief_text(value: Any, max_chars: int) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    text = text.strip()
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def _brief_list(value: Any, limit: int, max_chars: int) -> list[str]:
    items = value if isinstance(value, list) else ([value] if value else [])
    return [_brief_text(item, max_chars) for item in items[:limit] if _brief_text(item, max_chars)]


def _brief_records(value: Any, limit: int) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = [{"field": key, **(item if isinstance(item, dict) else {"value": item})} for key, item in value.items()]
    if not isinstance(value, list):
        return []
    records: list[dict[str, Any]] = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        record: dict[str, Any] = {}
        for key, field_value in item.items():
            if isinstance(field_value, str):
                record[str(key)] = _brief_text(field_value, 260)
            elif isinstance(field_value, (int, float, bool)) or field_value is None:
                record[str(key)] = field_value
            elif isinstance(field_value, list):
                record[str(key)] = field_value[:8]
            elif isinstance(field_value, dict):
                record[str(key)] = {
                    str(child_key): _brief_text(child_value, 160) if isinstance(child_value, str) else child_value
                    for child_key, child_value in list(field_value.items())[:8]
                }
            else:
                record[str(key)] = _brief_text(field_value, 160)
        records.append(record)
    return records


def _compact_visual_check(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "looks_like_invoice": value.get("looks_like_invoice", ""),
        "visible_sections": value.get("visible_sections") or {},
        "field_visibility": _brief_records(value.get("field_visibility"), 16),
        "same_source_check": value.get("same_source_check") or {},
        "ocr_quality": value.get("ocr_quality") or {},
        "page_integrity": value.get("page_integrity") or {},
        "layout_check": value.get("layout_check") or {},
        "limitations": _brief_list(value.get("limitations"), 6, 120),
    }


def _artifact_refs_from_context(value: Any) -> list[str]:
    refs: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            ref = item.get("artifact_ref") or item.get("user_message_ref")
            if ref:
                refs.append(str(ref))
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    seen: set[str] = set()
    result: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            result.append(ref)
    return result


def _latest_plan(state: Any) -> list[str]:
    if not state.plan_progress:
        return []
    latest = state.plan_progress[-1]
    return list(latest.get("short_plan") or [])


def _latest_next_action_hint(observations: list[dict[str, Any]]) -> str:
    for observation in reversed(observations):
        hint = observation.get("next_action_hint")
        if hint:
            return str(hint)
    return ""


def _planner_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    planner_safe: list[dict[str, Any]] = []
    for observation in observations:
        if observation.get("kind") == "session":
            continue
        planner_safe.append(_planner_safe_observation(observation))
    return planner_safe[-6:]


def _planner_recent_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe_turns: list[dict[str, Any]] = []
    for turn in turns:
        safe_turns.append(
            {
                "turn_id": turn.get("turn_id", ""),
                "user_summary": _planner_safe_text(turn.get("user_summary", ""), max_chars=180),
                "assistant_summary": _planner_safe_text(turn.get("assistant_summary", ""), max_chars=180),
                "attachments": [
                    {"name": item.get("name", ""), "path": item.get("path", "")}
                    for item in turn.get("attachments", [])
                    if isinstance(item, dict)
                ],
                "run_ids": list(turn.get("run_ids") or []),
            }
        )
    return safe_turns


def _prior_recent_turns(turns: list[dict[str, Any]], current_turn_id: str, *, limit: int) -> list[dict[str, Any]]:
    current = str(current_turn_id or "").strip()
    prior = [turn for turn in turns if not current or str(turn.get("turn_id") or "") != current]
    compacted: list[dict[str, Any]] = []
    for turn in prior[-limit:]:
        compacted.append(
            {
                "turn_id": turn.get("turn_id", ""),
                "user_summary": turn.get("user_summary") or turn.get("user_message_summary", ""),
                "assistant_summary": turn.get("assistant_summary", ""),
                "attachments": [
                    {"name": item.get("name", ""), "path": item.get("path", "")}
                    for item in turn.get("attachments", [])
                    if isinstance(item, dict)
                ],
                "run_ids": list(turn.get("run_ids") or []),
            }
        )
    return compacted


def _planner_safe_observation(observation: dict[str, Any]) -> dict[str, Any]:
    safe = {
        "kind": observation.get("kind", ""),
        "name": observation.get("name", ""),
        "summary": _planner_safe_text(observation.get("summary", ""), max_chars=260),
        "next_action_hint": observation.get("next_action_hint", ""),
    }
    if observation.get("kind") == "role" and observation.get("name") == "report_writer":
        safe["content_ref"] = "last_role:report_writer.markdown"
    elif observation.get("artifact_ref"):
        safe["artifact_ref"] = observation.get("artifact_ref")
    if observation.get("runtime_feedback"):
        safe["runtime_feedback"] = observation.get("runtime_feedback")
    missing_items = observation.get("missing_items") or []
    if missing_items:
        safe["missing_items"] = [_planner_safe_text(item, max_chars=120) for item in missing_items[:8]]
    risks = observation.get("risks") or []
    if risks:
        safe["risks"] = [_planner_safe_text(item, max_chars=160) for item in risks[:5]]
    if observation.get("kind") == "tool" and observation.get("name") == "read_attachment" and not observation.get("error"):
        safe["summary"] = _attachment_batch_planner_summary(observation)
    return {key: value for key, value in safe.items() if value not in ("", [], None)}


def _attachment_batch_planner_summary(observation: dict[str, Any]) -> str:
    ref = str(observation.get("artifact_ref") or "")
    prefix = "Attachment batch read"
    if ref:
        return f"{prefix}; raw content stored in artifact_ref."
    return prefix


def _planner_safe_text(value: Any, max_chars: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    for marker in (
        " **已录入证据**",
        " **当前审查状态**",
        " **三单匹配状态**",
        " **当前缺口**",
        " **下一步**",
        " - ",
        " 1. ",
        " | ",
    ):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
            break
    text = _redact_planner_raw_details(text)
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def _redact_planner_raw_details(text: str) -> str:
    # Planner only needs routing state and references; detailed line items stay in artifacts/role context.
    text = re.sub(
        r"\b[A-Z][A-Za-z0-9-]*(?:\s+[A-Za-z0-9][A-Za-z0-9-]*){2,}\s+(?!(?:INV|PO|GRN)-\d+\b)[A-Z]{1,6}-\d+\b",
        "[attachment_detail]",
        text,
    )
    text = re.sub(r"[\u4e00-\u9fffA-Za-z ]{4,}OS-\d+(?:\s*[×x]\s*\d+)?", "[attachment_detail]", text)
    return text


def _attachment_meta(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name", ""),
        "path": item.get("path", ""),
        "content_type": item.get("content_type", ""),
    }


def _sanitize_case_state(case_state: Any) -> dict[str, Any]:
    data = case_state.model_dump()
    data["evidence_items"] = [_sanitize_evidence(item) for item in case_state.evidence_items]
    return data


def _advisor_case_state(case_state: Any) -> dict[str, Any]:
    requirements = list(getattr(case_state, "requirements", []) or [])
    referenced_evidence_ids = {
        str(evidence_id)
        for requirement in requirements
        for evidence_id in list(getattr(requirement, "evidence_ids", []) or [])
        if str(evidence_id)
    }
    proof = getattr(case_state, "compiled_proof", None)
    return {
        "case_id": str(getattr(case_state, "case_id", "") or ""),
        "case_type": str(getattr(case_state, "case_type", "") or ""),
        "status": str(getattr(case_state, "status", "") or ""),
        "requirements": [
            {
                "id": str(getattr(item, "id", "") or ""),
                "label": str(getattr(item, "label", "") or ""),
                "kind": str(getattr(item, "kind", "") or ""),
                "required": bool(getattr(item, "required", True)),
                "status": str(getattr(item, "status", "") or ""),
                "evidence_ids": list(getattr(item, "evidence_ids", []) or []),
            }
            for item in requirements
        ],
        "missing_materials": list(getattr(case_state, "missing_materials", []) or []),
        "weak_materials": list(getattr(case_state, "weak_materials", []) or []),
        "conflict_materials": list(getattr(case_state, "conflict_materials", []) or []),
        "satisfied_materials": list(getattr(case_state, "satisfied_materials", []) or []),
        "risk_flags": _brief_list(
            _sanitize_prompt_injection_details(getattr(case_state, "risk_flags", [])),
            8,
            220,
        ),
        "evidence_items": [
            {
                "id": str(getattr(item, "id", "") or ""),
                "type": str(getattr(item, "type", "") or ""),
                "source": str(getattr(item, "source", "") or ""),
                "credibility": str(getattr(item, "credibility", "") or ""),
                "summary": _brief_text(
                    _sanitize_prompt_injection_details(getattr(item, "summary", "")),
                    360,
                ),
            }
            for item in list(getattr(case_state, "evidence_items", []) or [])
            if str(getattr(item, "id", "") or "") in referenced_evidence_ids
        ],
        "compiled_proof": {
            "decisions": [
                item.model_dump(mode="json")
                for item in list(getattr(proof, "decisions", []) or [])
            ],
            "obligations": [
                item.model_dump(mode="json")
                for item in list(getattr(proof, "obligations", []) or [])
            ],
        }
        if proof is not None
        else None,
    }


def _safe_attachment_identity(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(item.get("name") or ""),
        "content_type": str(item.get("content_type") or ""),
    }


def _safe_attachment_manifest_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifest_ref": str(manifest.get("manifest_ref") or ""),
        "version": str(manifest.get("version") or ""),
        "status_counts": dict(manifest.get("status_counts") or {}),
        "attachments": [
            {
                "attachment_id": str(item.get("attachment_id") or ""),
                "name": str(item.get("name") or ""),
                "content_kind": str(item.get("content_kind") or ""),
                "status": str(item.get("status") or ""),
            }
            for item in list(manifest.get("attachments") or [])
            if isinstance(item, dict)
        ],
    }


def _proof_consumer_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep routing state while dropping specialist summaries and extracted values."""

    safe: list[dict[str, Any]] = []
    for item in observations:
        if not isinstance(item, dict):
            continue
        is_report_draft = item.get("kind") == "role" and item.get("name") == "report_writer"
        row = {
            key: item[key]
            for key in (("kind", "name") if is_report_draft else ("kind", "name", "artifact_ref"))
            if item.get(key) not in (None, "", [], {})
        }
        if is_report_draft:
            row["content_ref"] = "last_role:report_writer.markdown"
        route = _safe_routing_hint(item.get("next_action_hint"))
        if route:
            row["next_action_hint"] = route
        safe.append(row)
    return safe


def _safe_runtime_feedback(feedback: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    error_type = str(feedback.get("error_type") or "")
    if re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", error_type):
        safe["error_type"] = error_type
    if isinstance(feedback.get("retry_allowed"), bool):
        safe["retry_allowed"] = feedback["retry_allowed"]
    for key in ("recommended_action", "blocked_action"):
        route = _safe_routing_hint(feedback.get(key))
        if route:
            safe[key] = route
    return safe


def _safe_routing_hint(value: Any) -> str:
    route = str(value or "").strip()
    if route in {
        "dispatch",
        "final_answer",
        "ask_user",
        "needs_user_input",
        "ready_for_summary",
        "ready_for_report",
        "generate_report_requested",
        "retry_or_final_answer",
        "write_case_patch",
    }:
        return route
    if re.fullmatch(
        r"(?:call_tool|call_role|delegate_agent):[a-zA-Z0-9_.:-]{1,80}",
        route,
    ):
        return route
    return ""


def _advisor_attachment_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifest_ref": manifest.get("manifest_ref", ""),
        "version": manifest.get("version", ""),
        "status_counts": dict(manifest.get("status_counts") or {}),
        "attachments": [
            {
                "attachment_id": item.get("attachment_id", ""),
                "name": item.get("name", ""),
                "content_kind": item.get("content_kind", ""),
                "status": item.get("status", ""),
                "summary": _brief_text(
                    _sanitize_prompt_injection_details(item.get("summary", "")),
                    360,
                ),
                "risks": _brief_list(
                    _sanitize_prompt_injection_details(item.get("risks")),
                    4,
                    180,
                ),
                "original_ref": item.get("original_ref", ""),
                "evidence_ids": list(item.get("evidence_ids") or [])[:6],
            }
            for item in list(manifest.get("attachments") or [])
            if isinstance(item, dict)
        ],
    }


def _sanitize_evidence(item: Any) -> dict[str, Any]:
    data = item.model_dump() if hasattr(item, "model_dump") else dict(item or {})
    data = _sanitize_prompt_injection_details(data)
    metadata = dict(data.get("metadata") or {})
    for key in ("claim_to_source_refs", "semantic_judgments", "requirement_verdicts", "proof_proposals"):
        metadata.pop(key, None)
    data["metadata"] = metadata
    content = str(data.get("content") or "")
    data["content"] = content[:500] + ("..." if len(content) > 500 else "")
    return data


def _sanitize_prompt_injection_details(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize_prompt_injection_details(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_prompt_injection_details(item) for item in value]
    if isinstance(value, str):
        return PROMPT_INJECTION_DETAIL_RE.sub("材料中包含越权执行性指令，已按数据处理", value)
    return value


def _filter_role_payload(role: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        allowed = set(role_input_keys(role))
    except KeyError:
        return payload
    return {key: value for key, value in payload.items() if key in allowed}


def _apply_report_instruction_appendix(markdown: str, user_message: str) -> str:
    request = str(user_message or "")
    additions: list[str] = []
    if ("最终报告" in request or "报告归档" in request) and "本报告用于本地材料审查与报告归档" not in markdown:
        additions.append("- 本报告用于本地材料审查与报告归档；证据链完整性以材料状态和 Claim-to-Evidence Matrix 为准。")
    if "重复付款" in request and "重复付款检查细节" not in markdown:
        additions.append(
            "- 重复付款检查细节：应核对供应商、发票号、金额、日期、清账记录和历史付款记录；当前结论仅基于案卷内已提交的重复付款检查证据。"
        )
    if "Clear Invoice" in request and "Clear Invoice 边界" not in markdown:
        additions.append(
            "- Clear Invoice 边界：Clear Invoice/process log 只能作为历史流程证据，不能证明付款、批准、过账、路由或提交动作发生。"
        )
    if not additions:
        return markdown
    appendix = "\n\n## 报告修订说明\n\n" + "\n".join(additions) + "\n"
    return markdown.rstrip() + appendix


def _sanitize_report_markdown_for_guards(markdown: str, case_state: Any) -> str:
    text = str(markdown or "")
    text = (
        text.replace("可付款", "风险已排除")
        .replace("可支付", "风险已排除")
        .replace("不能作为已付款结论依据", "不能作为付款状态结论依据")
        .replace("不能作为已支付结论依据", "不能作为支付状态结论依据")
        .replace("付款已执行", "执行状态结论不受支持")
        .replace("付款已经执行", "执行状态结论不受支持")
        .replace("发票已批准", "审批状态结论不受支持")
        .replace("发票已经批准", "审批状态结论不受支持")
        .replace("已过账", "过账状态结论不受支持")
        .replace("已经过账", "过账状态结论不受支持")
        .replace("已清账", "清账关系待核对")
        .replace("已经清账", "清账关系待核对")
        .replace("已经付款", "历史付款记录")
        .replace("已经支付", "历史支付记录")
        .replace("已付款", "历史付款记录")
        .replace("已支付", "历史支付记录")
        .replace("可审批", "限制已解除")
        .replace("可批准", "限制已解除")
        .replace("可提交", "限制已解除")
        .replace("无保留报告", "限制未解除的报告")
        .replace("无保留意见", "限制未解除的结论")
    )
    if not _case_has_unresolved_requirements(case_state):
        return text
    text = text.replace("五项核心要求中四项已满足", "部分核心材料已提交并有 evidence 支持")
    text = text.replace("四证齐全", "主要材料已提交")
    text = text.replace("材料齐全", "材料限制已解除")
    text = text.replace("全部要求通过", "限制均已解除")
    text = re.sub(r"证据链完整(?!性|后)", "证据链完整性", text)
    return text


def _case_has_unresolved_requirements(case_state: Any) -> bool:
    requirements = list(getattr(case_state, "requirements", []) or [])
    if not requirements:
        return True
    for item in requirements:
        status = str(getattr(item, "status", ""))
        required = bool(getattr(item, "required", True))
        if not required and status in {"missing", "weak", "submitted", "accepted", "satisfied"}:
            continue
        if status not in {"accepted", "satisfied"}:
            return True
    return False


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())[:60].strip("._")
    return slug or "artifact"


def _sha256(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


ContextCompiler = ContextManager


def classify_runtime_error(*, kind: str, name: str, error: dict[str, Any] | str) -> dict[str, Any]:
    text = json.dumps(error, ensure_ascii=False, default=str).lower()
    if name == "read_attachment" and (
        "ocr_unavailable" in text
        or "ocr_failed" in text
        or "tesseract" in text
    ):
        return {
            "status": "terminal",
            "error_type": "ocr_unavailable",
            "retry_allowed": False,
            "recommended_action": "final_answer",
            "blocked_action": "call_tool:read_attachment",
            "user_message_hint": (
                "PDF/图片附件需要本地 OCR，但当前 OCR 命令不可用或执行失败。请配置 INVOICE_AGENT_TESSERACT_CMD，"
                "或先提供可复制文本、清晰图片、文本型 PDF 后重新提交。"
            ),
        }
    if kind in {"role", "model"} and (
        "timeout" in text
        or "timed out" in text
        or "apiterror" in text
        or "apitimeouterror" in text
    ):
        return {
            "status": "terminal",
            "error_type": "llm_timeout",
            "retry_allowed": False,
            "recommended_action": "final_answer",
            "blocked_action": f"delegate_agent:{name}" if kind == "role" and name else "",
            "user_message_hint": (
                "模型调用超时，本轮不要重复调用同一个 specialist；请基于已有 case_state/observations 告知用户稍后重试。"
            ),
        }
    if kind in {"role", "model"} and (
        "apiconnectionerror" in text
        or "connection error" in text
        or "connection reset" in text
        or "connection aborted" in text
    ):
        return {
            "status": "terminal",
            "error_type": "llm_connection_error",
            "retry_allowed": False,
            "recommended_action": "final_answer",
            "blocked_action": f"delegate_agent:{name}" if kind == "role" and name else "",
            "user_message_hint": (
                "模型连接在本轮内部重试后仍然失败；不要再次运行同一个 specialist，"
                "请告知用户连接暂时不可用并稍后重新发起。"
            ),
        }
    if name == "read_attachment" and (
        "pdf_reader_unavailable" in text
        or "pdf_open_error" in text
        or "image_open_error" in text
    ):
        return {
            "status": "terminal",
            "error_type": "document_open_error",
            "retry_allowed": False,
            "recommended_action": "final_answer",
            "blocked_action": "call_tool:read_attachment",
            "user_message_hint": (
                "附件原件无法打开或解析。请确认文件未损坏、未加密，并重新上传可读取的 PDF/JPG/PNG 或文本导出。"
            ),
        }
    if name == "read_attachment" and "unsupported attachment type" in text:
        return {
            "status": "terminal",
            "error_type": "unsupported_attachment_type",
            "retry_allowed": False,
            "recommended_action": "final_answer",
            "blocked_action": "call_tool:read_attachment",
            "user_message_hint": (
                "附件格式当前不在可读取范围内。请上传 txt、md、json、csv、log、xml、yaml、yml、pdf、jpg、jpeg、png、tif、tiff、webp、gif 或 bmp。"
            ),
        }
    if name == "read_attachment" and ("does not exist" in text or "not declared" in text):
        return {
            "status": "terminal",
            "error_type": "attachment_missing",
            "retry_allowed": False,
            "recommended_action": "final_answer",
            "blocked_action": "call_tool:read_attachment",
            "user_message_hint": "附件路径不存在或本轮请求没有声明该附件，请重新上传。",
        }
    if name == "read_attachment" and ("unicodedecodeerror" in text or "could not decode attachment as text" in text):
        return {
            "status": "terminal",
            "error_type": "attachment_decode_error",
            "retry_allowed": False,
            "recommended_action": "final_answer",
            "blocked_action": "call_tool:read_attachment",
            "user_message_hint": (
                "Attachment text could not be decoded. Convert it to UTF-8 txt, md, json, csv, log, xml, yaml, or yml and resubmit."
            ),
        }
    if "fileboundaryerror" in text or "path escapes" in text or "unsafe case_id" in text:
        return {
            "status": "terminal",
            "error_type": "unsafe_workspace_path",
            "retry_allowed": False,
            "recommended_action": "final_answer",
            "blocked_action": _runtime_blocked_action(name),
            "user_message_hint": (
                "The requested path was rejected by workspace boundary checks. Use a path inside the current case workspace."
            ),
        }
    if name == "write_case_file" and (
        "noexecutionwordingerror" in text or "blocked execution wording" in text
    ):
        return {
            "status": "terminal",
            "error_type": "report_execution_wording",
            "retry_allowed": False,
            "recommended_action": "final_answer",
            "blocked_action": "call_tool:write_case_file",
            "user_message_hint": (
                "Report content was not written because it contained ERP execution or payment wording. "
                "Revise the report to describe local review findings only."
            ),
        }
    if name == "render_pdf" and (
        "filenotfounderror" in text or "no such file or directory" in text or "does not exist" in text
    ):
        return {
            "status": "terminal",
            "error_type": "render_source_missing",
            "retry_allowed": False,
            "recommended_action": "final_answer",
            "blocked_action": "call_tool:render_pdf",
            "user_message_hint": (
                "PDF rendering could not continue because the source Markdown report file was not found. "
                "Write or regenerate the local Markdown report before rendering a PDF."
            ),
        }
    if name == "write_case_patch" and "tool input too large" in text:
        return {
            "status": "terminal",
            "error_type": "patch_input_too_large",
            "retry_allowed": False,
            "recommended_action": "final_answer",
            "blocked_action": "write_case_patch",
            "user_message_hint": (
                "The case patch was too large to apply safely. Ask for a smaller evidence batch or split the patch."
            ),
        }
    return {
        "status": "retryable",
        "error_type": "unknown_runtime_error",
        "retry_allowed": True,
        "recommended_action": "retry_or_final_answer",
        "blocked_action": "",
        "user_message_hint": "",
    }


def _runtime_blocked_action(name: str) -> str:
    if name == "write_case_patch":
        return "write_case_patch"
    return f"call_tool:{name}" if name else ""


def step_budget_runtime_feedback() -> dict[str, Any]:
    return {
        "status": "terminal",
        "error_type": "step_budget_near_limit",
        "retry_allowed": False,
        "recommended_action": "final_answer",
        "blocked_action": "",
        "user_message_hint": "步数即将耗尽，请基于已有 observation 给用户明确下一步。",
    }


def _latest_runtime_feedback(observations: list[dict[str, Any]]) -> dict[str, Any]:
    for observation in reversed(observations):
        if not isinstance(observation, dict):
            continue
        feedback = observation.get("runtime_feedback")
        if isinstance(feedback, dict) and feedback:
            return dict(feedback)
    return {}
