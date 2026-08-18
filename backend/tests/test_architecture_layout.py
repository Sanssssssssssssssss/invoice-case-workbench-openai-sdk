from __future__ import annotations

from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"


def test_refactored_agent_prompt_layout_exists() -> None:
    expected = [
        APP / "runtime" / "turn_runner.py",
        APP / "runtime" / "policy_gate.py",
        APP / "runtime" / "supervisor_contract.py",
        APP / "runtime" / "tool_runtime.py",
        APP / "runtime" / "trace_recorder.py",
        APP / "runtime" / "checkpoints.py",
        APP / "agents" / "manager.py",
        APP / "agents" / "planner" / "prompt.md",
        APP / "compiler_runtime" / "prompts" / "task_compiler.md",
        APP / "compiler_runtime" / "prompts" / "executor.md",
        APP / "compiler_runtime" / "prompts" / "verifier.md",
        APP / "agents" / "materials_advisor" / "prompt.md",
        APP / "agents" / "patch_builder" / "deterministic.py",
        APP / "agents" / "report_writer" / "guard.py",
        APP / "domain" / "invoice_requirements.py",
        APP / "domain" / "matching_rules.py",
        APP / "state" / "case_state.py",
        APP / "state" / "evidence_store.py",
        APP / "tools" / "read_attachment.py",
        APP / "tools" / "write_report.py",
        APP / "skills" / "registry.py",
        APP / "evals" / "oracle.py",
    ]

    missing = [str(path.relative_to(APP)) for path in expected if not path.exists()]
    assert missing == []


def test_legacy_wrapper_paths_are_removed() -> None:
    legacy_paths = [
        APP / "graph.py",
        APP / "planner.py",
        APP / "runtime_controller.py",
        APP / "orchestration" / "graph.py",
        APP / "orchestration" / "graph_state.py",
        APP / "orchestration" / "supervisor_runtime.py",
        APP / "tool_runtime.py",
        APP / "artifacts.py",
        APP / "session_store.py",
        APP / "state" / "session_store.py",
        APP / "roles",
        APP / "prompts",
    ]

    assert [str(path.relative_to(APP)) for path in legacy_paths if path.exists()] == []
