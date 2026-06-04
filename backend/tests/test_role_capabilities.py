from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

from app.agents.capabilities import ROLE_CAPABILITIES, role_prompt_version
from app.agents.registry import RoleRegistry
from app.context import ContextManager
from app.harness import HarnessRuntime
from app.llm import LlmClient
from app.state.case_store import CaseStore
from app.state.schemas import RoleName


def test_all_roles_have_capability_metadata() -> None:
    assert set(ROLE_CAPABILITIES) == set(get_args(RoleName))
    registry = RoleRegistry(LlmClient())

    assert set(registry.role_names) == set(ROLE_CAPABILITIES)
    for name, capability in ROLE_CAPABILITIES.items():
        metadata = capability.trace_metadata()
        assert registry.capability(name) == capability
        assert registry.prompt_file(name) == capability.prompt_file
        assert registry.prompt_version(name) == capability.prompt_version
        assert registry.trace_metadata(name) == metadata
        assert metadata["output_model"] == capability.output_model.__name__
        assert metadata["prompt_version"] == capability.prompt_version
        assert metadata["context_policy"]
        assert metadata["max_retries"] == 1
        assert metadata["allowed_tools"] == []
        assert metadata["side_effects"] == "none"


def test_role_capability_input_keys_are_context_filter_source(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    context = ContextManager(store, LlmClient())
    harness = HarnessRuntime(store)

    for role, capability in ROLE_CAPABILITIES.items():
        case_id = f"case_{role}"
        state = harness.begin_run(case_id, "role input")
        case_state = store.load(case_id)
        payload = {key: f"value_for_{key}" for key in capability.input_keys}
        payload["unexpected_key"] = "drop me"

        hydrated = context.build_role_context(role=role, state=state, payload=payload, user_message="test", case_state=case_state)

        assert "unexpected_key" not in hydrated
        assert set(hydrated).issubset(set(capability.input_keys))


def test_role_agents_use_manifest_prompt_versions() -> None:
    class CaptureLlm(LlmClient):
        @property
        def available(self) -> bool:
            return True

        def complete_structured(
            self,
            *,
            role: str,
            system_prompt: str,
            payload: dict[str, Any],
            model_type,
            prompt_version: str = "v1",
            model: str | None = None,
        ):
            self.calls.append(
                type(
                    "Call",
                    (),
                    {
                        "role": role,
                        "prompt_version": prompt_version,
                        "to_debug_dict": lambda self: {"role": role, "prompt_version": prompt_version},
                    },
                )()
            )
            if role == "materials_advisor":
                return model_type.model_validate({"answer": "ok", "missing_materials": [], "next_questions": [], "tasks": []})
            if role == "evidence_reviewer":
                return model_type.model_validate(
                    {
                        "mode": "review",
                        "evidence_type": "unknown",
                        "credibility": "low",
                        "supports": [],
                        "conflicts": [],
                        "risk_flags": [],
                        "should_accept": False,
                        "extracted_fields": {},
                        "source_traceability": "unclear",
                        "suggested_patch": {},
                    }
                )
            if role == "case_patch_writer":
                return model_type.model_validate({"patch_type": "no_change", "case_updates": {}, "audit_note": ""})
            if role == "report_writer":
                return model_type.model_validate({"markdown": "# ok", "title": "ok"})
            raise AssertionError(role)

    llm = CaptureLlm()
    registry = RoleRegistry(llm)
    common_case_state = {"case_id": "case_prompt_version", "requirements": [], "evidence_items": []}

    registry.call("materials_advisor", {"user_question": "q", "case_state": common_case_state})
    registry.call("evidence_reviewer", {"user_message": "q", "case_state": common_case_state})
    registry.call("case_patch_writer", {"role_result": {}, "case_state": common_case_state})
    registry.call("report_writer", {"case_state": common_case_state})

    versions = {call.role: call.prompt_version for call in llm.calls}
    for role in ROLE_CAPABILITIES:
        assert versions[role] == role_prompt_version(role)


def test_role_trace_metadata_can_be_recorded_in_role_call_and_context_manifest(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    llm = LlmClient()
    harness = HarnessRuntime(store)
    context = ContextManager(store, llm)
    state = harness.begin_run("case_role_trace", "advisor")
    metadata = ROLE_CAPABILITIES["materials_advisor"].trace_metadata()
    role_input = context.build_role_context(
        role="materials_advisor",
        state=state,
        payload={"user_question": "缺什么？"},
        user_message="缺什么？",
        case_state=store.load("case_role_trace"),
    )

    harness.record_role_call(
        state,
        "materials_advisor",
        role_input,
        {"answer": "ok", "missing_materials": [], "next_questions": [], "tasks": []},
        capability=metadata,
    )
    context.write_context_manifest(
        state,
        target="role:materials_advisor",
        context_payload=role_input,
        included=list(role_input.keys()),
        excluded=[],
        model="fake",
        prompt_file=ROLE_CAPABILITIES["materials_advisor"].prompt_file,
        system_prompt="system prompt",
        metadata={"role_capability": metadata},
    )

    assert state.role_calls[0]["capability"]["prompt_version"] == ROLE_CAPABILITIES["materials_advisor"].prompt_version
    manifest_path = store.resolve_case_path("case_role_trace", f"traces/{state.run_id}/context_manifest_000_role_materials_advisor.json")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    role_capability = manifest["metadata"]["role_capability"]
    assert role_capability["output_model"] == "MaterialsAdvisorResult"
    assert role_capability["side_effects"] == "none"
