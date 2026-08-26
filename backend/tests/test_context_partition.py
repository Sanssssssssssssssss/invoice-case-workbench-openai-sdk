from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agents.manager import MANAGER_PROMPT
from app.agents.registry import RoleRegistry
from app.config import Settings
from app.context import ContextManager
from app.harness import HarnessRuntime
from app.llm import LlmClient
from app.runtime.context_partition import (
    build_context_packet,
    canonical_json,
    partition_context_payload,
    prompt_cache_model_settings_kwargs,
    usage_from_result,
    with_usage_metrics,
)
from app.state.case_store import CaseStore
from app.state.schemas import EvidenceReviewResult, Requirement
from app.tools.catalog import ToolCatalog


def _packet(role: str, *, context_payload: dict[str, Any] | None = None) -> Any:
    settings = Settings(llm_api_key="test", llm_base_url="https://api.openai.com/v1")
    registry = RoleRegistry(LlmClient(settings))
    if role == "planner":
        return build_context_packet(
            role="planner",
            prompt_version="supervisor_planner_v2.11_child_supervision",
            prompt_file="backend/app/agents/planner/prompt.md",
            system_prompt=MANAGER_PROMPT,
            context_payload=context_payload or {"user_message": "x", "context_pack": {"case_brief": "case", "current_goal": "goal"}},
            settings=settings,
            tool_catalog=ToolCatalog(),
        )
    capability = registry.capability(role)
    return build_context_packet(
        role=role,
        prompt_version=capability.prompt_version,
        prompt_file=capability.prompt_file,
        system_prompt=registry.prompt(role),
        context_payload=context_payload or {"case_state": {}, "memory_hints": [], "user_message": "x"},
        settings=settings,
        output_model=capability.output_model,
        role_contract=capability.trace_metadata(),
        tool_catalog=ToolCatalog(),
    )


def test_prompt_prefix_hash_snapshots_are_stable() -> None:
    expected = json.loads(Path("backend/tests/fixtures/prompt_prefix_hashes.json").read_text(encoding="utf-8"))
    actual = {}
    for role in ("planner", "materials_advisor", "case_patch_writer", "report_writer"):
        packet = _packet(role)
        actual[role] = {
            "stable_prefix_hash": packet.stable_prefix_hash,
            "tool_catalog_hash": packet.tool_catalog_hash,
            "skill_hash": packet.skill_hash,
            "tenant_policy_hash": packet.tenant_policy_hash,
            "prompt_cache_key": packet.prompt_cache_key,
        }

    assert actual == expected


def test_stable_prefix_excludes_run_specific_context() -> None:
    packet = _packet(
        "planner",
        context_payload={
            "user_message": "review case_a",
            "context_pack": {
                "case_brief": "case_id=case_a; status=new",
                "current_goal": "run run_abc123456789",
                "recent_observations": [{"artifact_ref": "traces/artifacts/run_abc123456789/art.json"}],
            },
        },
    )
    stable_text = canonical_json(packet.stable_prefix)

    assert "case_a" not in stable_text
    assert "run_abc123456789" not in stable_text
    assert packet.dynamic_context_hash
    assert packet.volatile_tail_hash


def test_partition_hashes_change_only_for_their_layer() -> None:
    base = _packet("planner")
    dynamic_changed = _packet("planner", context_payload={"user_message": "x", "context_pack": {"case_brief": "case changed", "current_goal": "goal"}})
    volatile_changed = _packet("planner", context_payload={"user_message": "x", "context_pack": {"case_brief": "case", "current_goal": "new goal"}})

    assert base.stable_prefix_hash == dynamic_changed.stable_prefix_hash == volatile_changed.stable_prefix_hash
    assert base.prompt_cache_key == dynamic_changed.prompt_cache_key == volatile_changed.prompt_cache_key
    assert base.dynamic_context_hash != dynamic_changed.dynamic_context_hash
    assert base.volatile_tail_hash != volatile_changed.volatile_tail_hash


def test_prompt_cache_key_binds_stable_prefix_hash() -> None:
    base = _packet("planner")
    changed = build_context_packet(
        role="planner",
        prompt_version="supervisor_planner_v2.6_native_tools",
        prompt_file="backend/app/agents/planner/prompt.md",
        system_prompt=MANAGER_PROMPT + "\nAdditional stable instruction.",
        context_payload={"user_message": "x", "context_pack": {"case_brief": "case", "current_goal": "goal"}},
        settings=Settings(llm_api_key="test", llm_base_url="https://api.openai.com/v1"),
        tool_catalog=ToolCatalog(),
    )

    assert base.stable_prefix_hash != changed.stable_prefix_hash
    assert base.prompt_cache_key != changed.prompt_cache_key
    assert base.stable_prefix_hash[:24] in base.prompt_cache_key


def test_unknown_context_keys_warn_in_prod_and_fail_in_strict() -> None:
    packet = build_context_packet(
        role="planner",
        prompt_version="supervisor_planner_v2.6_native_tools",
        prompt_file="backend/app/agents/planner/prompt.md",
        system_prompt=MANAGER_PROMPT,
        context_payload={"context_pack": {"case_brief": "case", "current_user_message_raw": "raw"}},
        settings=Settings(llm_api_key="test"),
        tool_catalog=ToolCatalog(),
    )

    manifest = packet.manifest_metadata()
    assert manifest["unknown_context_keys"] == ["current_user_message_raw"]
    assert manifest["partition_policy_warnings"]
    assert packet.dynamic_context["other_context"]["current_user_message_raw"] == "raw"

    try:
        partition_context_payload({"current_user_message_raw": "raw"}, strict=True)
    except ValueError as exc:
        assert "current_user_message_raw" in str(exc)
    else:
        raise AssertionError("Expected strict partitioning to reject unknown keys")


def test_prompt_cache_settings_only_for_openai_responses() -> None:
    packet = _packet("planner")
    openai_settings = Settings(llm_api_key="test", llm_provider="openai", llm_base_url="https://api.openai.com/v1")
    moonshot_settings = Settings(llm_api_key="test", llm_provider="openai", llm_base_url="https://api.moonshot.cn/v1")

    kwargs = prompt_cache_model_settings_kwargs(openai_settings, packet)

    assert kwargs["prompt_cache_retention"] == "24h"
    assert kwargs["extra_args"]["prompt_cache_key"] == packet.prompt_cache_key
    assert prompt_cache_model_settings_kwargs(moonshot_settings, packet) == {}


def test_retired_reviewer_contract_context_is_rejected() -> None:
    try:
        partition_context_payload(
            {
                "active_requirement_contracts": [{"contract_id": "CTR_1"}],
                "typed_holes": [{"id": "HOL_1"}],
            },
            strict=True,
        )
    except ValueError as exc:
        assert "active_requirement_contracts" in str(exc)
        assert "typed_holes" in str(exc)
    else:
        raise AssertionError("Expected retired Reviewer context keys to be rejected")


def test_usage_cached_tokens_flow_into_partition_metadata() -> None:
    class Usage:
        def model_dump(self) -> dict[str, Any]:
            return {
                "input_tokens": 2000,
                "output_tokens": 100,
                "total_tokens": 2100,
                "input_tokens_details": {"cached_tokens": 1536},
            }

    class Response:
        usage = Usage()

    class Result:
        raw_responses = [Response()]

    usage = usage_from_result(Result())
    partition = with_usage_metrics(_packet("planner").debug_metadata(), usage)

    assert usage["cached_tokens"] == 1536
    assert partition["prompt_tokens"] == 2000
    assert partition["cached_tokens"] == 1536
    assert partition["cache_hit_ratio"] == 0.768


def test_partition_previews_are_local_manifest_only() -> None:
    packet = _packet("planner")

    assert "partition_previews" in packet.manifest_metadata()
    assert set(packet.manifest_metadata()["partition_previews"]) == {"stable_prefix"}
    assert packet.manifest_metadata()["partition_sections"]["dynamic_context"]["keys"]
    assert "partition_previews" not in packet.debug_metadata()


def test_role_registry_reuses_manifest_partition_metadata(monkeypatch) -> None:
    llm = LlmClient(Settings(llm_api_key="test"))
    registry = RoleRegistry(llm)
    partition = {
        "stable_prefix_hash": "manifest-stable",
        "dynamic_context_hash": "manifest-dynamic",
        "volatile_tail_hash": "manifest-volatile",
        "prompt_cache_key": "invoice_workbench:test:materials_advisor:v1:tools",
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "cache_hit_ratio": 0.0,
        "role": "materials_advisor",
        "prompt_version": "test",
    }

    def raise_after_metadata(*args, **kwargs):
        assert kwargs["prompt_partition"]["stable_prefix_hash"] == "manifest-stable"
        raise RuntimeError("stop before sdk call")

    monkeypatch.setattr("app.agents.registry.build_context_packet", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not rebuild packet")))
    monkeypatch.setattr(registry, "as_tool", raise_after_metadata)
    try:
        registry.call("materials_advisor", {"user_question": "what materials are missing?"}, prompt_partition=partition)
    except RuntimeError as exc:
        assert "stop before sdk call" in str(exc)
    else:
        raise AssertionError("Expected sentinel RuntimeError")

    assert llm.calls[-1].prompt_partition["stable_prefix_hash"] == "manifest-stable"


def test_context_manifest_records_and_updates_partition_usage(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    context = ContextManager(store, LlmClient(Settings(llm_api_key="test")))
    state = HarnessRuntime(store).begin_run("case_manifest", "test", run_id="run_manifest")
    context_payload = {"context_pack": {"case_brief": "case"}}
    partition = _packet("planner", context_payload=context_payload).manifest_metadata()

    ref = context.write_context_manifest(
        state,
        target="planner",
        context_payload=context_payload,
        included=["case_brief"],
        excluded=[],
        partition_metadata=partition,
    )
    context.update_context_manifest_usage(
        state,
        "planner",
        {**partition, "prompt_tokens": 2000, "cached_tokens": 1000, "cache_hit_ratio": 0.5},
    )

    data = json.loads(store.resolve_case_path(state.case_id, ref).read_text(encoding="utf-8"))
    assert data["stable_prefix_hash"] == partition["stable_prefix_hash"]
    previews = data["prompt_partition"]["partition_previews"]
    assert previews["stable_prefix"]["role"] == "planner"
    assert data["payload"]["context_pack"]["case_brief"] == "case"
    sections = data["prompt_partition"]["partition_sections"]
    assert sections["dynamic_context"]["keys"] == ["case_brief"]
    assert sections["volatile_tail"]["keys"] == []
    runtime_partition = context.prompt_partition_metadata(state, "planner")
    assert runtime_partition["stable_prefix_hash"] == partition["stable_prefix_hash"]
    assert "partition_previews" not in runtime_partition
    assert "unknown_context_keys" not in runtime_partition
    assert "partition_policy_warnings" not in runtime_partition
    assert data["prompt_tokens"] == 2000
    assert data["cached_tokens"] == 1000
    assert data["cache_hit_ratio"] == 0.5


def test_planner_recent_turns_exclude_current_turn(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    context = ContextManager(store, LlmClient(Settings(llm_api_key="test")))
    case_state = store.load("case_recent_turns")
    context.sessions.append_user_turn(case_state.case_id, "prior question", [], "run_prior")
    context.sessions.append_assistant_turn(case_state.case_id, "turn_001", "prior answer", "run_prior")
    current = context.sessions.append_user_turn(
        case_state.case_id,
        "current question",
        [{"name": "invoice.md", "path": "attachments/current_invoice.md"}],
        "run_current",
    )
    state = HarnessRuntime(store).begin_run(case_state.case_id, "current question", run_id="run_current")
    state.turn_id = str(current["turn_id"])
    state.user_message_for_planner = "current question"
    state.current_goal = "current question"

    planner_context = context.build_planner_context(
        state=state,
        case_state=case_state,
        session=context.sessions.load_session(case_state.case_id),
        attachments=current["attachments"],
    )

    assert [item["turn_id"] for item in planner_context["recent_turns"]] == ["turn_001"]
    assert "current question" not in json.dumps(planner_context["recent_turns"], ensure_ascii=False)
    assert planner_context["attachments"][0]["name"] == "invoice.md"


def test_memory_hints_are_capped_and_advisory(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    context = ContextManager(store, LlmClient(Settings(llm_api_key="test")))
    case_state = store.load("case_memory")
    case_state.requirements = [Requirement(id="vendor_identity", status="missing")]
    store.save(case_state)
    for index in range(8):
        context.memory.add_memory(
            case_id=case_state.case_id,
            memory_type="retrieval_memory",
            text=f"vendor_identity missing supplier onboarding hint {index} with duplicate payment AP policy context",
            source_ref=f"traces/artifacts/run_mem/art_{index}.json",
        )

    planner_state = HarnessRuntime(store).begin_run(case_state.case_id, "vendor hint", run_id="run_mem")
    planner_state.user_message_for_planner = "vendor hint"
    planner_hints = context.build_planner_context(
        state=planner_state,
        case_state=case_state,
        session=context.sessions.load_session(case_state.case_id),
        attachments=[],
    )["memory_hints"]
    role_state = HarnessRuntime(store).begin_run(case_state.case_id, "vendor hint", run_id="run_mem_2")
    role_state.user_message_for_planner = "vendor hint"
    role_hints = context.build_role_context(
        role="case_patch_writer",
        state=role_state,
        payload={},
        user_message="vendor hint",
        case_state=case_state,
    )["memory_hints"]

    assert len(planner_hints) <= 3
    assert sum(len(item["text"]) for item in planner_hints) <= 500
    assert len(role_hints) <= 5
    assert sum(len(item["text"]) for item in role_hints) <= 1200
    assert all(item["truth_status"] == "advisory" for item in planner_hints + role_hints)
    assert all(item["source_ref"] and item["relevance_score"] >= 0 for item in planner_hints + role_hints)
    assert all(item["score_terms"] and item["score_reason"] for item in planner_hints + role_hints)


def test_memory_hints_filter_low_score_and_stale_conflicts(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    context = ContextManager(store, LlmClient(Settings(llm_api_key="test")))
    case_state = store.load("case_memory_conflict")
    case_state.requirements = [Requirement(id="vendor_identity", status="missing")]
    store.save(case_state)
    context.memory.add_memory(
        case_id=case_state.case_id,
        memory_type="retrieval_memory",
        text="vendor_identity satisfied and complete based on an old vendor onboarding note",
        source_ref="traces/artifacts/run_old/vendor.json",
    )
    context.memory.add_memory(
        case_id=case_state.case_id,
        memory_type="retrieval_memory",
        text="cafeteria menu and office seating preferences",
        source_ref="traces/artifacts/run_old/noise.json",
    )

    planner_state = HarnessRuntime(store).begin_run(case_state.case_id, "vendor identity hint", run_id="run_mem_conflict")
    planner_state.user_message_for_planner = "vendor identity hint"
    hints = context.build_planner_context(
        state=planner_state,
        case_state=case_state,
        session=context.sessions.load_session(case_state.case_id),
        attachments=[],
    )["memory_hints"]

    assert hints == []


def test_memory_hints_record_expired_and_invalid_freshness(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    context = ContextManager(store, LlmClient(Settings(llm_api_key="test")))
    case_state = store.load("case_memory_freshness")
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    context.memory.add_memory(
        case_id=case_state.case_id,
        memory_type="retrieval_memory",
        text="duplicate payment vendor invoice policy context",
        source_ref="traces/artifacts/run_mem/expired.json",
        metadata={"expires_at": expired},
    )
    context.memory.add_memory(
        case_id=case_state.case_id,
        memory_type="retrieval_memory",
        text="duplicate payment vendor invoice invalid expiry context",
        source_ref="traces/artifacts/run_mem/invalid.json",
        metadata={"expires_at": "not-a-date"},
    )

    role_state = HarnessRuntime(store).begin_run(case_state.case_id, "duplicate payment vendor invoice", run_id="run_mem_fresh")
    role_state.user_message_for_planner = "duplicate payment vendor invoice"
    hints = context.build_role_context(
        role="materials_advisor",
        state=role_state,
        payload={},
        user_message="duplicate payment vendor invoice",
        case_state=case_state,
    )["memory_hints"]
    freshness = {item["source_ref"]: item["freshness"] for item in hints}

    assert freshness["traces/artifacts/run_mem/expired.json"] == "expired"
    assert freshness["traces/artifacts/run_mem/invalid.json"] == "invalid_expires_at"


def test_advisory_rag_or_memory_evidence_cannot_satisfy_requirements(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    updated = store.apply_patch(
        "case_advisory_evidence",
        {
            "patch_type": "add_evidence",
            "case_updates": {
                "requirements": [{"id": "invoice", "label": "Invoice", "kind": "document"}],
                "add_evidence": [
                    {
                        "type": "policy_excerpt",
                        "source": "rag",
                        "credibility": "high",
                        "summary": "RAG says invoice is required.",
                        "review_result": {"should_accept": True},
                        "supports": [{"requirement": "invoice", "support_level": "full"}],
                        "metadata": {"truth_status": "advisory", "boundary": "memory_hint_only_not_case_truth"},
                    }
                ],
            },
        },
    )

    assert updated.requirements[0].status != "satisfied"
    assert updated.requirements[0].evidence_ids == []


def test_evidence_review_extracted_fields_flatten_common_groups() -> None:
    result = EvidenceReviewResult.model_validate(
        {
            "mode": "review",
            "extracted_fields": {
                "case_identity": {
                    "invoice_number": {
                        "value": "INV-1",
                        "status": "present",
                        "source_quote": "Invoice INV-1",
                        "source_locator": "invoice.pdf page 1",
                        "confidence": "high",
                    },
                    "supplier": {
                        "value": "Acme",
                        "status": "present",
                        "source_quote": "Acme",
                        "source_locator": "invoice.pdf page 1",
                        "confidence": "high",
                    },
                }
            },
        }
    )

    assert "case_identity" not in result.extracted_fields
    assert result.extracted_fields["invoice_number"].value == "INV-1"
    assert result.extracted_fields["supplier"].source_locator == "invoice.pdf page 1"
