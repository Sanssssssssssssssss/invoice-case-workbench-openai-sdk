from __future__ import annotations

import argparse
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
from app.compiler_runtime.runtime import (  # noqa: E402
    EvidenceCompilerRuntime,
    VerificationBatch,
    _task_compiler_repair_payload,
    _verifier_contracts,
)
from app.config import get_settings  # noqa: E402
from app.llm import LlmClient  # noqa: E402


ROLE_CONFIG = {
    "task_compiler": ("task_compiler", "task_compiler.md", ProofPlan),
    "fine_verifier": ("fine_verifier", "verifier.md", VerificationBatch),
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
    parser.add_argument(
        "--repair-source-draft",
        action="store_true",
        help="For TaskCompiler, replay only the repair of the frozen raw draft and its current Gate error.",
    )
    args = parser.parse_args()

    model_call = load_model_call(args.events, role=args.role, index=args.index)
    phase_payload = model_call.get("payload")
    if not isinstance(phase_payload, dict):
        raise ValueError("Selected model call has no complete phase payload")

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
            "thinking": settings.llm_thinking_type or "disabled",
            "temperature": settings.llm_temperature,
            "repair_source_draft": args.repair_source_draft,
        },
        "input_payload": phase_payload,
    }
    exit_code = 0
    try:
        output = runtime._run_phase(
            name=name,
            prompt_file=prompt_file,
            payload=phase_payload,
            output_type=output_type,
            max_turns=1,
        )
        if args.role == "task_compiler":
            required = phase_payload.get("required_output") or {}
            output = runtime._normalize_and_validate_task_plan(
                output,
                requirement_ids=list(required.get("active_requirement_ids") or []),
                task_objective=str(required.get("objective") or "").strip(),
            )
        result["status"] = "passed"
        result["output"] = output.model_dump(mode="json")
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


def load_model_call(events: Path, *, role: str, index: int = 0) -> dict[str, Any]:
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
        if isinstance(payload, dict):
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
