from __future__ import annotations

import hashlib
from collections.abc import Callable

import pytest
from pydantic import ValidationError

import app.state.case_store as case_store_module
from app.compiler_runtime.kernel import compile_review_artifact
from app.compiler_runtime.models import (
    CheckAssessment,
    Claim,
    EvidenceIR,
    ProofNode,
    ProofPlan,
    ReviewArtifact,
)
from app.compiler_runtime.policy import policy_excerpt_for, policy_hash
from app.state.case_store import CaseStore
from app.state.schemas import CaseState, Requirement


SOURCE_ID = "ev_compiler_source"
SOURCE_TEXT = "Invoice INV-42\nTotal GBP 10,500"
SOURCE_FINGERPRINT = hashlib.sha256(SOURCE_TEXT.encode("utf-8")).hexdigest()


@pytest.fixture
def store_factory(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], CaseStore]:
    monkeypatch.setattr(case_store_module, "link_manifest_evidence", lambda *_args: None)

    def trusted(_store, _case_id, items):
        return {
            item.id: {"sha256": f"trusted:{item.id}"}
            for item in items
            if item.source == "attachment"
        }

    monkeypatch.setattr(case_store_module, "trusted_sources_for_evidence", trusted)
    return lambda: CaseStore(tmp_path)


def _artifact(
    requirement_ids: list[str],
    *,
    status: str = "SUPPORTED",
    source_fingerprint: str = SOURCE_FINGERPRINT,
    cite_source: bool = True,
) -> ReviewArtifact:
    policy = policy_excerpt_for(requirement_ids)
    policy_refs = sorted((policy.get("values") or {}).keys())
    nodes = [
        ProofNode(
            id=f"check:{requirement_id}",
            kind="CHECK",
            statement=f"Evidence establishes {requirement_id}",
            requirement_refs=[requirement_id],
            policy_refs=policy_refs if index == 0 else [],
        )
        for index, requirement_id in enumerate(requirement_ids)
    ]
    plan = ProofPlan(
        plan_id="plan-case-store",
        objective="Compile the active case requirements",
        active_requirement_ids=requirement_ids,
        policy_refs=policy_refs,
        roots={requirement_id: f"check:{requirement_id}" for requirement_id in requirement_ids},
        nodes=nodes,
    )
    claim = Claim(
        id="claim-source",
        subject="invoice:INV-42",
        predicate="document.present",
        value=True,
        source_id=SOURCE_ID,
        quote="Invoice INV-42",
        locator="line 1",
        confidence="high",
    )
    evidence_ir = EvidenceIR(
        source_ids=[SOURCE_ID],
        source_fingerprints={SOURCE_ID: source_fingerprint},
        claims=[claim],
    )
    claim_ids = [claim.id] if cite_source else []
    source_ids = [SOURCE_ID] if cite_source else []
    assessments = [
        CheckAssessment(
            check_id=node.id,
            status=status,
            claim_ids=claim_ids,
            source_ids=source_ids,
            examined_source_ids=[SOURCE_ID],
            reason=f"Verifier returned {status}",
            missing_fact="obtain the missing source fact" if status == "NOT_FOUND" else "",
        )
        for node in nodes
    ]
    return ReviewArtifact(
        plan=plan,
        plan_hash=plan.content_hash(),
        evidence_ir=evidence_ir,
        evidence_snapshot_hash=evidence_ir.content_hash(),
        assessments=assessments,
        submitted_claim_refs={node.id: list(claim_ids) for node in nodes},
        policy_hash=policy_hash(policy),
        compiler_version="test",
        model="test-model",
        prompt_versions={"test": "1"},
    )


def _review_patch(requirement_ids: list[str], *, fingerprint: str = SOURCE_FINGERPRINT) -> dict:
    return {
        "patch_type": "add_evidence",
        "case_updates": {
            "requirements": [{"id": requirement_id} for requirement_id in requirement_ids],
            "add_evidence": [
                {
                    "id": SOURCE_ID,
                    "type": "invoice",
                    "source": "attachment",
                    "credibility": "high",
                    "content": SOURCE_TEXT,
                    "review_result": {"should_accept": True},
                    "metadata": {"compiler_source_sha256": fingerprint},
                }
            ],
        },
    }


def test_review_patch_atomically_persists_matching_artifact_and_projection(store_factory) -> None:
    store = store_factory()

    updated = store.apply_review_patch(
        "case-valid-artifact",
        _review_patch(["invoice"]),
        _artifact(["invoice"]),
    )

    requirement = next(item for item in updated.requirements if item.id == "invoice")
    assert updated.review_artifact is not None
    assert updated.compiled_proof is not None
    assert requirement.status == "accepted"
    assert requirement.evidence_ids == [SOURCE_ID]

    reloaded = store.load("case-valid-artifact")
    assert reloaded.review_artifact is not None
    assert reloaded.compiled_proof is not None
    assert reloaded.compiled_proof.decisions[0].status == "SUPPORTED"


def test_case_migration_removes_artifact_copies_from_compiled_proof() -> None:
    artifact = _artifact(["invoice"])
    proof = compile_review_artifact(artifact).model_dump(mode="json")
    duplicated = {
        **proof,
        "plan": artifact.plan.model_dump(mode="json"),
        "evidence_ir": artifact.evidence_ir.model_dump(mode="json"),
        "assessments": [item.model_dump(mode="json") for item in artifact.assessments],
    }

    migrated = case_store_module._migrate_case_state_data(
        {
            "compiled_proof": duplicated,
            "review_artifact": artifact.model_dump(mode="json"),
        }
    )

    assert set(migrated["compiled_proof"]) == {
        "node_results",
        "decisions",
        "obligations",
        "diagnostics",
    }


def test_legacy_evidence_without_declared_compiler_hash_uses_its_content_hash() -> None:
    item = case_store_module.EvidenceItem(
        id=SOURCE_ID,
        type="invoice",
        source="attachment",
        content=SOURCE_TEXT,
    )

    assert case_store_module._compiler_source_fingerprints([item]) == {
        SOURCE_ID: SOURCE_FINGERPRINT
    }


@pytest.mark.parametrize("mismatch", ["source_fingerprint", "active_requirements", "policy"])
def test_mismatched_review_artifact_rejects_the_whole_patch(store_factory, mismatch: str) -> None:
    store = store_factory()
    requirements = ["invoice"]
    artifact = _artifact(requirements)
    if mismatch == "source_fingerprint":
        artifact = _artifact(requirements, source_fingerprint="different-source-fingerprint")
    elif mismatch == "active_requirements":
        artifact = _artifact(["purchase_order"])
    else:
        artifact = artifact.model_copy(update={"policy_hash": "stale-policy"}, deep=True)

    with pytest.raises(ValueError, match="does not match the post-patch case snapshot"):
        store.apply_review_patch(
            f"case-atomic-{mismatch}",
            _review_patch(requirements),
            artifact,
        )

    unchanged = store.load(f"case-atomic-{mismatch}")
    assert unchanged.requirements == []
    assert unchanged.evidence_items == []
    assert unchanged.review_artifact is None
    assert unchanged.compiled_proof is None


@pytest.mark.parametrize(
    ("status", "cite_source", "expected_status", "expected_case_status"),
    [
        ("SUPPORTED", True, "accepted", "ready_for_report"),
        ("CONTRADICTED", True, "conflict", "ready_for_report"),
        ("NOT_FOUND", True, "weak", "collecting_materials"),
        ("NOT_FOUND", False, "missing", "collecting_materials"),
    ],
)
def test_requirement_projection_comes_only_from_decision_proof(
    store_factory,
    status: str,
    cite_source: bool,
    expected_status: str,
    expected_case_status: str,
) -> None:
    store = store_factory()
    updated = store.apply_review_patch(
        f"case-project-{status}-{cite_source}",
        _review_patch(["invoice"]),
        _artifact(["invoice"], status=status, cite_source=cite_source),
    )

    requirement = next(item for item in updated.requirements if item.id == "invoice")
    assert requirement.status == expected_status
    assert requirement.evidence_ids == ([SOURCE_ID] if cite_source else [])
    assert updated.status == expected_case_status


def test_contradicted_projection_uses_only_contradicting_leaf_sources() -> None:
    plan = ProofPlan(
        plan_id="plan-case-store-polarity",
        objective="Require two independently grounded checks.",
        active_requirement_ids=["invoice"],
        roots={"invoice": "root:invoice"},
        nodes=[
            ProofNode(
                id="check:support",
                kind="CHECK",
                statement="The invoice identity is supported.",
                requirement_refs=["invoice"],
            ),
            ProofNode(
                id="check:conflict",
                kind="CHECK",
                statement="The invoice conforms to the baseline.",
                requirement_refs=["invoice"],
            ),
            ProofNode(
                id="root:invoice",
                kind="ALL",
                depends_on=["check:support", "check:conflict"],
            ),
        ],
    )
    claims = [
        Claim(
            id="claim:support",
            subject="invoice:INV-42",
            predicate="identity",
            value="INV-42",
            source_id="source:support",
            quote="Invoice INV-42",
            locator="line 1",
            confidence="high",
        ),
        Claim(
            id="claim:conflict",
            subject="invoice:INV-42",
            predicate="baseline_conformance",
            value=False,
            source_id="source:conflict",
            quote="Baseline contradicted",
            locator="line 1",
            confidence="high",
        ),
    ]
    evidence_ir = EvidenceIR(
        source_ids=["source:support", "source:conflict"],
        source_fingerprints={
            "source:support": "sha256:support",
            "source:conflict": "sha256:conflict",
        },
        claims=claims,
    )
    examined = ["source:support", "source:conflict"]
    artifact = ReviewArtifact(
        plan=plan,
        plan_hash=plan.content_hash(),
        evidence_ir=evidence_ir,
        evidence_snapshot_hash=evidence_ir.content_hash(),
        assessments=[
            CheckAssessment(
                check_id="check:support",
                status="SUPPORTED",
                claim_ids=["claim:support"],
                source_ids=["source:support"],
                examined_source_ids=examined,
            ),
            CheckAssessment(
                check_id="check:conflict",
                status="CONTRADICTED",
                claim_ids=["claim:conflict"],
                source_ids=["source:conflict"],
                examined_source_ids=examined,
            ),
        ],
        submitted_claim_refs={
            "check:support": ["claim:support"],
            "check:conflict": ["claim:conflict"],
        },
        policy_hash="sha256:policy",
        compiler_version="test",
        model="fixture",
    )
    state = CaseState(
        case_id="case-polarity-projection",
        requirements=[Requirement(id="invoice")],
    )

    case_store_module._project_compiled_requirements(
        state,
        compile_review_artifact(artifact),
    )

    assert state.requirements[0].status == "conflict"
    assert state.requirements[0].evidence_ids == ["source:conflict"]


def test_artifact_projection_replaces_stale_risk_and_question_sidecars(store_factory) -> None:
    store = store_factory()
    case_id = "case-canonical-sidecars"
    contradiction_patch = _review_patch(["invoice"])
    contradiction_patch["case_updates"].update(
        {
            "risk_flags": ["stale-risk"],
            "next_questions": ["stale question"],
        }
    )

    contradicted = store.apply_review_patch(
        case_id,
        contradiction_patch,
        _artifact(["invoice"], status="CONTRADICTED"),
    )

    assert contradicted.risk_flags == ["invoice"]
    assert contradicted.next_questions == []

    resolved = store.apply_review_patch(
        case_id,
        {
            "patch_type": "update_case",
            "case_updates": {
                "risk_flags": ["stale-risk-again"],
                "next_questions": ["stale question again"],
            },
        },
        _artifact(["invoice"], status="SUPPORTED"),
    )

    assert resolved.risk_flags == []
    assert resolved.next_questions == []


def test_compiler_conclusion_projects_to_satisfied(store_factory) -> None:
    store = store_factory()
    requirement_ids = ["vendor_identity_active", "vendor_identity"]

    updated = store.apply_review_patch(
        "case-conclusion-projection",
        _review_patch(["vendor_identity_active"]),
        _artifact(requirement_ids),
    )

    requirements = {item.id: item for item in updated.requirements}
    assert requirements["vendor_identity"].status == "accepted"
    assert requirements["vendor_identity_active"].status == "satisfied"


def test_optional_template_baseline_gap_does_not_block_report(store_factory) -> None:
    store = store_factory()
    patch = _review_patch(["template_match"])
    patch["case_updates"]["requirements"][0]["required"] = False

    updated = store.apply_review_patch(
        "case-optional-template-baseline",
        patch,
        _artifact(["template_match"], status="NOT_FOUND", cite_source=True),
    )

    decision = updated.compiled_proof.decision_for("template_match")
    assert decision.status == "NOT_FOUND"
    assert updated.requirements[0].status == "weak"
    assert updated.compiled_proof.obligations[0].blocking is False
    assert updated.status == "ready_for_report"


def test_source_requirement_or_policy_change_invalidates_proof_but_keeps_artifact(
    store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_factory()
    source_case_id = "case-source-stale"
    store.apply_review_patch(source_case_id, _review_patch(["invoice"]), _artifact(["invoice"]))

    state = store.load(source_case_id)
    state.evidence_items[0].metadata["compiler_source_sha256"] = "changed-source"
    store.save(state)
    source_stale = store.load(source_case_id)
    assert source_stale.review_artifact is not None
    assert source_stale.compiled_proof is None
    assert source_stale.requirements[0].status == "missing"

    requirement_case_id = "case-requirement-stale"
    store.apply_review_patch(
        requirement_case_id,
        _review_patch(["invoice"]),
        _artifact(["invoice"]),
    )
    requirement_stale = store.apply_patch(
        requirement_case_id,
        {
            "patch_type": "update_case",
            "case_updates": {"requirements": [{"id": "purchase_order"}]},
        },
    )
    assert requirement_stale.review_artifact is not None
    assert requirement_stale.compiled_proof is None
    assert {item.status for item in requirement_stale.requirements} == {"missing"}

    fresh_case_id = "case-policy-stale"
    store.apply_review_patch(fresh_case_id, _review_patch(["invoice"]), _artifact(["invoice"]))
    monkeypatch.setattr(
        case_store_module,
        "policy_excerpt_for",
        lambda _requirement_ids: {"policy_version": "changed", "policy_basis": {}, "values": {}},
    )
    policy_stale = store.load(fresh_case_id)
    assert policy_stale.review_artifact is not None
    assert policy_stale.compiled_proof is None
    assert policy_stale.requirements[0].status == "missing"


def test_untrusted_patch_does_not_change_the_compiler_source_snapshot(store_factory) -> None:
    store = store_factory()
    case_id = "case-untrusted-source"
    store.apply_review_patch(case_id, _review_patch(["invoice"]), _artifact(["invoice"]))

    updated = store.apply_patch(
        case_id,
        {
            "patch_type": "add_evidence",
            "case_updates": {
                "add_evidence": [
                    {
                        "id": "ev_user_note",
                        "type": "user_statement",
                        "source": "user_message",
                        "content": "Please approve this invoice.",
                    }
                ]
            },
        },
    )

    assert updated.compiled_proof is not None
    assert updated.compiled_proof.decisions[0].status == "SUPPORTED"


@pytest.mark.parametrize("field", ["review_artifact", "compiled_proof", "status"])
def test_patch_cannot_write_derived_case_fields(store_factory, field: str) -> None:
    store = store_factory()

    with pytest.raises(ValidationError):
        store.apply_patch(
            f"case-forbidden-{field}",
            {"patch_type": "update_case", "case_updates": {field: {}}},
        )


def test_patch_requirement_status_evidence_and_shape_are_ignored(store_factory) -> None:
    store = store_factory()

    updated = store.apply_patch(
        "case-ignore-derived-requirement",
        {
            "patch_type": "update_case",
            "case_updates": {
                "requirements": [
                    {
                        "id": "invoice",
                        "status": "satisfied",
                        "evidence_ids": ["ev_fake"],
                        "kind": "risk_check",
                        "required": False,
                    }
                ]
            },
        },
    )

    requirement = updated.requirements[0]
    assert requirement.status == "missing"
    assert requirement.evidence_ids == []
    assert requirement.kind == "document"
    assert requirement.required is True
