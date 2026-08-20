from __future__ import annotations

from graphlib import TopologicalSorter
from typing import Iterable, Mapping

from app.compiler_runtime.models import (
    AssessmentStatus,
    CheckAssessment,
    Claim,
    CompilationDiagnostic,
    CompiledProof,
    DecisionProof,
    NodeResult,
    ProofNode,
    ProofObligation,
    ReviewArtifact,
    explicit_final_statuses,
)


def compile_review_artifact(
    artifact: ReviewArtifact,
    *,
    requirement_requiredness: Mapping[str, bool] | None = None,
) -> CompiledProof:
    """Compile model work into proofs using only graph and reference invariants."""
    plan = artifact.plan
    nodes = {node.id: node for node in plan.nodes}
    assessments = {item.check_id: item for item in artifact.assessments}
    claims = {claim.id: claim for claim in artifact.evidence_ir.claims}
    source_ids = set(artifact.evidence_ir.source_ids)
    submitted_claim_refs = {
        check_id: set(claim_ids)
        for check_id, claim_ids in artifact.submitted_claim_refs.items()
    }
    unconfigured_policy_refs = set(artifact.unconfigured_policy_refs)
    diagnostics: list[CompilationDiagnostic] = []

    integrity_errors: list[str] = []
    if artifact.plan_hash != plan.content_hash():
        integrity_errors.append("STALE_PLAN")
        diagnostics.append(
            CompilationDiagnostic(
                code="STALE_PLAN",
                message="ReviewArtifact plan hash does not match its ProofPlan",
            )
        )
    if artifact.evidence_snapshot_hash != artifact.evidence_ir.content_hash():
        integrity_errors.append("STALE_EVIDENCE_SNAPSHOT")
        diagnostics.append(
            CompilationDiagnostic(
                code="STALE_EVIDENCE_SNAPSHOT",
                message="ReviewArtifact evidence snapshot hash does not match its EvidenceIR",
            )
        )

    check_ids = {node.id for node in plan.nodes if node.kind == "CHECK"}
    for check_id in sorted(set(assessments) - set(nodes)):
        diagnostics.append(
            CompilationDiagnostic(
                code="UNKNOWN_CHECK_ASSESSMENT",
                node_id=check_id,
                message="Verifier returned an assessment for a node outside this ProofPlan",
                blocking=False,
            )
        )
    for check_id in sorted(set(assessments) & (set(nodes) - check_ids)):
        diagnostics.append(
            CompilationDiagnostic(
                code="NON_CHECK_ASSESSMENT",
                node_id=check_id,
                message="Verifier assessments are accepted only for CHECK nodes",
                blocking=False,
            )
        )

    results: dict[str, NodeResult] = {}
    missing_facts: dict[str, str] = {}
    order = tuple(TopologicalSorter({node.id: set(node.depends_on) for node in plan.nodes}).static_order())
    for node_id in order:
        node = nodes[node_id]
        if node.kind == "CHECK":
            result, diagnostic, missing_fact = _compile_check(
                node,
                assessments.get(node_id),
                claims=claims,
                source_ids=source_ids,
                check_submitted=node_id in submitted_claim_refs,
                submitted_claim_ids=submitted_claim_refs.get(node_id, set()),
                unconfigured_policy_refs=unconfigured_policy_refs,
                integrity_errors=integrity_errors,
            )
            results[node_id] = result
            if diagnostic is not None:
                diagnostics.append(diagnostic)
            if result.status == "NOT_FOUND":
                missing_facts[node_id] = missing_fact or node.statement
            continue

        dependency_results = [results[item] for item in node.depends_on]
        status = _aggregate(node.kind, [item.status for item in dependency_results])
        results[node_id] = NodeResult(
            node_id=node.id,
            kind=node.kind,
            status=status,
            reason=_aggregate_reason(node, dependency_results, status),
            claim_ids=_unique(item for result in dependency_results for item in result.claim_ids),
            source_ids=_unique(item for result in dependency_results for item in result.source_ids),
        )

    obligations: list[ProofObligation] = []
    decisions: list[DecisionProof] = []
    for requirement_id in plan.active_requirement_ids:
        required = (
            True
            if requirement_requiredness is None
            else bool(requirement_requiredness.get(requirement_id, True))
        )
        root_id = plan.roots[requirement_id]
        root_status = results[root_id].status
        reachable_checks = sorted(
            node_id
            for node_id in _reachable_from(root_id, nodes)
            if nodes[node_id].kind == "CHECK"
        )
        supporting = [node_id for node_id in reachable_checks if results[node_id].status == "SUPPORTED"]
        contradicting = [node_id for node_id in reachable_checks if results[node_id].status == "CONTRADICTED"]
        unresolved = [node_id for node_id in reachable_checks if results[node_id].status == "NOT_FOUND"]
        obligation_ids: list[str] = []
        if root_status == "NOT_FOUND":
            for check_id in unresolved:
                obligation_id = f"obligation:{requirement_id}:{check_id}"
                obligation_ids.append(obligation_id)
                obligations.append(
                    ProofObligation(
                        id=obligation_id,
                        requirement_id=requirement_id,
                        check_id=check_id,
                        missing_fact=missing_facts.get(check_id, nodes[check_id].statement),
                        blocking=required,
                    )
                )
        decisions.append(
            DecisionProof(
                requirement_id=requirement_id,
                root_node_id=root_id,
                status=root_status,
                supporting_check_ids=supporting,
                contradicting_check_ids=contradicting,
                unresolved_check_ids=unresolved,
                obligation_ids=obligation_ids,
                plan_hash=artifact.plan_hash,
                evidence_snapshot_hash=artifact.evidence_snapshot_hash,
                policy_hash=artifact.policy_hash,
                stop_reason=_stop_reason(root_status, obligation_ids, blocking=required),
            )
        )

    blocking_nodes = {item.check_id for item in obligations if item.blocking}
    diagnostics = [
        item.model_copy(update={"blocking": item.blocking and (not item.node_id or item.node_id in blocking_nodes)})
        for item in diagnostics
    ]
    return CompiledProof(
        node_results=[results[node.id] for node in plan.nodes],
        decisions=decisions,
        obligations=obligations,
        diagnostics=diagnostics,
    )


def _compile_check(
    node: ProofNode,
    assessment: CheckAssessment | None,
    *,
    claims: dict[str, Claim],
    source_ids: set[str],
    check_submitted: bool,
    submitted_claim_ids: set[str],
    unconfigured_policy_refs: set[str],
    integrity_errors: list[str],
) -> tuple[NodeResult, CompilationDiagnostic | None, str]:
    if integrity_errors:
        return (
            NodeResult(
                node_id=node.id,
                kind=node.kind,
                status="NOT_FOUND",
                reason="artifact integrity check failed",
            ),
            None,
            "refresh the stale plan or evidence snapshot",
        )
    blocked_policy_refs = sorted(set(node.policy_refs) & unconfigured_policy_refs)
    if blocked_policy_refs:
        partial_claim_ids = [claim_id for claim_id in claims if claim_id in submitted_claim_ids]
        return _rejected_check(
            node,
            code="POLICY_NOT_CONFIGURED",
            message=f"Required policy values are not configured: {blocked_policy_refs}",
            missing_fact=f"configure policy values: {', '.join(blocked_policy_refs)}",
            claim_ids=partial_claim_ids,
            source_ids=_unique(claims[claim_id].source_id for claim_id in partial_claim_ids),
        )
    if not check_submitted:
        return _rejected_check(
            node,
            code="MISSING_CHECK_SUBMISSION",
            message="Executor did not submit this CHECK before verification",
            missing_fact="complete and submit this CHECK from the evidence sandbox",
        )
    if assessment is None:
        return _rejected_check(
            node,
            code="MISSING_ASSESSMENT",
            message="Verifier did not return an assessment for this CHECK",
            missing_fact=node.statement,
        )
    unknown_claims = sorted(set(assessment.claim_ids) - set(claims))
    if unknown_claims:
        return _rejected_check(
            node,
            code="INVALID_CLAIM_REFERENCE",
            message=f"Assessment cites unknown Claims: {unknown_claims}",
            missing_fact="bind the cited facts as admitted Claims",
        )
    unknown_sources = sorted(set(assessment.source_ids) - source_ids)
    if unknown_sources:
        return _rejected_check(
            node,
            code="INVALID_SOURCE_REFERENCE",
            message=f"Assessment cites unknown sources: {unknown_sources}",
            missing_fact="cite a source admitted to the EvidenceIR",
        )
    if assessment.claim_ids and not assessment.source_ids:
        return _rejected_check(
            node,
            code="MISSING_SOURCE_REFERENCE",
            message="An assessment that cites Claims must cite their concrete sources",
            missing_fact="cite the concrete source for the assessment Claims",
        )
    uncited_claim_sources = sorted(
        {
            getattr(claims[claim_id], "source_id")
            for claim_id in assessment.claim_ids
            if getattr(claims[claim_id], "source_id") not in assessment.source_ids
        }
    )
    if uncited_claim_sources:
        return _rejected_check(
            node,
            code="CLAIM_SOURCE_NOT_CITED",
            message=f"Assessment omits sources used by its Claims: {uncited_claim_sources}",
            missing_fact="cite every source used by the assessment Claims",
        )
    unsubmitted_claims = sorted(set(assessment.claim_ids) - submitted_claim_ids)
    if unsubmitted_claims:
        return _rejected_check(
            node,
            code="UNSUBMITTED_CLAIM_REFERENCE",
            message=f"Assessment cites Claims not submitted for this CHECK: {unsubmitted_claims}",
            missing_fact="submit the candidate Claims for this CHECK before verification",
        )
    explicit_statuses = explicit_final_statuses(assessment.reason)
    if explicit_statuses and explicit_statuses != {assessment.status}:
        return _rejected_check(
            node,
            code="ASSESSMENT_STATUS_REASON_CONFLICT",
            message=(
                "Verifier structured status conflicts with its explicit final classification: "
                f"status={assessment.status}, reason_statuses={sorted(explicit_statuses)}"
            ),
            missing_fact="return one internally consistent final classification for this CHECK",
            claim_ids=assessment.claim_ids,
            source_ids=assessment.source_ids,
        )
    if assessment.status == "NOT_FOUND":
        diagnostic = None
        if not assessment.missing_fact:
            diagnostic = CompilationDiagnostic(
                code="MISSING_FACT_UNSPECIFIED",
                node_id=node.id,
                message="NOT_FOUND assessment did not describe the missing fact",
            )
        return (
            NodeResult(
                node_id=node.id,
                kind=node.kind,
                status="NOT_FOUND",
                reason=assessment.reason or "verifier could not establish the check",
                claim_ids=assessment.claim_ids,
                source_ids=assessment.source_ids,
            ),
            diagnostic,
            assessment.missing_fact or node.statement,
        )

    claim_source_ids = {claims[claim_id].source_id for claim_id in assessment.claim_ids}
    extra_sources = sorted(set(assessment.source_ids) - claim_source_ids)
    if extra_sources:
        return _rejected_check(
            node,
            code="UNSUPPORTED_SOURCE_REFERENCE",
            message=f"Strong assessment cites sources not used by its Claims: {extra_sources}",
            missing_fact="cite only the concrete sources used by the strong assessment Claims",
        )

    examined_source_ids = set(assessment.examined_source_ids)
    if examined_source_ids != source_ids:
        missing_sources = sorted(source_ids - examined_source_ids)
        extra_sources = sorted(examined_source_ids - source_ids)
        return _rejected_check(
            node,
            code="SOURCE_COVERAGE_INCOMPLETE",
            message=(
                "Strong assessment did not examine exactly the admitted source snapshot; "
                f"missing={missing_sources}, extra={extra_sources}"
            ),
            missing_fact="examine every source admitted to this EvidenceIR before a strong conclusion",
        )

    low_confidence_claims = sorted(
        claim_id
        for claim_id in assessment.claim_ids
        if claims[claim_id].confidence == "low"
    )
    if low_confidence_claims:
        return _rejected_check(
            node,
            code="LOW_CONFIDENCE_CLAIM",
            message=f"Strong assessment cites low-confidence Claims: {low_confidence_claims}",
            missing_fact="replace low-confidence Claims with sufficiently reliable grounded evidence",
        )

    if not assessment.claim_ids:
        return _rejected_check(
            node,
            code="MISSING_CLAIM_REFERENCE",
            message="A strong assessment must cite at least one admitted Claim",
            missing_fact="cite an admitted Claim that supports the assessment",
        )
    if not assessment.source_ids:
        return _rejected_check(
            node,
            code="MISSING_SOURCE_REFERENCE",
            message="A strong assessment must cite at least one concrete source",
            missing_fact="cite the concrete source for the assessment",
        )
    return (
        NodeResult(
            node_id=node.id,
            kind=node.kind,
            status=assessment.status,
            reason=assessment.reason,
            claim_ids=assessment.claim_ids,
            source_ids=assessment.source_ids,
        ),
        None,
        "",
    )


def _rejected_check(
    node: ProofNode,
    *,
    code: str,
    message: str,
    missing_fact: str,
    claim_ids: Iterable[str] = (),
    source_ids: Iterable[str] = (),
) -> tuple[NodeResult, CompilationDiagnostic, str]:
    return (
        NodeResult(
            node_id=node.id,
            kind=node.kind,
            status="NOT_FOUND",
            reason=message,
            claim_ids=_unique(claim_ids),
            source_ids=_unique(source_ids),
        ),
        CompilationDiagnostic(code=code, node_id=node.id, message=message),
        missing_fact,
    )


def _aggregate(kind: str, statuses: list[AssessmentStatus]) -> AssessmentStatus:
    if kind == "ALL":
        if "CONTRADICTED" in statuses:
            return "CONTRADICTED"
        if all(item == "SUPPORTED" for item in statuses):
            return "SUPPORTED"
        return "NOT_FOUND"
    if kind == "ANY":
        if "SUPPORTED" in statuses:
            return "SUPPORTED"
        if all(item == "CONTRADICTED" for item in statuses):
            return "CONTRADICTED"
        return "NOT_FOUND"
    raise ValueError(f"Unsupported aggregate kind: {kind}")


def _aggregate_reason(node: ProofNode, dependencies: list[NodeResult], status: AssessmentStatus) -> str:
    child_states = ", ".join(f"{item.node_id}={item.status}" for item in dependencies)
    return f"{node.kind} evaluated to {status} from {child_states}"


def _reachable_from(root_id: str, nodes: dict[str, ProofNode]) -> set[str]:
    result: set[str] = set()
    pending = [root_id]
    while pending:
        node_id = pending.pop()
        if node_id in result:
            continue
        result.add(node_id)
        pending.extend(nodes[node_id].depends_on)
    return result


def _unique(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _stop_reason(
    status: AssessmentStatus,
    obligation_ids: list[str],
    *,
    blocking: bool = True,
) -> str:
    if status == "SUPPORTED":
        return "the requirement proof is supported"
    if status == "CONTRADICTED":
        return "the requirement proof is contradicted"
    if obligation_ids:
        return (
            "blocking proof obligations remain unresolved"
            if blocking
            else "optional evidence remains unresolved"
        )
    return "the requirement could not be established"
