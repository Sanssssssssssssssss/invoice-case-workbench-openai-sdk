from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ExpectedSpec, REPO_ROOT, ScenarioSpec


SCENARIOS_ROOT = REPO_ROOT / "benchmarks" / "invoice_tau" / "scenarios"
REPORTS_ROOT = REPO_ROOT / "benchmarks" / "invoice_tau" / "reports"


def load_scenario(path: Path) -> tuple[ScenarioSpec, ExpectedSpec, Path]:
    if path.is_dir():
        scenario_path = path / "scenario.json"
        expected_path = path / "expected.json"
        scenario_dir = path
    else:
        scenario_path = path
        expected_path = path.with_name("expected.json")
        scenario_dir = path.parent
    scenario = ScenarioSpec.model_validate(_read_json(scenario_path))
    expected = ExpectedSpec.model_validate(_read_json(expected_path) if expected_path.exists() else {})
    return scenario, expected, scenario_dir


def discover_scenarios(root: Path = SCENARIOS_ROOT, names: list[str] | None = None) -> list[Path]:
    if names:
        result: list[Path] = []
        for name in names:
            candidate = root / name
            if not candidate.exists():
                raise FileNotFoundError(f"Scenario not found: {candidate}")
            result.append(candidate)
        return result
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and (path / "scenario.json").exists())


def new_report_dir(prefix: str = "run") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    root = REPORTS_ROOT / f"{prefix}_{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"Expected JSON object in {path}")
    return data
