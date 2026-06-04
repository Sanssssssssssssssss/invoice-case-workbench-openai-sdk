from __future__ import annotations

import json
import re
from typing import Any

from app.state.case_store import CaseStore, utc_now


class ArtifactStore:
    def __init__(self, store: CaseStore) -> None:
        self.store = store

    def save(self, case_id: str, run_id: str, artifact_type: str, name: str, payload: Any) -> str:
        root = self.store.resolve_case_path(case_id, f"traces/artifacts/{run_id}")
        root.mkdir(parents=True, exist_ok=True)
        index = len(list(root.glob("art_*.json"))) + 1
        filename = f"art_{index:03d}_{_safe_slug(artifact_type)}_{_safe_slug(name)}.json"
        relative_path = f"traces/artifacts/{run_id}/{filename}"
        target = self.store.resolve_case_path(case_id, relative_path)
        envelope = {
            "artifact_type": artifact_type,
            "name": name,
            "run_id": run_id,
            "case_id": case_id,
            "created_at": utc_now(),
            "payload": payload,
        }
        target.write_text(json.dumps(envelope, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return relative_path

    def read(self, case_id: str, artifact_ref: str) -> Any:
        path = self.store.resolve_case_path(case_id, artifact_ref)
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("payload")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())[:60].strip("._")
    return slug or "artifact"
