from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.compiler_runtime.runtime import (
    EvidenceCompilerRuntime,
    expand_active_requirements,
    policy_excerpt_for,
    prepare_sources,
)
from app.compiler_runtime.transcript import ModelTranscriptHooks
from app.config import PROJECT_ROOT, get_settings
from app.harness import HarnessRuntime
from app.llm import LlmClient
from app.state.case_store import CaseStore


FREEZE_SEED = "compiler-runtime-freeze-20260821"
LIVE_ROOT = PROJECT_ROOT / "benchmarks" / "invoice_tau" / "live_acceptance"


class ConnectionPing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    message: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one deliberately small Evidence Compiler live gate.")
    parser.add_argument(
        "--stage",
        required=True,
        choices=("connect", "plan", "execute", "hooks", "verify", "e2e"),
    )
    parser.add_argument("--case-id", default="")
    parser.add_argument(
        "--canary",
        default="",
        choices=("amount_conflict", "duplicate_reversal", "partial_receipt", "vendor_identity_active"),
        help="Run a specific live-acceptance canary instead of the reproducible random pick.",
    )
    args = parser.parse_args()

    settings = get_settings()
    if settings.llm_provider.lower() != "deepseek":
        raise RuntimeError(f"Compiler live gates require provider=deepseek, got {settings.llm_provider!r}")
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is required for a live Compiler gate")

    case_id = args.case_id or f"compiler_gate_{args.stage}"
    store = CaseStore(settings.workspace_root)
    harness = HarnessRuntime(store)
    state = harness.begin_run(case_id, f"compiler gate: {args.stage}")
    hooks = ModelTranscriptHooks(
        harness,
        state,
        prompt_version=f"compiler_gate_{args.stage}_v1",
        secret_values=[settings.llm_api_key],
    )
    llm = LlmClient(settings)

    if args.stage == "connect":
        output = llm.complete_structured(
            role="compiler_connect",
            system_prompt="Return the requested structured connectivity acknowledgement. Do not call tools.",
            payload={"request": "Confirm this live provider request with status=ok and a short visible message."},
            model_type=ConnectionPing,
            prompt_version="compiler_connect_v1",
            hooks=hooks,
        ).model_dump(mode="json")
        selection = None
    else:
        selection = _select_canary(args.stage, args.canary)
        harness.append_debug_event(
            state,
            kind="compiler_gate_selection",
            name=args.stage,
            payload=selection["selection"],
            summary=f"compiler gate {args.stage}: {selection['selection']['picked_id']}",
        )
        runtime = EvidenceCompilerRuntime(llm, hooks=hooks, settings=settings)
        active_ids = expand_active_requirements(selection["requirement_ids"])
        policy = policy_excerpt_for(active_ids)
        prepared = prepare_sources(selection["source_items"])
        if args.stage == "plan":
            plan = runtime.compile_task(
                active_requirement_ids=active_ids,
                policy_excerpt=policy,
                source_catalog=[
                    {
                        "source_id": item.record.source_id,
                        "title": item.record.title,
                        "kind": item.record.kind,
                        "characters": len(item.record.content),
                    }
                    for item in prepared
                ],
                extraction_summary=[],
            )
            output = {"plan": plan.model_dump(mode="json")}
        elif args.stage == "execute":
            plan = runtime.compile_task(
                active_requirement_ids=active_ids,
                policy_excerpt=policy,
                source_catalog=[
                    {
                        "source_id": item.record.source_id,
                        "title": item.record.title,
                        "kind": item.record.kind,
                        "characters": len(item.record.content),
                    }
                    for item in prepared
                ],
            )
            summary, sandbox = runtime.execute_plan(
                plan=plan,
                prepared_sources=prepared,
                policy_excerpt=policy,
            )
            check_ids = {node.id for node in plan.nodes if node.kind == "CHECK"}
            submitted_ids = {item.check_id for item in sandbox.submissions}
            if submitted_ids != check_ids:
                raise RuntimeError(
                    f"Executor did not submit every CHECK: missing={sorted(check_ids - submitted_ids)}"
                )
            if not sandbox.read_source_ids or not sandbox.evidence_ir.claims:
                raise RuntimeError("Executor did not read a source and bind at least one grounded Claim")
            output = {
                "plan": plan.model_dump(mode="json"),
                "executor_summary": summary.model_dump(mode="json"),
                "evidence_ir": sandbox.evidence_ir.model_dump(mode="json"),
                "read_source_ids": list(sandbox.read_source_ids),
                "submitted_check_ids": sorted(submitted_ids),
            }
        else:
            result = runtime.run(
                active_requirement_ids=active_ids,
                prepared_sources=prepared,
                policy_excerpt=policy,
            )
            expected_check = _assert_expected_proof(result.proof, selection["expected"])
            output = {
                "artifact": result.artifact.model_dump(mode="json"),
                "proof": result.proof.model_dump(mode="json"),
                "retry_count": result.retry_count,
                "review_result": result.review_result,
                "expected_check": expected_check,
            }

    hooks.flush()
    transcript = hooks.transcript_path
    blocks = transcript.read_text(encoding="utf-8").count("调用 ")
    if hooks.call_count != blocks:
        raise RuntimeError(f"Provider-call/TXT mismatch: calls={hooks.call_count}, blocks={blocks}")
    result_payload = {
        "stage": args.stage,
        "case_id": case_id,
        "run_id": state.run_id,
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "provider_calls": hooks.call_count,
        "transcript": str(transcript),
        "selection": selection["selection"] if selection else None,
        "output": output,
    }
    print(json.dumps(result_payload, ensure_ascii=False, indent=2))
    return 0


def _select_canary(stage: str, canary_id: str = "") -> dict[str, object]:
    candidates = sorted(
        path for path in LIVE_ROOT.iterdir() if path.is_dir() and (path / "scenario.json").exists()
    )
    if not candidates:
        raise FileNotFoundError(f"No live Compiler canaries under {LIVE_ROOT}")
    digest = hashlib.sha256(f"{FREEZE_SEED}:{stage}".encode("utf-8")).hexdigest()
    if canary_id:
        picked = next((path for path in candidates if path.name == canary_id), None)
        if picked is None:
            raise ValueError(f"Unknown live Compiler canary: {canary_id}")
    else:
        picked = random.Random(int(digest, 16)).choice(candidates)
    scenario = json.loads((picked / "scenario.json").read_text(encoding="utf-8"))
    expected = json.loads((picked / "expected.json").read_text(encoding="utf-8"))
    requirements = [
        str(item.get("id") or "")
        for item in (scenario.get("initial_case_state") or {}).get("requirements") or []
        if str(item.get("id") or "")
    ]
    source_items = []
    for index, path in enumerate(sorted((picked / "attachments").iterdir())):
        if not path.is_file():
            continue
        source_items.append(
            {
                "attachment_id": f"gate_{picked.name}_{index + 1}",
                "name": path.name,
                "content_kind": _fixture_kind(path.stem),
                "content": path.read_text(encoding="utf-8"),
                "original_ref": f"benchmarks/invoice_tau/live_acceptance/{picked.name}/attachments/{path.name}",
            }
        )
    return {
        "selection": {
            "freeze_seed": FREEZE_SEED,
            "gate_name": stage,
            "seed_sha256": digest,
            "candidate_ids": [path.name for path in candidates],
            "picked_id": picked.name,
        },
        "requirement_ids": requirements,
        "source_items": source_items,
        "expected": expected,
    }


def _assert_expected_proof(proof: object, expected: object) -> dict[str, object]:
    if not isinstance(expected, dict):
        raise TypeError("Canary expected.json must contain an object")
    requirement_id = str(expected.get("proof_requirement_id") or "")
    expected_status = str(expected.get("decision_status") or "")
    decisions = {
        item.requirement_id: item
        for item in getattr(proof, "decisions", [])
    }
    decision = decisions.get(requirement_id)
    if decision is None:
        raise RuntimeError(f"Expected proof decision missing for {requirement_id!r}")
    if decision.status != expected_status:
        raise RuntimeError(
            f"Decision mismatch for {requirement_id}: expected={expected_status}, actual={decision.status}"
        )

    scoped_obligations = [
        item
        for item in getattr(proof, "obligations", [])
        if item.requirement_id == requirement_id
    ]
    expected_blocking = bool(expected.get("proof_has_blocking_obligations", False))
    actual_blocking = bool(scoped_obligations)
    if actual_blocking != expected_blocking:
        raise RuntimeError(
            f"Blocking-obligation mismatch for {requirement_id}: "
            f"expected={expected_blocking}, actual={actual_blocking}"
        )

    minimum_sources = int(expected.get("proof_min_source_count") or 0)
    node_results = {
        item.node_id: item
        for item in getattr(proof, "node_results", [])
    }
    root = node_results.get(decision.root_node_id)
    source_count = len(root.source_ids) if root is not None else 0
    if source_count < minimum_sources:
        raise RuntimeError(
            f"Proof source coverage too small for {requirement_id}: "
            f"expected>={minimum_sources}, actual={source_count}"
        )
    return {
        "requirement_id": requirement_id,
        "decision_status": decision.status,
        "blocking_obligations": actual_blocking,
        "root_source_count": source_count,
        "passed": True,
    }


def _fixture_kind(stem: str) -> str:
    value = stem.lower()
    if value == "invoice":
        return "invoice"
    if value == "po":
        return "purchase_order"
    if value == "grn":
        return "goods_receipt"
    if value == "vendor":
        return "vendor_record"
    if value in {"duplicate_search", "payment_history", "reversal_record"}:
        return "duplicate_payment_check"
    return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
