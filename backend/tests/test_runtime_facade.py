from __future__ import annotations

from app.runtime.context_assembler import ContextAssembler
from app.runtime.patch_normalizer import PatchNormalizer
from app.runtime.policy_gate import PolicyGate
from app.runtime.recovery_policy import RecoveryPolicy
from app.runtime.tool_runtime import ToolRuntime
from app.runtime.trace_recorder import TraceRecorder
from app.runtime.turn_runner import AgentRuntime, TurnRunner
from app.skills import SkillRegistry
from app.state.case_store import CaseStore
from app.tools.catalog import ToolCatalog


def test_agent_runtime_exposes_sdk_runtime_components(tmp_path) -> None:
    runtime = AgentRuntime(store=CaseStore(tmp_path / "cases"))
    runner = runtime.runner

    assert isinstance(runner, TurnRunner)
    assert isinstance(runner.context_assembler, ContextAssembler)
    assert isinstance(runner.policy_gate, PolicyGate)
    assert isinstance(runner.tool_runtime, ToolRuntime)
    assert isinstance(runner.tools, ToolCatalog)
    assert isinstance(runner.recovery, RecoveryPolicy)
    assert isinstance(runner.trace_recorder, TraceRecorder)
    assert isinstance(runner.patch_normalizer, PatchNormalizer)
    assert isinstance(runner.skills, SkillRegistry)


def test_legacy_runtime_modules_are_removed() -> None:
    import app.orchestration as orchestration

    assert not hasattr(orchestration, "routes")
    assert not hasattr(orchestration, "runtime_controller")
    assert not hasattr(orchestration, "graph")
