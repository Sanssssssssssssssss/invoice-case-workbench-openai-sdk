from __future__ import annotations

import json
from dataclasses import asdict, fields
from typing import Any

from app.harness import HarnessRunState
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest


class RuntimeCheckpointStore:
    """Persist resumable SDK and harness state outside the model runtime."""

    def __init__(self, store: CaseStore) -> None:
        self.store = store

    def save(
        self,
        *,
        state: HarnessRunState,
        request: AgentTurnRequest,
        sdk_state: str = "",
        interruptions: list[dict[str, Any]] | None = None,
    ) -> None:
        payload = {
            "run_state": asdict(state),
            "request": request.model_dump(mode="json"),
            "sdk_state": sdk_state,
            "interruptions": interruptions or [],
        }
        path = self.store.resolve_case_path(state.case_id, f"traces/{state.run_id}/runtime_state.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def load(self, case_id: str, run_id: str) -> tuple[HarnessRunState, AgentTurnRequest, str, list[dict[str, Any]]]:
        case_id = self.store.validate_case_id(case_id)
        path = self.store.resolve_case_path(case_id, f"traces/{run_id}/runtime_state.json")
        if not path.exists():
            raise FileNotFoundError(run_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = _run_state_from_json(payload.get("run_state") or {})
        request = AgentTurnRequest.model_validate(payload.get("request") or {"case_id": case_id, "message": ""})
        sdk_state = str(payload.get("sdk_state") or "")
        interruptions = payload.get("interruptions") if isinstance(payload.get("interruptions"), list) else []
        return state, request, sdk_state, [item for item in interruptions if isinstance(item, dict)]


def _run_state_from_json(data: dict[str, Any]) -> HarnessRunState:
    names = {field.name for field in fields(HarnessRunState)}
    kwargs = {key: value for key, value in dict(data or {}).items() if key in names}
    return HarnessRunState(**kwargs)
