from __future__ import annotations

import hashlib

import pytest

from app.compiler_runtime.kernel import compile_review_artifact
from app.compiler_runtime.models import CheckAssessment, EvidenceIR, ProofNode, ProofPlan
from app.compiler_runtime.runtime import (
    EvidenceCompilerRuntime,
    ExecutorSummary,
    VerificationBatch,
    _artifact,
    _completion_hook,
    attachment_source_admission,
    expand_active_requirements,
    policy_excerpt_for,
    prepare_sources,
    _planning_extraction_summary,
    _planning_source_catalog,
    _review_result,
    _retryable_checks,
)
from app.compiler_runtime.sandbox import EvidenceSandbox
from app.config import Settings
from app.llm import LlmClient
from app.runtime.patch_normalizer import compact_case_patch_for_write
from app.state.schemas import EvidenceReviewResult


def _settings(tmp_path) -> Settings:
    return Settings(
        workspace_root=tmp_path / "cases",
        storage_root=tmp_path / "storage",
        session_db_path=tmp_path / "storage" / "sessions.sqlite",
        memory_db_path=tmp_path / "storage" / "memory.sqlite",
        llm_provider="deepseek",
        llm_model="deepseek-v4-flash",
        llm_api_key="test-only",
        llm_base_url="https://api.deepseek.com",
    )


def _plan() -> ProofPlan:
    return ProofPlan(
        plan_id="plan.vendor",
        objective="Verify the supplied vendor source exists.",
        active_requirement_ids=["vendor_identity"],
        roots={"vendor_identity": "check.vendor"},
        nodes=[
            ProofNode(
                id="check.vendor",
                kind="CHECK",
                statement="A vendor identity is grounded in the supplied source.",
                requirement_refs=["vendor_identity"],
            )
        ],
    )


def _sources():
    return prepare_sources(
        [
            {
                "attachment_id": "att_vendor",
                "name": "vendor.md",
                "content_kind": "vendor_record",
                "content": "Vendor V-100 is ACTIVE.",
                "original_ref": "attachments/vendor.md",
            }
        ]
    )


def test_task_compiler_planning_context_is_independent_of_source_identity() -> None:
    first_catalog = [
        {"source_id": "source.secret-a", "title": "invoice-a.pdf", "kind": "invoice", "characters": 120},
        {"source_id": "source.secret-b", "title": "invoice-b.pdf", "kind": "invoice", "characters": 80},
    ]
    renamed_catalog = [
        {"source_id": "renamed-1", "title": "x.pdf", "kind": "invoice", "characters": 120},
        {"source_id": "renamed-2", "title": "y.pdf", "kind": "invoice", "characters": 80},
    ]
    first_extraction = [
        {
            "attachment_id": "att-secret",
            "name": "invoice-a.pdf",
            "content_kind": "invoice",
            "available_fields": ["total", "currency"],
            "warnings": ["low contrast"],
        }
    ]
    renamed_extraction = [
        {
            "attachment_id": "renamed-att",
            "name": "x.pdf",
            "content_kind": "invoice",
            "available_fields": ["currency", "total"],
            "warnings": ["different warning text"],
        }
    ]

    assert _planning_source_catalog(first_catalog) == _planning_source_catalog(renamed_catalog)
    assert _planning_extraction_summary(first_extraction) == _planning_extraction_summary(renamed_extraction)
    assert _planning_source_catalog(first_catalog) == [
        {"kind": "invoice", "count": 2, "total_characters": 200}
    ]


class _ScriptedRuntime(EvidenceCompilerRuntime):
    def __init__(self, llm: LlmClient, *, resolve_on_retry: bool) -> None:
        super().__init__(llm)
        self.resolve_on_retry = resolve_on_retry
        self.execute_calls = 0
        self.verify_calls = 0

    def compile_task(self, **_kwargs):
        return _plan()

    def execute_plan(self, *, plan, prepared_sources, policy_excerpt, sandbox=None, focus_check_ids=(), hook_feedback=()):
        del policy_excerpt, hook_feedback
        self.execute_calls += 1
        if sandbox is None:
            record = prepared_sources[0].record
            sandbox = EvidenceSandbox(
                sources=[record],
                allowed_check_ids=["check.vendor"],
                evidence_ir=EvidenceIR(
                    source_ids=[record.source_id],
                    source_fingerprints={
                        record.source_id: hashlib.sha256(record.content.encode("utf-8")).hexdigest()
                    },
                ),
            )
            sandbox.read_source(record.source_id)
        if self.execute_calls == 1 or self.resolve_on_retry:
            record = prepared_sources[0].record
            sandbox.bind_claim(
                subject="vendor:V-100",
                predicate="status",
                value="ACTIVE" if self.execute_calls == 1 else "identity_confirmed",
                source_id=record.source_id,
                quote="Vendor V-100 is ACTIVE.",
                locator="line 3",
                confidence="high",
            )
            sandbox.submit_check(
                check_id="check.vendor",
                claim_ids=[item.id for item in sandbox.evidence_ir.claims],
                note="grounded source read",
            )
        return (
            ExecutorSummary(
                completed_check_ids=[] if focus_check_ids else ["check.vendor"],
                unresolved_check_ids=list(focus_check_ids),
                summary="scripted executor",
            ),
            sandbox,
        )

    def verify(self, *, plan, sandbox, policy_excerpt):
        del plan, policy_excerpt
        self.verify_calls += 1
        claim_ids = [item.id for item in sandbox.evidence_ir.claims]
        if self.verify_calls == 1 and self.resolve_on_retry:
            return [
                CheckAssessment(
                    check_id="check.vendor",
                    status="NOT_FOUND",
                    examined_source_ids=list(sandbox.evidence_ir.source_ids),
                    missing_fact="a second grounded identity fact",
                )
            ]
        return [
            CheckAssessment(
                check_id="check.vendor",
                status="SUPPORTED",
                claim_ids=claim_ids,
                source_ids=[sandbox.evidence_ir.source_ids[0]],
                examined_source_ids=list(sandbox.evidence_ir.source_ids),
                reason="the vendor identity is directly grounded",
            )
        ]


class _DecisiveAnyRuntime(EvidenceCompilerRuntime):
    def __init__(self, llm: LlmClient) -> None:
        super().__init__(llm)
        self.execute_calls = 0

    def compile_task(self, **_kwargs):
        return ProofPlan(
            plan_id="plan.decisive-any",
            objective="Stop when one independently sufficient branch is supported.",
            active_requirement_ids=["vendor_identity"],
            roots={"vendor_identity": "root.any"},
            nodes=[
                ProofNode(
                    id="check.primary",
                    kind="CHECK",
                    statement="The primary source establishes the vendor identity.",
                    requirement_refs=["vendor_identity"],
                ),
                ProofNode(
                    id="check.alternative",
                    kind="CHECK",
                    statement="An alternative source establishes the vendor identity.",
                    requirement_refs=["vendor_identity"],
                ),
                ProofNode(id="root.any", kind="ANY", depends_on=["check.primary", "check.alternative"]),
            ],
        )

    def execute_plan(self, *, plan, prepared_sources, policy_excerpt, sandbox=None, **_kwargs):
        del plan, policy_excerpt
        self.execute_calls += 1
        if sandbox is None:
            record = prepared_sources[0].record
            sandbox = EvidenceSandbox(
                sources=[record],
                allowed_check_ids=["check.primary", "check.alternative"],
                evidence_ir=EvidenceIR(
                    source_ids=[record.source_id],
                    source_fingerprints={
                        record.source_id: hashlib.sha256(record.content.encode("utf-8")).hexdigest()
                    },
                ),
            )
            sandbox.read_source(record.source_id)
            bound = sandbox.bind_claim(
                subject="vendor:V-100",
                predicate="identity",
                value="V-100",
                source_id=record.source_id,
                quote="Vendor V-100 is ACTIVE.",
                locator="line 5",
                confidence="high",
            )
            sandbox.submit_check(
                check_id="check.primary",
                claim_ids=[bound["claim"]["id"]],
            )
            sandbox.submit_check(check_id="check.alternative", note="no alternative source")
        return ExecutorSummary(summary="scripted decisive ANY"), sandbox

    def verify(self, *, plan, sandbox, policy_excerpt):
        del plan, policy_excerpt
        claim = sandbox.evidence_ir.claims[0]
        return [
            CheckAssessment(
                check_id="check.primary",
                status="SUPPORTED",
                claim_ids=[claim.id],
                source_ids=[claim.source_id],
                examined_source_ids=list(sandbox.evidence_ir.source_ids),
            ),
            CheckAssessment(
                check_id="check.alternative",
                status="NOT_FOUND",
                examined_source_ids=list(sandbox.evidence_ir.source_ids),
                missing_fact="an alternative source",
            ),
        ]


def test_prepare_sources_is_content_addressed_and_preserves_exact_text() -> None:
    first = _sources()[0]
    second = prepare_sources(
        [
            {
                "attachment_id": "att_vendor",
                "name": "vendor.md",
                "content_kind": "vendor_record",
                "content": "Vendor V-100 is BLOCKED.",
            }
        ]
    )[0]

    assert "Vendor V-100 is ACTIVE." in first.record.content
    assert first.record.source_id != second.record.source_id
    assert first.metadata["attachment_id"] == "att_vendor"


def test_attachment_source_admission_requires_success_scope_and_readable_content() -> None:
    valid = {
        "status": "success",
        "manifest_status": "active",
        "content": "Invoice INV-1",
    }

    assert attachment_source_admission(valid) == (True, "admitted")
    assert attachment_source_admission({**valid, "status": "error"}) == (
        False,
        "attachment_status_not_success",
    )
    assert attachment_source_admission({**valid, "manifest_status": "quarantined"}) == (
        False,
        "manifest_status_quarantined",
    )
    assert attachment_source_admission({**valid, "manifest_status": "excluded"}) == (
        False,
        "manifest_status_excluded",
    )
    assert attachment_source_admission(
        {**valid, "metadata": {"classification": "cross_case_sample"}}
    ) == (False, "classification_cross_case_sample")
    assert attachment_source_admission({**valid, "cross_case": True}) == (
        False,
        "source_explicitly_excluded",
    )
    assert attachment_source_admission({**valid, "content": ""}) == (
        False,
        "source_content_unreadable",
    )


def test_prepare_sources_deduplicates_identical_source_ids() -> None:
    source = {
        "source_id": "source.same",
        "source_content": "Vendor V-100 is ACTIVE.",
        "name": "vendor.md",
    }

    prepared = prepare_sources([source, dict(source)])

    assert len(prepared) == 1
    assert prepared[0].record.source_id == "source.same"


def test_prepare_sources_rejects_conflicting_content_for_one_source_id() -> None:
    with pytest.raises(ValueError, match="identifies conflicting content"):
        prepare_sources(
            [
                {"source_id": "source.same", "source_content": "Vendor V-100 is ACTIVE."},
                {"source_id": "source.same", "source_content": "Vendor V-100 is BLOCKED."},
            ]
        )


def test_policy_activation_and_unconfigured_values_stay_declarative() -> None:
    active = expand_active_requirements(["invoice", "purchase_order", "goods_receipt"])
    policy = policy_excerpt_for(active)

    assert "three_way_amount_match" in active
    assert policy["values"]["amount_tolerance_percent"] == {"configured": True, "value": "2"}
    duplicate_policy = policy_excerpt_for(["no_active_duplicate"])
    assert duplicate_policy["values"]["duplicate_search_window"] == {
        "configured": False,
        "value": None,
    }


def test_policy_expands_requirement_premises_in_stable_order() -> None:
    expected = ["vendor_identity_active", "vendor_identity"]

    assert expand_active_requirements(["vendor_identity_active"]) == expected
    assert expand_active_requirements(["vendor_identity_active"]) == expected
    assert expand_active_requirements(["vendor_identity_active", "vendor_identity_active"]) == expected


def test_prepare_sources_preserves_persisted_source_identity_exactly() -> None:
    source_content = "Vendor V-100 is ACTIVE.\nEffective 2026-08-01."
    persisted = {
        "source_id": "evc_persisted_vendor",
        "source_content": source_content,
        "source_fingerprint": hashlib.sha256(source_content.encode("utf-8")).hexdigest(),
        "already_persisted": True,
        "name": "vendor.md",
        "evidence_type": "vendor_record",
        "content": "this fallback must not replace persisted source_content",
    }

    first = prepare_sources([persisted])[0]
    replay = prepare_sources([dict(persisted)])[0]

    assert first == replay
    assert first.record.source_id == persisted["source_id"]
    assert first.record.content == persisted["source_content"]
    assert first.metadata["source_fingerprint"] == persisted["source_fingerprint"]
    assert first.metadata["already_persisted"] is True


@pytest.mark.parametrize("missing_field", ["source_id", "source_content", "source_fingerprint"])
def test_prepare_sources_rejects_incomplete_persisted_identity(missing_field: str) -> None:
    persisted = {
        "source_id": "evc_persisted_vendor",
        "source_content": "Vendor V-100 is ACTIVE.",
        "source_fingerprint": "sha256:persisted-vendor-v1",
        "already_persisted": True,
    }
    persisted[missing_field] = ""

    with pytest.raises(ValueError, match=missing_field):
        prepare_sources([persisted])


def test_runtime_finishes_supported_case_without_retry(tmp_path) -> None:
    runtime = _ScriptedRuntime(LlmClient(_settings(tmp_path)), resolve_on_retry=False)

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=_sources(),
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert result.proof.decision_for("vendor_identity").status == "SUPPORTED"
    assert result.retry_count == 0
    assert runtime.execute_calls == 1
    assert runtime.verify_calls == 1
    EvidenceReviewResult.model_validate(result.review_result)
    evidence = result.review_result["suggested_patch"]["add_evidence"][0]
    assert evidence["credibility"] == "medium"
    assert evidence["metadata"]["classification"] == "business_evidence"
    assert evidence["review_result"]["should_accept"] is True
    assert result.review_result["source_traceability"] == "unclear"


def test_review_result_preserves_source_quality_and_does_not_accept_unused_source(tmp_path) -> None:
    runtime = _ScriptedRuntime(LlmClient(_settings(tmp_path)), resolve_on_retry=False)
    sources = _sources() + prepare_sources(
        [
            {
                "attachment_id": "att_process",
                "name": "process.log",
                "content": "Workflow event observed.",
                "classification": "process_only",
                "credibility": "low",
            }
        ]
    )

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=sources,
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    by_name = {
        item["metadata"]["source_filename"]: item
        for item in result.review_result["suggested_patch"]["add_evidence"]
    }
    unused = by_name["process.log"]
    assert unused["credibility"] == "low"
    assert unused["metadata"]["classification"] == "process_only"
    assert unused["review_result"]["should_accept"] is False


def test_review_result_accepts_grounded_source_submitted_to_unresolved_check(tmp_path) -> None:
    prepared = _sources()
    record = prepared[0].record
    sandbox = EvidenceSandbox(
        sources=[record],
        allowed_check_ids=["check.vendor"],
        evidence_ir=EvidenceIR(
            source_ids=[record.source_id],
            source_fingerprints={
                record.source_id: prepared[0].metadata["source_fingerprint"]
            },
        ),
    )
    sandbox.read_source(record.source_id)
    bound = sandbox.bind_claim(
        subject="vendor:V-100",
        predicate="status",
        value="ACTIVE",
        source_id=record.source_id,
        quote="Vendor V-100 is ACTIVE.",
        locator="BODY",
        confidence="high",
    )
    sandbox.submit_check(
        check_id="check.vendor",
        claim_ids=[bound["claim"]["id"]],
    )
    plan = _plan()
    policy = policy_excerpt_for(["vendor_identity"])
    artifact = _artifact(
        plan=plan,
        evidence_ir=sandbox.evidence_ir,
        assessments=[
            CheckAssessment(
                check_id="check.vendor",
                status="NOT_FOUND",
                examined_source_ids=[record.source_id],
                missing_fact="a configured verification policy",
            )
        ],
        submitted_claim_refs={"check.vendor": [bound["claim"]["id"]]},
        policy_excerpt=policy,
        model="fixture",
    )

    review = _review_result(
        prepared_sources=prepared,
        sandbox=sandbox,
        artifact=artifact,
        proof=compile_review_artifact(artifact),
    )

    evidence = review["suggested_patch"]["add_evidence"][0]
    assert evidence["review_result"]["should_accept"] is True
    assert evidence["supports"] == []


def test_runtime_does_not_retry_unresolved_leaf_below_decisive_any_root(tmp_path) -> None:
    runtime = _DecisiveAnyRuntime(LlmClient(_settings(tmp_path)))

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=_sources(),
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert result.proof.decision_for("vendor_identity").status == "SUPPORTED"
    assert result.proof.obligations == []
    assert result.retry_count == 0
    assert runtime.execute_calls == 1


def test_verifier_receives_full_sources_and_only_per_check_submitted_claims(
    tmp_path,
    monkeypatch,
) -> None:
    prepared = prepare_sources(
        [
            {"attachment_id": "att_vendor", "content": "Vendor V-100 is ACTIVE."},
            {"attachment_id": "att_note", "content": "Unrelated payment note."},
        ]
    )
    records = [item.record for item in prepared]
    sandbox = EvidenceSandbox(
        sources=records,
        allowed_check_ids=["check.vendor"],
        evidence_ir=EvidenceIR(
            source_ids=[item.source_id for item in records],
            source_fingerprints={
                item.source_id: hashlib.sha256(item.content.encode("utf-8")).hexdigest()
                for item in records
            },
        ),
    )
    for record in records:
        sandbox.read_source(record.source_id)
    vendor_record = next(item for item in records if "Vendor V-100" in item.content)
    note_record = next(item for item in records if "Unrelated payment" in item.content)
    vendor_claim = sandbox.bind_claim(
        subject="vendor:V-100",
        predicate="status",
        value="ACTIVE",
        source_id=vendor_record.source_id,
        quote="Vendor V-100 is ACTIVE.",
        locator="line 5",
        confidence="high",
    )["claim"]
    sandbox.bind_claim(
        subject="note:1",
        predicate="text",
        value="unrelated",
        source_id=note_record.source_id,
        quote="Unrelated payment note.",
        locator="line 5",
        confidence="high",
    )
    sandbox.submit_check(check_id="check.vendor", claim_ids=[vendor_claim["id"]])
    captured: dict = {}

    def fake_phase(**kwargs):
        captured.update(kwargs["payload"])
        return VerificationBatch(
            assessments=[
                CheckAssessment(
                    check_id="check.vendor",
                    status="SUPPORTED",
                    claim_ids=[vendor_claim["id"]],
                    source_ids=[vendor_record.source_id],
                    examined_source_ids=sorted(item.source_id for item in records),
                )
            ]
        )

    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    monkeypatch.setattr(runtime, "_run_phase", fake_phase)

    runtime.verify(plan=_plan(), sandbox=sandbox, policy_excerpt=policy_excerpt_for(["vendor_identity"]))

    assert "claims" not in captured
    assert {item["source_id"]: item["content"] for item in captured["sources"]} == {
        item.source_id: item.content for item in records
    }
    check = captured["checks"][0]
    assert check["submitted_claim_refs"] == [vendor_claim["id"]]
    assert [item["id"] for item in check["candidate_claims"]] == [vendor_claim["id"]]


def test_runtime_retries_only_unresolved_checks_once(tmp_path) -> None:
    runtime = _ScriptedRuntime(LlmClient(_settings(tmp_path)), resolve_on_retry=True)

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=_sources(),
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert result.proof.decision_for("vendor_identity").status == "SUPPORTED"
    assert result.retry_count == 1
    assert runtime.execute_calls == 2
    assert runtime.verify_calls == 2
    assert len(result.artifact.evidence_ir.claims) == 2


def test_completion_hook_stops_only_after_every_check_is_submitted() -> None:
    source = _sources()[0].record
    sandbox = EvidenceSandbox(
        sources=[source],
        allowed_check_ids=["check.one", "check.two"],
        evidence_ir=EvidenceIR(
            source_ids=[source.source_id],
            source_fingerprints={
                source.source_id: hashlib.sha256(source.content.encode("utf-8")).hexdigest()
            },
        ),
    )
    hook = _completion_hook(sandbox, ["check.one", "check.two"])

    sandbox.submit_check(check_id="check.one", note="missing")
    assert hook(None, []).is_final_output is False
    sandbox.submit_check(check_id="check.two", note="missing")
    result = hook(None, [])

    assert result.is_final_output is True
    assert result.final_output.unresolved_check_ids == ["check.one", "check.two"]


def test_retry_completion_hook_requires_a_new_submission_for_the_focus_check() -> None:
    source = _sources()[0].record
    sandbox = EvidenceSandbox(
        sources=[source],
        allowed_check_ids=["check.one", "check.two"],
        evidence_ir=EvidenceIR(
            source_ids=[source.source_id],
            source_fingerprints={
                source.source_id: hashlib.sha256(source.content.encode("utf-8")).hexdigest()
            },
        ),
    )
    sandbox.submit_check(check_id="check.one", note="missing")
    sandbox.submit_check(check_id="check.two", note="complete")
    hook = _completion_hook(
        sandbox,
        ["check.one"],
        prior_submission_counts={"check.one": 1},
    )

    sandbox.read_source(source.source_id)
    assert hook(None, []).is_final_output is False
    sandbox.submit_check(check_id="check.one", note="still missing")

    result = hook(None, [])
    assert result.is_final_output is True
    assert result.final_output.unresolved_check_ids == ["check.one"]


def test_unconfigured_policy_hole_is_not_retried_by_evidence_executor() -> None:
    plan = ProofPlan(
        plan_id="policy-hole",
        objective="Keep policy administration outside the evidence sandbox.",
        active_requirement_ids=["no_active_duplicate"],
        policy_refs=["duplicate_search_window"],
        roots={"no_active_duplicate": "check.window"},
        nodes=[
            ProofNode(
                id="check.window",
                kind="CHECK",
                statement="The duplicate search window is configured.",
                requirement_refs=["no_active_duplicate"],
                policy_refs=["duplicate_search_window"],
            )
        ],
    )

    assert _retryable_checks(
        plan,
        ["check.window"],
        policy_excerpt_for(["no_active_duplicate"]),
    ) == []
    artifact = _artifact(
        plan=plan,
        evidence_ir=EvidenceIR(),
        assessments=[],
        submitted_claim_refs={"check.window": []},
        policy_excerpt=policy_excerpt_for(["no_active_duplicate"]),
        model="fixture",
    )
    assert artifact.unconfigured_policy_refs == ["duplicate_search_window"]


def test_persisted_source_fails_closed_when_content_does_not_match_fingerprint() -> None:
    with pytest.raises(ValueError, match="does not match its source_fingerprint"):
        prepare_sources(
            [
                {
                    "already_persisted": True,
                    "source_id": "source.invoice",
                    "source_content": "truncated source",
                    "source_fingerprint": hashlib.sha256(b"original source").hexdigest(),
                }
            ]
        )


def test_compiler_source_content_is_not_truncated_before_persistence() -> None:
    content = "source text " * 100
    compact = compact_case_patch_for_write(
        {
            "case_updates": {
                "add_evidence": [
                    {
                        "content": content,
                        "metadata": {"compiler_source_sha256": hashlib.sha256(content.encode()).hexdigest()},
                    }
                ]
            }
        }
    )

    assert compact["case_updates"]["add_evidence"][0]["content"] == content
