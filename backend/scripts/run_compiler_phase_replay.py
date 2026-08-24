from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.compiler_runtime.models import ProofPlan  # noqa: E402
from app.agents.thinking import role_thinking_type  # noqa: E402
from app.compiler_runtime.runtime import (  # noqa: E402
    EvidenceCompilerRuntime,
    VerificationBatch,
    _ExecutorConversation,
    _initial_sandbox,
    _planning_source_documents,
    _submitted_proof_terms,
    _task_compiler_repair_payload,
    _transitive_upstream_check_ids,
    _verifier_contracts,
    prepare_sources,
)
from app.config import get_settings  # noqa: E402
from app.llm import LlmClient  # noqa: E402


ROLE_CONFIG = {
    "task_compiler": ("task_compiler", "task_compiler.md", ProofPlan),
    "fine_verifier": ("fine_verifier", "verifier.md", VerificationBatch),
    "executor": ("executor", "executor.md", None),
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay one frozen TaskCompiler or Verifier model phase without running a case.",
    )
    parser.add_argument("events", type=Path, help="Run-local debug events.jsonl")
    parser.add_argument("--role", required=True, choices=tuple(ROLE_CONFIG))
    parser.add_argument("--index", type=int, default=0, help="Zero-based matching call index")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--thinking", choices=("inherit", "disabled", "high"), default="inherit")
    parser.add_argument("--model", default="")
    parser.add_argument("--check-id", default="", help="For Executor, select one frozen CHECK")
    parser.add_argument(
        "--source-snapshot",
        type=Path,
        help="For TaskCompiler, add complete source documents from a run snapshot.",
    )
    parser.add_argument(
        "--repair-source-draft",
        action="store_true",
        help="For TaskCompiler, replay only the repair of the frozen raw draft and its current Gate error.",
    )
    args = parser.parse_args()

    model_call = load_model_call(
        args.events,
        role=args.role,
        index=args.index,
        check_id=args.check_id,
    )
    phase_payload = model_call.get("payload")
    if not isinstance(phase_payload, dict):
        raise ValueError("Selected model call has no complete phase payload")
    if args.source_snapshot and args.role == "task_compiler":
        snapshot = json.loads(args.source_snapshot.read_text(encoding="utf-8"))
        evidence_items = ((snapshot.get("case_state") or {}).get("evidence_items") or [])
        if not evidence_items:
            evidence_items = snapshot.get("evidence_items") or []
        phase_payload = {
            **phase_payload,
            "source_documents": _planning_source_documents(evidence_items),
        }
    elif args.role == "executor" and not args.source_snapshot:
        parser.error("--source-snapshot is required for executor")

    settings = get_settings()
    updates: dict[str, Any] = {}
    if args.thinking != "inherit":
        updates["llm_thinking_type"] = args.thinking
    if args.model:
        updates["llm_model"] = args.model
    if updates:
        settings = settings.model_copy(update=updates)
    llm = LlmClient(settings)
    runtime = EvidenceCompilerRuntime(llm, settings=settings)
    name, prompt_file, output_type = ROLE_CONFIG[args.role]
    effective_thinking = (
        role_thinking_type(name, phase_payload, settings.llm_thinking_type)
        if args.thinking == "inherit"
        else args.thinking
    )
    if args.repair_source_draft:
        if args.role != "task_compiler":
            parser.error("--repair-source-draft is only valid for task_compiler")
        previous_draft = ProofPlan.model_validate_json(str(model_call.get("raw_response") or ""))
        required = phase_payload.get("required_output") or {}
        try:
            runtime._normalize_and_validate_task_plan(
                previous_draft,
                requirement_ids=list(required.get("active_requirement_ids") or []),
                task_objective=str(required.get("objective") or "").strip(),
            )
        except ValueError as exc:
            phase_payload = _task_compiler_repair_payload(phase_payload, exc, previous_draft)
        else:
            raise ValueError("Frozen TaskCompiler draft already passes the current Gate")
    elif args.role == "fine_verifier":
        phase_payload = {
            **phase_payload,
            "verification_contracts": _verifier_contracts(phase_payload.get("checks") or []),
        }

    result: dict[str, Any] = {
        "schema_version": "1",
        "replayed_at": datetime.now(timezone.utc).isoformat(),
        "role": args.role,
        "source": {
            "events": str(args.events),
            "index": args.index,
            "run_id": model_call.get("run_id", ""),
            "prompt_version": model_call.get("prompt_version", ""),
            "prompt_sha256": _digest(model_call.get("system_prompt", "")),
            "payload_sha256": _digest(phase_payload),
            "raw_response_sha256": _digest(model_call.get("raw_response", "")),
        },
        "configuration": {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "thinking": effective_thinking,
            "temperature": settings.llm_temperature,
            "repair_source_draft": args.repair_source_draft,
            "source_snapshot": str(args.source_snapshot or ""),
        },
        "input_payload": phase_payload,
    }
    exit_code = 0
    try:
        if args.role == "executor":
            output = replay_executor(
                runtime,
                phase_payload=phase_payload,
                snapshot=json.loads(args.source_snapshot.read_text(encoding="utf-8")),
                check_id=args.check_id,
            )
        else:
            output = runtime._run_phase(
                name=name,
                prompt_file=prompt_file,
                payload=phase_payload,
                output_type=output_type,
                max_turns=1,
                thinking_override=None if args.thinking == "inherit" else args.thinking,
            )
        if args.role == "task_compiler":
            required = phase_payload.get("required_output") or {}
            output = runtime._normalize_and_validate_task_plan(
                output,
                requirement_ids=list(required.get("active_requirement_ids") or []),
                task_objective=str(required.get("objective") or "").strip(),
            )
        result["status"] = "passed"
        result["output"] = (
            output if isinstance(output, dict) else output.model_dump(mode="json")
        )
    except Exception as exc:
        exit_code = 1
        result["status"] = "failed"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}

    result["model_calls"] = [call.to_debug_dict() for call in llm.calls]
    args.output_dir.mkdir(parents=True, exist_ok=False)
    output_path = args.output_dir / "replay.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(output_path)}, ensure_ascii=False))
    return exit_code


def replay_executor(
    runtime: EvidenceCompilerRuntime,
    *,
    phase_payload: dict[str, Any],
    snapshot: dict[str, Any],
    check_id: str = "",
) -> dict[str, Any]:
    case_state = snapshot.get("case_state") or snapshot
    artifact = case_state.get("review_artifact") or {}
    plan = ProofPlan.model_validate(phase_payload.get("proof_plan") or artifact.get("plan"))
    policy = dict(phase_payload.get("policy") or {})
    prepared_sources = prepare_sources(_frozen_source_items(case_state))
    sandbox = _initial_sandbox(
        plan=plan,
        prepared_sources=prepared_sources,
        policy_excerpt=policy,
    )
    for source in sandbox.source_records:
        _require_ok(sandbox.read_source(source.source_id))

    focus = check_id.strip() or str((phase_payload.get("focus_check_ids") or [""])[0])
    if not focus:
        raise ValueError("Frozen Executor call has no focused CHECK")
    _seed_frozen_upstream(plan, focus, artifact, sandbox)
    upstream = _replay_upstream_results(plan, focus, sandbox)

    events: list[dict[str, Any]] = []

    def capture(kind: str, payload: dict[str, Any], _summary: str) -> None:
        events.append({"kind": kind, **payload})

    runtime.progress_sink = capture
    conversation = _ExecutorConversation(
        checkpoint=sandbox,
        sandbox=copy.deepcopy(sandbox),
    )
    summary, candidate = runtime.execute_plan(
        plan=plan,
        prepared_sources=prepared_sources,
        policy_excerpt=policy,
        sandbox=sandbox,
        focus_check_id=focus,
        upstream_frontier_results=upstream,
        conversation=conversation,
    )
    terms = _submitted_proof_terms(candidate, check_ids={focus})
    finished_tools = [item for item in events if item.get("kind") == "tool_finished"]
    rejected_tools = [item for item in finished_tools if item.get("status") == "rejected"]
    return {
        "check_id": focus,
        "summary": summary.model_dump(mode="json"),
        "provider_calls": conversation.provider_calls,
        "tool_calls": len(finished_tools),
        "tool_errors": len(rejected_tools),
        "tool_error_codes": [str(item.get("hook_code") or "") for item in rejected_tools],
        "target_terms": terms,
        "semantic_fingerprint": _digest(terms),
    }


def _seed_frozen_upstream(
    plan: ProofPlan,
    check_id: str,
    artifact: dict[str, Any],
    sandbox: Any,
) -> None:
    claims = {
        str(item.get("id") or ""): item
        for item in ((artifact.get("evidence_ir") or {}).get("claims") or [])
    }
    bindings = {
        str(item.get("id") or ""): item
        for item in (artifact.get("binding_proposals") or [])
    }
    witnesses = {
        str(item.get("id") or ""): item
        for item in (artifact.get("calculation_witnesses") or [])
    }
    witness_order = list(witnesses)
    submitted_claims = artifact.get("submitted_claim_refs") or {}
    submitted_bindings = artifact.get("submitted_binding_refs") or {}
    submitted_witnesses = artifact.get("submitted_witness_refs") or {}

    for upstream_id in _transitive_upstream_check_ids(plan, check_id):
        binding_ids = list(submitted_bindings.get(upstream_id) or [])
        witness_ids = list(submitted_witnesses.get(upstream_id) or [])
        needed_witnesses = _witness_closure(witness_ids, witnesses)
        claim_ids = set(submitted_claims.get(upstream_id) or [])
        for binding_id in binding_ids:
            for ref in (bindings.get(binding_id) or {}).get("term_refs") or []:
                if ref.get("kind") == "CLAIM":
                    claim_ids.add(str(ref.get("ref_id") or ""))
        for witness_id in needed_witnesses:
            for operand in (witnesses.get(witness_id) or {}).get("operands") or []:
                ref = operand.get("ref") or {}
                if ref.get("kind") == "CLAIM":
                    claim_ids.add(str(ref.get("ref_id") or ""))

        for claim_id in claims:
            if claim_id not in claim_ids:
                continue
            claim = claims[claim_id]
            _require_ok(
                sandbox.bind_claim(
                    subject=claim.get("subject", ""),
                    predicate=claim.get("predicate", ""),
                    value=claim.get("value"),
                    source_id=claim.get("source_id", ""),
                    quote=claim.get("quote", ""),
                    locator=claim.get("locator", ""),
                    confidence=claim.get("confidence", "medium"),
                    claim_id=claim_id,
                    attributes=claim.get("attributes") or {},
                )
            )
        for witness_id in witness_order:
            if witness_id not in needed_witnesses:
                continue
            witness = witnesses[witness_id]
            result = _require_ok(
                sandbox.compute_witness(
                    check_id=witness.get("check_id", ""),
                    facet_ref=witness.get("facet_ref", ""),
                    operation=witness.get("operation", ""),
                    refs=[item.get("ref") or {} for item in witness.get("operands") or []],
                )
            )
            actual_id = str((result.get("witness") or {}).get("id") or "")
            if actual_id != witness_id:
                raise ValueError(f"Frozen witness replay mismatch: expected={witness_id}, got={actual_id}")
        _require_ok(
            sandbox.submit_check(
                check_id=upstream_id,
                claim_ids=list(submitted_claims.get(upstream_id) or []),
                binding_proposals=[bindings[item] for item in binding_ids],
                witness_ids=witness_ids,
                note="frozen upstream replay",
            )
        )


def _frozen_source_items(case_state: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in case_state.get("evidence_items") or []:
        metadata = item.get("metadata") or {}
        source_id = str(metadata.get("source_doc_id") or "")
        fingerprint = str(metadata.get("source_fingerprint") or "")
        content = str(item.get("content") or "")
        if source_id and fingerprint and content:
            result.append(
                {
                    **metadata,
                    "already_persisted": True,
                    "source_id": source_id,
                    "source_content": content,
                    "source_fingerprint": fingerprint,
                    "name": metadata.get("source_filename") or item.get("title") or source_id,
                    "type": item.get("type") or "unknown",
                }
            )
        else:
            result.append(dict(item))
    return result


def _witness_closure(roots: list[str], witnesses: dict[str, dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    pending = list(roots)
    while pending:
        witness_id = pending.pop()
        if witness_id in result:
            continue
        witness = witnesses.get(witness_id)
        if witness is None:
            raise ValueError(f"Frozen witness not found: {witness_id}")
        result.add(witness_id)
        pending.extend(
            str(ref.get("ref_id") or "")
            for operand in witness.get("operands") or []
            if (ref := operand.get("ref") or {}).get("kind") == "WITNESS"
        )
    return result


def _replay_upstream_results(plan: ProofPlan, check_id: str, sandbox: Any) -> list[dict[str, Any]]:
    nodes = {node.id: node for node in plan.nodes}
    direct = set(nodes[check_id].upstream_check_ids)
    return [
        {
            "check_id": upstream_id,
            "direct_dependency": upstream_id in direct,
            "statement": nodes[upstream_id].statement,
            "facet_refs": list(nodes[upstream_id].facet_refs),
            "semantic_role_refs": list(nodes[upstream_id].semantic_role_refs),
            "committed": True,
            "accepted_terms": _submitted_proof_terms(sandbox, check_ids={upstream_id}),
        }
        for upstream_id in _transitive_upstream_check_ids(plan, check_id)
    ]


def _require_ok(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok") is not True:
        raise ValueError(f"Frozen sandbox replay failed: {result.get('error')}")
    return result


def load_model_call(
    events: Path,
    *,
    role: str,
    index: int = 0,
    check_id: str = "",
) -> dict[str, Any]:
    if index < 0:
        raise ValueError("index must be non-negative")
    matches: list[dict[str, Any]] = []
    for line in events.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("kind") != "model_call" or event.get("name") != role:
            continue
        payload = event.get("payload")
        phase_payload = payload.get("payload") if isinstance(payload, dict) else None
        focus_ids = phase_payload.get("focus_check_ids") if isinstance(phase_payload, dict) else []
        if isinstance(payload, dict) and (not check_id or check_id in (focus_ids or [])):
            matches.append({**payload, "run_id": event.get("run_id", "")})
    if index >= len(matches):
        raise IndexError(f"No {role!r} model call at index {index}; found {len(matches)}")
    return matches[index]


def _digest(value: Any) -> str:
    encoded = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
