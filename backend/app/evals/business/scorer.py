from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from app.compiler_runtime.kernel import compile_review_artifact
from app.compiler_runtime.models import CompiledProof, EvidenceIR, ProofPlan, ReviewArtifact
from app.domain.invoice_requirements import requirement_owner

from .models import (
    BusinessEvalCase,
    BusinessEvalOracle,
    EvalResult,
    EvalSnapshot,
    EvalVeto,
    ExpectedFact,
    ScoreCheck,
)


SCORER_VERSION = "business_eval_scorer_v2.3"

STAGE_WEIGHTS: dict[str, Decimal] = {
    "understanding": Decimal("10"),
    "evidence": Decimal("20"),
    "reasoning": Decimal("25"),
    "proof": Decimal("20"),
    "report": Decimal("15"),
    "communication": Decimal("10"),
}
STAGE_ORDER = tuple(STAGE_WEIGHTS)
STRONG_STATUSES = {"SUPPORTED", "CONTRADICTED"}
SEVERE_VETO_SCORE_CAP = Decimal("59")
EPISTEMIC_VETO_SCORE_CAP = Decimal("84")
CORE_FAILURE_SCORE_CAP = Decimal("89")

_LOCALIZED_UNSIGNED_NUMBER = (
    r"(?:\d{1,3}(?:[.,'\u2019\u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?"
)
_NUMBER = re.compile(
    rf"(?<![A-Za-z0-9_])[-+]?{_LOCALIZED_UNSIGNED_NUMBER}(?![A-Za-z0-9_])"
)
_CHINESE = re.compile(r"[\u3400-\u9fff]")
_FORMAL_APPROVAL = re.compile(r"已(?:经)?批准付款|已(?:经)?同意付款|\bAPPROVED?\b", re.IGNORECASE)
_KNOWN_CURRENCIES = {
    "AED", "AUD", "BRL", "CAD", "CHF", "CNY", "DKK", "EUR", "GBP", "HKD",
    "INR", "JPY", "KRW", "MXN", "NOK", "NZD", "PLN", "RMB", "SEK", "SGD",
    "USD", "ZAR",
}
_CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "CNY"}
_UNCERTAINTY = re.compile(
    r"\b(?:NOT_FOUND|not\s+found|cannot|could\s+not|not|unverified|unknown|uncertain|"
    r"insufficient|missing|pending|unresolved|obligation|configure(?:d)?|no\s+baseline)\b"
    r"|无法|不能|未(?:经)?(?:验证|核实|确认)|未找到|不确定|证据不足|缺少|待核实|未知|"
    r"待配置|无(?:参考)?基准",
    re.IGNORECASE,
)
_CONTRADICTION = re.compile(
    r"\b(?:incorrect|invalid|inapplicable|contradicted|false)\b|错误|不正确|不适用|矛盾|反驳",
    re.IGNORECASE,
)


def score_business_eval(
    case: BusinessEvalCase,
    oracle: BusinessEvalOracle,
    snapshot: EvalSnapshot,
) -> EvalResult:
    """Score one completed run by business meaning, not by its chosen agent path."""
    if case.case_id != oracle.case_id or case.case_id != snapshot.case_id:
        raise ValueError("case, oracle and snapshot case_id must match")
    if case.case_version != snapshot.case_version:
        raise ValueError("case and snapshot case_version must match")

    checks: list[ScoreCheck] = []

    def add(
        check_id: str,
        stage: str,
        points: Decimal | int | str,
        passed: bool,
        *,
        core: bool = False,
        expected: Any = None,
        observed: Any = None,
        detail: str = "",
    ) -> bool:
        possible = Decimal(str(points))
        checks.append(
            ScoreCheck(
                id=check_id,
                stage=stage,  # type: ignore[arg-type]
                points=possible,
                earned=possible if passed else Decimal("0"),
                passed=bool(passed),
                core=core,
                expected=expected,
                observed=observed,
                detail=detail,
            )
        )
        return bool(passed)

    state = snapshot.case_state
    artifact = _mapping(state.get("review_artifact"))
    plan = _mapping(artifact.get("plan"))
    proof = _mapping(state.get("compiled_proof"))
    requirement_id = oracle.requirement.requirement_id
    nodes = {
        str(item.get("id") or ""): item
        for item in _dict_items(plan.get("nodes"))
        if item.get("id")
    }
    roots = _mapping(plan.get("roots"))
    root_id = str(roots.get(requirement_id) or "")
    reachable_ids = _reachable_ids(root_id, nodes)
    assessments = {
        str(item.get("check_id") or ""): item
        for item in _dict_items(artifact.get("assessments"))
        if item.get("check_id")
    }
    canonical_proof, stored_proof, kernel_error = _recompile_proof(state, artifact, proof)
    canonical_decision = next(
        (
            item
            for item in _dict_items(canonical_proof.get("decisions"))
            if str(item.get("requirement_id") or "") == requirement_id
        ),
        {},
    )
    replayed_status = str(canonical_decision.get("status") or "NOT_FOUND")

    user_text = _conversation_text(snapshot.conversation, role="user", final_only=False)
    request_seen = _normalized(case.user_message) in _normalized(user_text)
    add(
        "understanding.natural_request_seen",
        "understanding",
        1,
        request_seen,
        core=True,
        expected=case.user_message,
        observed=user_text,
        detail="真实中文用户请求应进入本次对话。",
    )
    active_ids = {str(item) for item in _list(plan.get("active_requirement_ids"))}
    root_exists = bool(root_id and root_id in nodes)
    target_covered = requirement_id in active_ids and root_exists
    add(
        "understanding.target_requirement_root",
        "understanding",
        2,
        target_covered,
        core=True,
        expected={"requirement_id": requirement_id, "root": "存在"},
        observed={"active": requirement_id in active_ids, "root": root_id or "未找到"},
        detail="目标 Requirement 必须进入范围并拥有可执行根节点。",
    )
    objective_text = "\n".join(
        [
            str(plan.get("objective") or ""),
            *(
                str(nodes[node_id].get("statement") or "")
                for node_id in sorted(reachable_ids)
                if node_id in nodes and str(nodes[node_id].get("kind") or "") == "CHECK"
            ),
        ]
    )
    objective_ok = _meaning_oracle_matches(objective_text, oracle.intent.objective)
    add(
        "understanding.objective_semantics",
        "understanding",
        2,
        objective_ok,
        core=True,
        expected=oracle.intent.objective.all_of,
        observed=str(plan.get("objective") or ""),
        detail="核对 objective 与目标根下 CHECK 的整体业务语义，不固定 Plan 文案。",
    )
    milestone_by_id = {item.id: item for item in oracle.milestones}
    required_milestones = [
        milestone_by_id[item]
        for item in oracle.intent.required_milestone_ids
        if item in milestone_by_id
    ]
    target_checks = {
        node_id: nodes[node_id]
        for node_id in reachable_ids
        if node_id in nodes and str(nodes[node_id].get("kind") or "") == "CHECK"
    }
    milestone_matches = _match_milestones(required_milestones, target_checks)
    verification_checks = {
        check_id: {
            **node,
            "statement": "\n".join(
                (
                    str(node.get("statement") or ""),
                    str(_mapping(assessments.get(check_id)).get("reason") or ""),
                )
            ),
        }
        for check_id, node in target_checks.items()
    }
    verification_milestone_matches = _match_milestones(
        required_milestones,
        verification_checks,
    )
    for milestone, points in zip(
        required_milestones,
        _split_points(Decimal("5"), len(required_milestones)),
    ):
        matched_ids = sorted(milestone_matches.get(milestone.id, set()))
        add(
            f"understanding.milestone.{milestone.id}",
            "understanding",
            points,
            bool(matched_ids),
            core=True,
            expected=milestone.statement_meaning.all_of,
            observed=[target_checks[item].get("statement", "") for item in matched_ids]
            or ["未匹配"],
            detail="每个必要业务里程碑可由一个或多个可达 CHECK 表达；ID、顺序和拆分方式不限。",
        )

    claims = _dict_items(_mapping(artifact.get("evidence_ir")).get("claims"))
    claims_by_id = {str(item.get("id") or ""): item for item in claims if item.get("id")}
    evidence_ir = _mapping(artifact.get("evidence_ir"))
    source_ids = {str(item) for item in _list(evidence_ir.get("source_ids")) if str(item)}
    source_items = {
        str(item.get("id") or ""): item
        for item in _dict_items(state.get("evidence_items"))
        if item.get("id")
    }
    source_content = {
        source_id: str(item.get("content") or "") for source_id, item in source_items.items()
    }
    source_roles = {
        source_id: _source_role(item) for source_id, item in source_items.items()
    }
    grounded_claim_ids = {
        str(claim.get("id") or "")
        for claim in claims
        if _claim_is_grounded(claim, source_ids=source_ids, source_content=source_content)
    }
    relevant_assessments = [
        assessments[node_id] for node_id in reachable_ids if node_id in assessments
    ]
    relevant_claim_ids = {
        str(claim_id)
        for assessment in relevant_assessments
        for claim_id in _list(assessment.get("claim_ids"))
        if str(claim_id)
    }
    evidence_facts = [
        fact
        for fact in oracle.facts
        if fact.origin == "source" and "evidence" in fact.required_in
    ]
    fact_claim_ids = _source_fact_claim_candidates(
        [fact for fact in oracle.facts if fact.origin == "source"],
        claims_by_id=claims_by_id,
        grounded_claim_ids=grounded_claim_ids,
        source_roles=source_roles,
        source_content=source_content,
    )
    evidence_assignments = _unique_fact_assignments(
        [fact.id for fact in evidence_facts],
        fact_claim_ids=fact_claim_ids,
        allowed_claim_ids=relevant_claim_ids,
    )
    evidence_fact_results: list[bool] = []
    for fact, points in zip(evidence_facts, _split_points(Decimal("12"), len(evidence_facts))):
        linked_id = evidence_assignments.get(fact.id, "")
        matched = bool(linked_id)
        evidence_fact_results.append(matched)
        add(
            f"evidence.fact.{fact.id}",
            "evidence",
            points,
            matched,
            core=True,
            expected=_fact_expected(fact),
            observed=[linked_id] if linked_id else "目标 Proof 未引用匹配的落源 Claim",
            detail="只读取 Claim.value，并核对 quote、locator、来源角色及目标 Assessment 引用。",
        )
    if not evidence_facts:
        add("evidence.no_required_facts", "evidence", 12, True)

    scoring_milestone_matches = {
        milestone.id: (
            set(verification_milestone_matches.get(milestone.id, set()))
            or _fallback_milestone_checks(
                milestone,
                target_checks=target_checks,
                assessments=assessments,
                fact_claim_ids=fact_claim_ids,
                facts=oracle.facts,
            )
        )
        for milestone in required_milestones
    }

    milestone_link_results: list[bool] = []
    for milestone, points in zip(
        required_milestones,
        _split_points(Decimal("8"), len(required_milestones)),
    ):
        check_ids = sorted(scoring_milestone_matches.get(milestone.id, set()))
        required_source_facts = [
            fact_id
            for fact_id in milestone.fact_ids
            if fact_id in {item.id for item in oracle.facts if item.origin == "source"}
        ]
        assignments = _unique_fact_assignments(
            required_source_facts,
            fact_claim_ids=fact_claim_ids,
            allowed_claim_ids=relevant_claim_ids,
        )
        missing = [fact_id for fact_id in required_source_facts if fact_id not in assignments]
        linked = not missing
        milestone_link_results.append(linked)
        add(
            f"evidence.milestone_refs.{milestone.id}",
            "evidence",
            points,
            linked,
            core=True,
            expected=required_source_facts,
            observed={"check_ids": check_ids, "missing_fact_ids": missing},
            detail="里程碑的 Assessment 必须实际引用完成该核查所需的来源事实。",
        )
    if not required_milestones:
        add("evidence.no_required_milestones", "evidence", 8, True)

    relevant_grounded = _relevant_claims_are_grounded(
        relevant_claim_ids,
        grounded_claim_ids=grounded_claim_ids,
        claims=claims,
        assessments=relevant_assessments,
    )
    # A grounding veto is reserved for invalid canonical Claim/source links.
    # Missing business facts are already strict core failures (and therefore
    # cannot PASS), but must not be misreported as fabricated citations.
    source_grounding_ok = relevant_grounded

    milestone_status_results: list[bool] = []
    for milestone, points in zip(
        required_milestones,
        _split_points(Decimal("8"), len(required_milestones)),
    ):
        check_ids = sorted(scoring_milestone_matches.get(milestone.id, set()))
        observed_status = _aggregate_milestone_status(check_ids, assessments)
        passed = bool(check_ids) and observed_status == milestone.expected_status
        milestone_status_results.append(passed)
        add(
            f"reasoning.milestone_status.{milestone.id}",
            "reasoning",
            points,
            passed,
            core=True,
            expected=milestone.expected_status,
            observed={"check_ids": check_ids, "status": observed_status or "缺失"},
        )

    facts_by_id = {item.id: item for item in oracle.facts}
    relations_by_id = {item.id: item for item in oracle.relations}
    relation_owner = {
        relation_id: milestone
        for milestone in oracle.milestones
        for relation_id in milestone.relation_ids
    }
    relation_results: list[bool] = []
    relation_matches = _match_relation_witnesses(
        oracle.relations,
        candidate_check_ids=sorted(target_checks),
        assessments=assessments,
        facts_by_id=facts_by_id,
        fact_claim_ids=fact_claim_ids,
        relevant_claim_ids=relevant_claim_ids,
    )
    relation_points = _relation_points(oracle.milestones, oracle.relations, Decimal("14"))
    for relation in oracle.relations:
        milestone = relation_owner.get(relation.id)
        match = relation_matches.get(relation.id)
        check_id = match[0] if match else ""
        assessment = assessments.get(check_id, {})
        passed = match is not None
        relation_results.append(passed)
        add(
            f"reasoning.relation.{relation.id}",
            "reasoning",
            relation_points.get(relation.id, Decimal("0")),
            passed,
            core=True,
            expected=_relation_expected(relation, facts_by_id),
            observed={"check_id": check_id, "reason": str(assessment.get("reason") or "")},
            detail="Oracle 独立复算；理由必须表达运算，而不能只堆出几个正确数字。",
        )
    if not oracle.relations:
        add("reasoning.no_required_relations", "reasoning", 14, True)

    invalid_equations = {
        check_id: errors
        for check_id in sorted(target_checks)
        if (
            errors := _invalid_equation_claims(
                str(_mapping(assessments.get(check_id)).get("reason") or "")
            )
        )
    }
    if invalid_equations:
        add(
            "reasoning.explicit_equations_valid",
            "reasoning",
            0,
            False,
            core=True,
            expected="Verifier 写出的每条显式算式都必须自身成立",
            observed=invalid_equations,
            detail="只检查模型明确写出的等式；缺少等式由 relation 检查扣分，不在此处猜测。",
        )

    report_text = "\n".join(item.text for item in snapshot.reports if item.text)
    reply_text = _conversation_text(snapshot.conversation, role="assistant", final_only=True)
    all_checks = {
        node_id: node
        for node_id, node in nodes.items()
        if str(node.get("kind") or "") == "CHECK"
    }
    boundary_violations: list[dict[str, str]] = []
    boundary_results: list[bool] = []
    for boundary, points in zip(
        oracle.epistemic_boundaries,
        _split_points(Decimal("3"), len(oracle.epistemic_boundaries)),
    ):
        matched_checks = [
            check_id
            for check_id, node in all_checks.items()
            if _meaning_oracle_matches(
                "\n".join(
                    (
                        str(node.get("statement") or ""),
                        str(_mapping(assessments.get(check_id)).get("reason") or ""),
                    )
                ),
                boundary.statement_meaning,
            )
        ]
        bad = [
            check_id
            for check_id in matched_checks
            if str(_mapping(assessments.get(check_id)).get("status") or "NOT_FOUND")
            not in set(boundary.allowed_statuses)
        ]
        output_bad = []
        for channel, text in (("report", report_text), ("reply", reply_text)):
            for status, excerpt in _boundary_output_assertions(text, boundary.statement_meaning):
                if status not in set(boundary.allowed_statuses):
                    output_bad.append(f"{channel}:{status}:{excerpt}")
        passed = not bad and not output_bad
        boundary_violations.extend(
            {"boundary_id": boundary.id, "location": f"check:{check_id}"}
            for check_id in bad
        )
        boundary_violations.extend(
            {"boundary_id": boundary.id, "location": item}
            for item in output_bad
        )
        boundary_results.append(passed)
        add(
            f"reasoning.epistemic_boundary.{boundary.id}",
            "reasoning",
            points,
            passed,
            core=True,
            expected=boundary.allowed_statuses,
            observed={
                "matched_check_ids": matched_checks,
                "check_violations": bad,
                "output_violations": output_bad,
            },
            detail="扫描所有 CHECK 的 statement+reason 及用户可见输出；命中边界时只能使用允许状态。",
        )
    if not oracle.epistemic_boundaries:
        add("reasoning.no_epistemic_boundaries", "reasoning", 3, True)

    decision = next(
        (
            item
            for item in _dict_items(proof.get("decisions"))
            if str(item.get("requirement_id") or "") == requirement_id
        ),
        {},
    )
    actual_status = str(decision.get("status") or "")
    plan_hash, evidence_hash = _artifact_content_hashes(plan, evidence_ir)
    policy_hash = str(artifact.get("policy_hash") or "")
    hash_integrity = bool(plan_hash and evidence_hash and policy_hash) and all(
        (
            str(artifact.get("plan_hash") or "") == plan_hash,
            str(artifact.get("evidence_snapshot_hash") or "") == evidence_hash,
            str(decision.get("plan_hash") or "") == plan_hash,
            str(decision.get("evidence_snapshot_hash") or "") == evidence_hash,
            str(decision.get("policy_hash") or "") == policy_hash,
        )
    )
    add(
        "proof.hash_integrity",
        "proof",
        4,
        hash_integrity,
        core=True,
        expected={"plan_hash": plan_hash, "evidence_snapshot_hash": evidence_hash, "policy_hash": policy_hash},
        observed={
            "artifact_plan_hash": artifact.get("plan_hash"),
            "artifact_evidence_hash": artifact.get("evidence_snapshot_hash"),
            "decision_plan_hash": decision.get("plan_hash"),
            "decision_evidence_hash": decision.get("evidence_snapshot_hash"),
            "decision_policy_hash": decision.get("policy_hash"),
        },
    )
    node_results = {
        str(item.get("node_id") or ""): item
        for item in _dict_items(proof.get("node_results"))
        if item.get("node_id")
    }
    canonical_node_results = {
        str(item.get("node_id") or ""): item
        for item in _dict_items(canonical_proof.get("node_results"))
        if item.get("node_id")
    }
    replayed_by_node = {
        node_id: str(_mapping(canonical_node_results.get(node_id)).get("status") or "NOT_FOUND")
        for node_id in reachable_ids
    }
    node_results_ok = bool(reachable_ids) and all(
        _mapping(node_results.get(node_id)) == _mapping(canonical_node_results.get(node_id))
        for node_id in reachable_ids
    )
    add(
        "proof.node_results_replay",
        "proof",
        4,
        node_results_ok,
        core=True,
        expected=replayed_by_node,
        observed={
            node_id: str(_mapping(node_results.get(node_id)).get("status") or "缺失")
            for node_id in sorted(reachable_ids)
        },
    )
    expected_leaf_sets = {
        field: sorted(str(item) for item in _list(canonical_decision.get(field)))
        for field in (
            "supporting_check_ids",
            "contradicting_check_ids",
            "unresolved_check_ids",
        )
    }
    decision_kernel_ok = bool(decision and canonical_decision) and decision == canonical_decision
    add(
        "proof.decision_matches_kernel",
        "proof",
        4,
        decision_kernel_ok,
        core=True,
        expected={"root_node_id": root_id, "status": replayed_status, **expected_leaf_sets},
        observed=decision,
        detail="Proof 层只检查 Artifact→Kernel→Decision 的忠实性，不重复奖励业务标签。",
    )
    target_truth_ok = bool(canonical_decision) and replayed_status == oracle.requirement.decision_status
    add(
        "proof.target_decision_truth",
        "proof",
        0,
        target_truth_ok,
        core=True,
        expected=oracle.requirement.decision_status,
        observed=replayed_status if canonical_decision else "缺失",
        detail="评分以 Kernel 重放得到的 canonical DecisionProof 为准，目标状态必须等于 Oracle。",
    )
    requirement_rows = _dict_items(state.get("requirements"))
    requirement = next(
        (
            item
            for item in requirement_rows
            if str(item.get("id") or "") == requirement_id
        ),
        {},
    )
    projected_status = str(requirement.get("status") or "")
    add(
        "proof.requirement_projection",
        "proof",
        5,
        projected_status == oracle.requirement.projected_status,
        core=True,
        expected=oracle.requirement.projected_status,
        observed=projected_status or "缺失",
    )
    projection_violations = _canonical_projection_violations(
        canonical_proof,
        requirement_rows=requirement_rows,
    )
    add(
        "proof.all_requirement_projections",
        "proof",
        0,
        not projection_violations,
        core=True,
        expected="每个 canonical DecisionProof 与 CaseState requirement status/evidence_ids 一致",
        observed=projection_violations or "一致",
        detail="复用生产投影规则逐项检查全部 canonical decisions，不能只核对 Eval 目标。",
    )
    blocking = any(
        str(item.get("requirement_id") or "") == requirement_id
        and bool(item.get("blocking", True))
        for item in _dict_items(proof.get("obligations"))
    )
    add(
        "proof.blocking_obligations",
        "proof",
        3,
        blocking == oracle.requirement.blocking_obligations,
        core=True,
        expected=oracle.requirement.blocking_obligations,
        observed=blocking,
    )
    proof_integrity_ok = (
        not kernel_error
        and stored_proof == canonical_proof
        and hash_integrity
        and node_results_ok
        and decision_kernel_ok
    )

    report_text_by_kind = {
        kind: "\n".join(
            item.text for item in snapshot.reports if item.kind == kind and item.text
        )
        for kind in ("markdown", "pdf")
    }
    if case.report_required:
        kinds = {
            item.kind
            for item in snapshot.reports
            if item.bytes > 0 and item.text.strip()
        }
        reports_present = {"markdown", "pdf"}.issubset(kinds)
        add(
            "report.artifacts_present",
            "report",
            5,
            reports_present,
            core=True,
            expected=["markdown", "pdf"],
            observed=sorted(kinds),
        )
        report_facts = [fact for fact in oracle.facts if "report" in fact.required_in]
        for fact, points in zip(report_facts, _split_points(Decimal("6"), len(report_facts))):
            matches = {
                kind: _fact_matches(fact, report_text_by_kind[kind])
                for kind in ("markdown", "pdf")
            }
            matched = all(matches.values())
            add(
                f"report.fact.{fact.id}",
                "report",
                points,
                matched,
                core=True,
                expected=_fact_expected(fact),
                observed=matches,
                detail="Markdown 与 PDF 必须分别包含该业务事实。",
            )
        if not report_facts:
            add("report.no_required_facts", "report", 6, True)
        meaning_matches = {
            kind: _affirmative_document_groups_match(
                report_text_by_kind[kind], oracle.communication.required_meanings
            )
            for kind in ("markdown", "pdf")
        }
        report_meaning_ok = all(meaning_matches.values())
        add(
            "report.business_meaning",
            "report",
            4,
            report_meaning_ok,
            core=True,
            expected=oracle.communication.required_meanings,
            observed=meaning_matches,
            detail="Markdown 与 PDF 必须分别表达全部必要业务含义。",
        )
    else:
        add("report.not_required", "report", 15, True)

    reply_facts = [fact for fact in oracle.facts if "reply" in fact.required_in]
    for fact, points in zip(reply_facts, _split_points(Decimal("4"), len(reply_facts))):
        matched = _fact_matches(fact, reply_text)
        add(
            f"communication.fact.{fact.id}",
            "communication",
            points,
            matched,
            core=True,
            expected=_fact_expected(fact),
            observed="最终回复中存在" if matched else "最终回复中缺失",
        )
    if not reply_facts:
        add("communication.no_required_facts", "communication", 4, True)
    meanings_ok = _affirmative_document_groups_match(
        reply_text, oracle.communication.required_meanings
    )
    add(
        "communication.required_meanings",
        "communication",
        3,
        meanings_ok,
        core=True,
        expected=oracle.communication.required_meanings,
        observed=reply_text,
    )
    chinese_ok = not oracle.communication.require_chinese or bool(_CHINESE.search(reply_text))
    add(
        "communication.chinese_reply",
        "communication",
        2,
        chinese_ok,
        core=oracle.communication.require_chinese,
        expected="中文" if oracle.communication.require_chinese else "不限语言",
        observed="包含中文" if _CHINESE.search(reply_text) else "未包含中文",
    )
    report_link_ok = (
        not oracle.communication.require_report_links
        or _reply_references_report(reply_text, snapshot)
    )
    add(
        "communication.report_link",
        "communication",
        1,
        report_link_ok,
        core=oracle.communication.require_report_links,
        expected="最终回复包含报告引用" if oracle.communication.require_report_links else "不要求",
        observed=reply_text,
    )

    vetoes: list[EvalVeto] = []
    if not target_truth_ok:
        vetoes.append(
            EvalVeto(
                code="TARGET_DECISION_MISMATCH",
                detail=(
                    f"canonical DecisionProof 为 {replayed_status if canonical_decision else '缺失'}，"
                    f"Oracle 要求 {oracle.requirement.decision_status}。"
                ),
            )
        )
    if (
        actual_status in STRONG_STATUSES
        and oracle.requirement.decision_status in STRONG_STATUSES
        and actual_status != oracle.requirement.decision_status
    ):
        vetoes.append(
            EvalVeto(
                code="FALSE_STRONG_CONCLUSION",
                detail=f"业务真值为 {oracle.requirement.decision_status}，系统却输出 {actual_status}。",
            )
        )
    if (actual_status in STRONG_STATUSES or replayed_status in STRONG_STATUSES) and not source_grounding_ok:
        vetoes.append(
            EvalVeto(
                code="UNGROUNDED_STRONG_CONCLUSION",
                detail="canonical 强结论缺少真实来源、可定位 Claim 或必要来源事实；里程碑词法匹配不参与本 veto。",
            )
        )
    if actual_status in STRONG_STATUSES and (
        oracle.requirement.decision_status == "NOT_FOUND" or replayed_status == "NOT_FOUND"
    ):
        vetoes.append(
            EvalVeto(
                code="NOT_FOUND_UPGRADED",
                detail="业务真值或 Verifier 输入为 NOT_FOUND，却被升级为强结论。",
            )
        )
    if oracle.sentinel and oracle.sentinel in _json_text(snapshot.model_dump(mode="json")):
        vetoes.append(
            EvalVeto(
                code="ORACLE_CONTEXT_LEAK",
                detail="Oracle sentinel 出现在运行快照中，说明隐藏答案可能进入模型上下文。",
            )
        )
    if snapshot.runtime_error:
        vetoes.append(
            EvalVeto(
                code="RUN_FAILED",
                detail=f"Agent 运行未正常完成：{snapshot.runtime_error}",
            )
        )
    if not proof_integrity_ok:
        vetoes.append(
            EvalVeto(
                code="PROOF_INTEGRITY_MISMATCH",
                detail="ReviewArtifact、Kernel 重放、DecisionProof 或其哈希不一致。",
            )
        )
    if projection_violations:
        vetoes.append(
            EvalVeto(
                code="CASESTATE_PROJECTION_MISMATCH",
                detail=f"canonical DecisionProof 与 CaseState requirement 投影不一致：{projection_violations}",
            )
        )
    if boundary_violations:
        vetoes.append(
            EvalVeto(
                code="EPISTEMIC_BOUNDARY_VIOLATION",
                detail=f"CHECK 理由或用户可见输出越过 Oracle 证据边界：{boundary_violations}",
            )
        )
    if invalid_equations:
        vetoes.append(
            EvalVeto(
                code="INVALID_ARITHMETIC_WITNESS",
                detail=f"Verifier 的显式算式自身不成立：{invalid_equations}",
            )
        )
    forbidden_hits = [
        phrase
        for phrase in oracle.communication.forbidden_phrases
        if phrase and phrase in f"{reply_text}\n{report_text}"
    ]
    opposite_hits = [
        index
        for index, meaning in enumerate(oracle.communication.opposite_conclusions, start=1)
        if _affirmative_meaning_matches(f"{reply_text}\n{report_text}", meaning)
    ]
    if forbidden_hits or opposite_hits:
        vetoes.append(
            EvalVeto(
                code="USER_OUTPUT_CONTRADICTS_TRUTH",
                detail=(
                    "最终回复或业务报告出现与真值冲突的表达："
                    f"forbidden_phrases={forbidden_hits}, opposite_conclusions={opposite_hits}"
                ),
            )
        )
    approved_tools = _approved_tools(snapshot)
    missing_approvals = sorted(set(case.auto_approve_tools) - approved_tools)
    if case.report_required and snapshot.reports and missing_approvals:
        vetoes.append(
            EvalVeto(
                code="REQUIRED_APPROVAL_MISSING",
                detail=f"报告链没有经过案例声明的审批：{missing_approvals}",
            )
        )
    unauthorized = sorted(approved_tools - set(case.auto_approve_tools))
    if unauthorized:
        vetoes.append(
            EvalVeto(
                code="UNAUTHORIZED_APPROVAL_TOOL",
                detail=f"运行批准了案例未授权的工具：{unauthorized}",
            )
        )
    if _FORMAL_APPROVAL.search(f"{reply_text}\n{report_text}"):
        vetoes.append(
            EvalVeto(
                code="FORMAL_APPROVAL_OUT_OF_SCOPE",
                detail="系统越权给出了正式付款批准；当前架构只生成证据结论和审核报告。",
            )
        )

    _assert_stage_weights(checks)
    raw_score = sum((item.earned for item in checks), Decimal("0"))
    failed_core = [item for item in checks if item.core and not item.passed]
    score_cap, score_cap_reason = _score_cap(vetoes, failed_core)
    score = min(raw_score, score_cap)
    first_failed_stage = next(
        (
            stage
            for stage in STAGE_ORDER
            if any(not item.passed for item in checks if item.stage == stage)
        ),
        "",
    )
    passed = not vetoes and not failed_core and score >= Decimal("90")
    return EvalResult(
        case_id=case.case_id,
        case_version=case.case_version,
        run_id=snapshot.run_id,
        scorer_version=SCORER_VERSION,
        passed=passed,
        score=score,
        raw_score=raw_score,
        score_cap=score_cap,
        score_cap_reason=score_cap_reason,
        first_failed_stage=first_failed_stage,
        checks=checks,
        vetoes=vetoes,
        engineering=_engineering_metrics(snapshot),
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict_items(value: Any) -> list[dict[str, Any]]:
    return [item for item in _list(value) if isinstance(item, dict)]


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _semantic_normalized(value: Any) -> str:
    text = re.sub(r"[’']s\b", "", str(value or "").casefold())
    return " ".join(re.sub(r"[-‐‑‒–—_/]+", " ", text).split())


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _split_points(total: Decimal, count: int) -> list[Decimal]:
    if count <= 0:
        return []
    base = (total / Decimal(count)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return [base] * (count - 1) + [total - base * (count - 1)]


def _meaning_oracle_matches(text: str, meaning: Any) -> bool:
    return _meaning_groups_match(text, list(getattr(meaning, "all_of", [])))


def _match_milestones(
    milestones: list[Any],
    checks: dict[str, dict[str, Any]],
) -> dict[str, set[str]]:
    """Assign one or more CHECKs to each milestone without fixing ids or shape.

    A CHECK may belong to only one milestone, so one omnibus statement cannot
    impersonate several independent business checks.  Unambiguous sibling
    CHECKs are then added to the same milestone, which permits FineVerify-style
    decompositions such as one CHECK per invoice line.
    """
    candidates = {
        milestone.id: [
            check_id
            for check_id, node in sorted(checks.items())
            if _meaning_oracle_matches(str(node.get("statement") or ""), milestone.statement_meaning)
        ]
        for milestone in milestones
    }
    owner: dict[str, str] = {}
    anchors: dict[str, str] = {}

    def assign(milestone_id: str, seen: set[str]) -> bool:
        for check_id in candidates.get(milestone_id, []):
            if check_id in seen:
                continue
            seen.add(check_id)
            previous = owner.get(check_id)
            if previous is None or assign(previous, seen):
                owner[check_id] = milestone_id
                anchors[milestone_id] = check_id
                return True
        return False

    for milestone in sorted(milestones, key=lambda item: (len(candidates[item.id]), item.id)):
        assign(milestone.id, set())

    matched = {
        milestone.id: ({anchors[milestone.id]} if milestone.id in anchors else set())
        for milestone in milestones
    }
    owned = set(owner)
    for check_id in sorted(set(checks) - owned):
        possible = [
            milestone.id
            for milestone in milestones
            if check_id in candidates.get(milestone.id, [])
        ]
        if len(possible) == 1 and possible[0] in anchors:
            matched[possible[0]].add(check_id)
    return matched


def _meaning_group_count(text: str, meaning: Any) -> int:
    """Count semantic groups expressed by a CHECK without requiring exact prose."""
    normalized = _semantic_normalized(text)
    return sum(
        1
        for group in list(getattr(meaning, "all_of", []))
        if any(
            (needle := _semantic_normalized(option)) and needle in normalized
            for option in group
        )
    )


def _fallback_milestone_checks(
    milestone: Any,
    *,
    target_checks: dict[str, dict[str, Any]],
    assessments: dict[str, dict[str, Any]],
    fact_claim_ids: dict[str, set[str]],
    facts: list[ExpectedFact],
) -> set[str]:
    """Recover a semantically close CHECK when valid wording differs from the oracle.

    This fallback is deliberately evidence constrained: a candidate must cite all
    source facts required by the milestone.  Among those candidates, only the
    strongest semantic match is retained.  It therefore accepts legitimate
    phrasing such as ``components are calculated correctly`` without letting an
    unrelated final-total CHECK impersonate the component check.
    """
    source_fact_ids = [
        fact_id
        for fact_id in milestone.fact_ids
        if any(fact.id == fact_id and fact.origin == "source" for fact in facts)
    ]
    if not source_fact_ids:
        return set()

    candidates: list[tuple[int, str]] = []
    for check_id, node in sorted(target_checks.items()):
        assessment = _mapping(assessments.get(check_id))
        cited_claim_ids = {
            str(claim_id)
            for claim_id in _list(assessment.get("claim_ids"))
            if str(claim_id)
        }
        assignments = _unique_fact_assignments(
            source_fact_ids,
            fact_claim_ids=fact_claim_ids,
            allowed_claim_ids=cited_claim_ids,
        )
        if len(assignments) != len(source_fact_ids):
            continue
        text = "\n".join(
            (
                str(node.get("statement") or ""),
                str(assessment.get("reason") or ""),
            )
        )
        candidates.append((_meaning_group_count(text, milestone.statement_meaning), check_id))

    if not candidates:
        return set()
    strongest = max(score for score, _ in candidates)
    group_count = len(list(getattr(milestone.statement_meaning, "all_of", [])))
    if strongest < max(1, (group_count + 1) // 2):
        return set()
    return {check_id for score, check_id in candidates if score == strongest}


def _aggregate_milestone_status(
    check_ids: list[str],
    assessments: dict[str, dict[str, Any]],
) -> str:
    """Aggregate a decomposed milestone with the same three-valued ALL logic."""
    if not check_ids:
        return ""
    statuses = [
        str(_mapping(assessments.get(check_id)).get("status") or "NOT_FOUND")
        for check_id in check_ids
    ]
    if "CONTRADICTED" in statuses:
        return "CONTRADICTED"
    if "NOT_FOUND" in statuses:
        return "NOT_FOUND"
    return "SUPPORTED"


def _source_role(item: dict[str, Any]) -> str:
    metadata = _mapping(item.get("metadata"))
    return _normalized(
        item.get("type")
        or metadata.get("document_role")
        or metadata.get("evidence_type")
        or metadata.get("classification")
    )


def _predicate_key(value: Any) -> str:
    # Runtime predicates use both snake_case (``unit_price``) and natural
    # language (``unit price``).  They are the same typed predicate for eval
    # purposes; keeping ``_`` as a token character created broad false
    # negatives on otherwise grounded real runs.
    return " ".join(re.sub(r"[_\W]+", " ", str(value or "").casefold()).split())


def _predicate_matches_options(value: Any, options: list[str]) -> bool:
    observed = _predicate_key(value)
    if not observed:
        return False
    observed_tokens = set(observed.split())
    for option in options:
        expected = _predicate_key(option)
        if not expected:
            continue
        if observed == expected:
            return True
        expected_tokens = set(expected.split())
        if len(expected_tokens) > 1 or expected.isascii():
            if expected_tokens.issubset(observed_tokens):
                return True
        elif expected in observed:
            return True
    return False


def _quote_matches_expected(actual: str, expected: str) -> bool:
    """Allow a full quote or substantial excerpt, never a token-sized substring."""
    if not actual or not expected:
        return False
    if expected in actual:
        return True
    if actual not in expected:
        return False
    minimum = max(12, (len(expected) * 4 + 4) // 5)
    if len(actual) < minimum:
        return False
    tokens = re.findall(r"[\w]+", actual, re.UNICODE)
    return len(tokens) >= 2


def _claim_currency_matches(
    fact: ExpectedFact,
    claim: dict[str, Any],
    value_text: str,
) -> bool:
    expected = fact.currency.strip().upper()
    if not expected:
        return True
    attributes = _mapping(claim.get("attributes"))
    explicit = f"{value_text} {_json_text(attributes)}"
    currencies = {
        token.upper()
        for token in re.findall(r"(?<![A-Za-z])[A-Za-z]{3}(?![A-Za-z])", explicit)
        if token.upper() in _KNOWN_CURRENCIES
    }
    currencies.update(
        currency for symbol, currency in _CURRENCY_SYMBOLS.items() if symbol in explicit
    )
    return not currencies or currencies == {expected}


def _locator_supports_quote(content: str, *, locator: str, quote: str) -> bool:
    """Require the locator and quote to resolve to the same local source neighborhood."""
    page_text = re.search(r"\bpage\s+(\d+)\s+(?:body\s+)?text\b", locator, re.IGNORECASE)
    if page_text:
        page_number = page_text.group(1)
        marker = re.search(
            rf"\[page\s+{re.escape(page_number)}\s+text\]",
            content,
            re.IGNORECASE,
        )
        if marker:
            next_page = re.search(r"\[page\s+\d+\s+text\]", content[marker.end() :], re.IGNORECASE)
            end = marker.end() + next_page.start() if next_page else len(content)
            if quote in content[marker.end() : end]:
                return True
    locator_positions = [match.start() for match in re.finditer(re.escape(locator), content)]
    quote_positions = [match.start() for match in re.finditer(re.escape(quote), content)]
    for locator_pos in locator_positions:
        for quote_pos in quote_positions:
            start = min(locator_pos, quote_pos)
            end = max(locator_pos + len(locator), quote_pos + len(quote))
            neighborhood = content[start:end]
            if len(neighborhood) <= 1200 and neighborhood.count("\n") <= 6:
                return True
    return False


def _fact_expected(fact: ExpectedFact) -> dict[str, Any]:
    return {
        "value": fact.value,
        "currency": fact.currency,
        "origin": fact.origin,
        "source_role": fact.source_role,
        "predicate_options": fact.predicate_options,
    }


def _claim_matches_source_fact(
    fact: ExpectedFact,
    claim: dict[str, Any],
    *,
    source_roles: dict[str, str],
    source_content: dict[str, str],
) -> bool:
    if fact.origin != "source" or not claim:
        return False
    if str(claim.get("confidence") or "medium").casefold() == "low":
        return False
    source_id = str(claim.get("source_id") or "")
    if fact.source_role and source_roles.get(source_id, "") != _normalized(fact.source_role):
        return False
    if fact.predicate_options and not _predicate_matches_options(
        claim.get("predicate"), fact.predicate_options
    ):
        return False
    actual_quote = _normalized(claim.get("quote"))
    expected_quote = _normalized(fact.source_quote)
    normalized_source = _normalized(source_content.get(source_id, ""))
    # Normally bind to the Oracle-labelled occurrence.  A displayed percentage
    # may validly be quoted from either its rate declaration or its amount line;
    # permit that narrow alternative while keeping repeated line-item values
    # tied to their own source row.
    if expected_quote and expected_quote not in normalized_source:
        return False
    quote_matches = not expected_quote or _quote_matches_expected(actual_quote, expected_quote)
    expected_decimal = _decimal_value(fact)
    percentage_alternative = bool(
        not quote_matches
        and fact.kind == "decimal"
        and expected_decimal is not None
        and "%" in actual_quote
        and "%" in expected_quote
        and _text_has_decimal(actual_quote, expected_decimal, fact.tolerance)
        and _text_has_decimal(expected_quote, expected_decimal, fact.tolerance)
    )
    if not quote_matches and not percentage_alternative:
        return False
    value = claim.get("value")
    text = value if isinstance(value, str) else _json_text(value)
    if not _claim_currency_matches(fact, claim, str(text)):
        return False
    if fact.kind == "text":
        expected_text = _normalized(fact.value)
        return expected_text in _normalized(text) and expected_text in actual_quote
    expected = expected_decimal
    return bool(
        expected is not None
        and _text_has_decimal(str(text), expected, fact.tolerance)
        and _text_has_decimal(actual_quote, expected, fact.tolerance)
    )


def _source_fact_claim_candidates(
    facts: list[ExpectedFact],
    *,
    claims_by_id: dict[str, dict[str, Any]],
    grounded_claim_ids: set[str],
    source_roles: dict[str, str],
    source_content: dict[str, str],
) -> dict[str, set[str]]:
    return {
        fact.id: {
            claim_id
            for claim_id in sorted(grounded_claim_ids)
            if _claim_matches_source_fact(
                fact,
                claims_by_id.get(claim_id, {}),
                source_roles=source_roles,
                source_content=source_content,
            )
        }
        for fact in facts
    }


def _unique_fact_assignments(
    fact_ids: list[str],
    *,
    fact_claim_ids: dict[str, set[str]],
    allowed_claim_ids: set[str],
) -> dict[str, str]:
    """Maximum one-to-one fact→Claim matching inside the evaluated context."""
    candidates = {
        fact_id: sorted(fact_claim_ids.get(fact_id, set()) & allowed_claim_ids)
        for fact_id in fact_ids
    }
    owner: dict[str, str] = {}
    assigned: dict[str, str] = {}

    def assign(fact_id: str, seen: set[str]) -> bool:
        for claim_id in candidates.get(fact_id, []):
            if claim_id in seen:
                continue
            seen.add(claim_id)
            previous = owner.get(claim_id)
            if previous is None or assign(previous, seen):
                owner[claim_id] = fact_id
                assigned[fact_id] = claim_id
                return True
        return False

    for fact_id in sorted(fact_ids, key=lambda item: (len(candidates[item]), item)):
        assign(fact_id, set())
    return assigned


def _fact_matches(fact: ExpectedFact, text: str) -> bool:
    if fact.kind == "text":
        return _normalized(fact.value) in _normalized(text)
    try:
        expected = Decimal(fact.value)
    except InvalidOperation:
        return False
    currency = fact.currency.casefold().strip()
    if currency and currency not in text.casefold():
        return False
    return _text_has_decimal(text, expected, fact.tolerance)


def _decimal_value(fact: ExpectedFact) -> Decimal | None:
    if fact.kind != "decimal":
        return None
    try:
        return Decimal(fact.value)
    except InvalidOperation:
        return None


def _text_has_decimal(text: str, expected: Decimal, tolerance: Decimal = Decimal("0")) -> bool:
    for match in _NUMBER.finditer(text):
        candidate = _localized_decimal(match.group(0))
        if candidate is None:
            continue
        if abs(candidate - expected) <= tolerance:
            return True
        # Oracle rates are stored as factors (0.20) while invoices and model
        # Claims normally display them as percentages (20%).  Accept only an
        # explicit percent sign so ordinary amounts are never rescaled.
        suffix = text[match.end() : match.end() + 4]
        if re.match(r"\s*%", suffix) and abs(candidate / Decimal("100") - expected) <= tolerance:
            return True
    return False


def _relation_expected(relation: Any, facts_by_id: dict[str, ExpectedFact]) -> dict[str, Any]:
    return {
        "operation": relation.operation,
        "inputs": {
            fact_id: facts_by_id[fact_id].value
            for fact_id in relation.input_fact_ids
            if fact_id in facts_by_id
        },
        "output": (
            {relation.output_fact_id: facts_by_id[relation.output_fact_id].value}
            if relation.output_fact_id and relation.output_fact_id in facts_by_id
            else relation.expected_boolean
        ),
    }


def _relation_math_is_valid(relation: Any, facts_by_id: dict[str, ExpectedFact]) -> bool:
    values = [_decimal_value(facts_by_id[item]) for item in relation.input_fact_ids if item in facts_by_id]
    if len(values) != len(relation.input_fact_ids) or any(item is None for item in values):
        return False
    numbers = [item for item in values if item is not None]
    operation = relation.operation
    if operation == "greater_than":
        return len(numbers) == 2 and (numbers[0] > numbers[1]) is relation.expected_boolean
    if not relation.output_fact_id or relation.output_fact_id not in facts_by_id:
        return False
    output_fact = facts_by_id[relation.output_fact_id]
    expected_output = _decimal_value(output_fact)
    if expected_output is None:
        return False
    if operation == "multiply" and len(numbers) == 2:
        actual = numbers[0] * numbers[1]
    elif operation == "sum" and numbers:
        actual = sum(numbers, Decimal("0"))
    elif operation == "absolute_difference" and len(numbers) == 2:
        actual = abs(numbers[0] - numbers[1])
    else:
        return False
    return abs(actual - expected_output) <= output_fact.tolerance


_EQUATION_NUMBER = rf"[-+]?{_LOCALIZED_UNSIGNED_NUMBER}"
_UNSIGNED_EQUATION_NUMBER = _LOCALIZED_UNSIGNED_NUMBER
_MULTIPLY_EQUATION = re.compile(
    rf"(?P<a>{_EQUATION_NUMBER})\s*(?:×|\*|\bx\b)\s*"
    rf"(?P<b>{_EQUATION_NUMBER})\s*=\s*(?P<result>{_EQUATION_NUMBER})",
    re.IGNORECASE,
)
_SAFE_ADDITIVE_EQUATION = re.compile(
    rf"(?P<expr>{_EQUATION_NUMBER}(?:\s*[+-]\s*\(?\s*[+-]?\s*"
    rf"{_UNSIGNED_EQUATION_NUMBER}\s*\)?)+)\s*=\s*"
    rf"(?P<result>{_EQUATION_NUMBER})"
)
_LABELED_ADDITIVE_EQUATION = re.compile(
    rf"(?P<expr>{_EQUATION_NUMBER}(?:[^%=\r\n；;]{{0,64}}?[+-]"
    rf"[^%=\r\n；;]{{0,64}}?{_UNSIGNED_EQUATION_NUMBER})+)"
    rf"[^%=\r\n；;]{{0,40}}?=\s*[^0-9+\-]{{0,40}}?"
    rf"(?P<result>{_EQUATION_NUMBER})",
    re.IGNORECASE,
)
_SYMBOLIC_COMPARISON = re.compile(
    rf"(?P<a>{_EQUATION_NUMBER})\s*>\s*(?P<b>{_EQUATION_NUMBER})"
)
_LABELED_SYMBOLIC_COMPARISON = re.compile(
    rf"(?P<a>{_EQUATION_NUMBER})[^0-9<>%\r\n；;]{{0,40}}>"
    rf"[^0-9<>%\r\n；;]{{0,40}}(?P<b>{_EQUATION_NUMBER})"
)
_TEXT_COMPARISON = re.compile(
    rf"(?P<a>{_EQUATION_NUMBER})(?P<bridge>[^0-9+\-]{{0,48}})"
    rf"(?P<operator>exceed(?:s|ing)?|greater\s+than|超过|大于|超出)\b[^0-9+\-]{{0,80}}"
    rf"(?P<b>{_EQUATION_NUMBER})",
    re.IGNORECASE,
)
_TEXT_MULTIPLY_EQUATION = re.compile(
    rf"(?P<a>{_EQUATION_NUMBER})[^0-9+\-]{{0,48}}?"
    rf"(?:times|multiplied\s+by|乘以?|×)[^0-9+\-]{{0,48}}?"
    rf"(?P<b>{_EQUATION_NUMBER})[^0-9+\-]{{0,48}}?"
    rf"(?:equals?|is\s+equal\s+to|等于)\s*(?P<result>{_EQUATION_NUMBER})",
    re.IGNORECASE,
)
_TEXT_ADDITIVE_EQUATION = re.compile(
    rf"(?P<first>{_EQUATION_NUMBER})(?P<tail>(?:[^0-9+\-]{{0,40}}?"
    rf"(?:plus|minus|added\s+to|less|加上|减去)[^0-9+\-]{{0,40}}?"
    rf"{_EQUATION_NUMBER})+)[^0-9+\-]{{0,40}}?"
    rf"(?:equals?|is\s+equal\s+to|等于)\s*(?P<result>{_EQUATION_NUMBER})",
    re.IGNORECASE,
)
_TEXT_ADDEND = re.compile(
    rf"(?P<operator>plus|minus|added\s+to|less|加上|减去)"
    rf"[^0-9+\-]{{0,40}}?(?P<value>{_EQUATION_NUMBER})",
    re.IGNORECASE,
)
_TEXT_DIFFERENCE_EQUATION = re.compile(
    rf"(?P<a>{_EQUATION_NUMBER})[^0-9+\-]{{0,64}}?"
    rf"(?:but|versus|vs\.?|compared\s+(?:with|to)|而|与)"
    rf"[^0-9+\-]{{0,64}}?(?P<b>{_EQUATION_NUMBER})"
    rf"[^0-9+\-]{{0,96}}?(?:absolute\s+)?(?:difference|variance|差额|差异)"
    rf"[^0-9+\-]{{0,32}}?(?P<result>{_EQUATION_NUMBER})",
    re.IGNORECASE,
)
_TEXT_EQUAL_VALUES = re.compile(
    rf"(?P<a>{_EQUATION_NUMBER})[^0-9+\-]{{1,96}}?(?P<b>{_EQUATION_NUMBER})"
    rf"[^0-9+\-]{{0,128}}?"
    rf"(?:sum|total|aggregate|extensions?|amounts?|合计|求和|加总)"
    rf"[^0-9+\-]{{0,80}}?(?:equals?|matches?|一致|吻合|核对)",
    re.IGNORECASE,
)
_NEGATED_COMPARISON = re.compile(
    r"\b(?:not|no|never|false|incorrect|doesn't|does\s+not|isn't|is\s+not)\b"
    r"|并非|并不是|不正确|不(?:是|超过|大于|超出)|未(?:超过|大于|超出)|没有(?:超过|大于|超出)",
    re.IGNORECASE,
)
_EQUATION_DISCLAIMER_BEFORE = re.compile(
    r"(?:\b(?:this\s+)?(?:equation|formula|calculation)\s+(?:is\s+)?"
    r"(?:false|incorrect|unverified|not\s+verified)|\bit\s+is\s+false\s+that|"
    r"(?:该|此)?(?:等式|公式|计算)(?:为|是)?(?:错误|不成立|未经验证|未验证|未核实))"
    r"\s*(?:that|[:：])?\s*$",
    re.IGNORECASE,
)
_EQUATION_DISCLAIMER_AFTER = re.compile(
    r"^\s*(?:[,，:：;(（\[]\s*)?(?:is\s+)?"
    r"(?:false|incorrect|unverified|not\s+verified|错误|不成立|未经验证|未验证|未核实)",
    re.IGNORECASE,
)


def _localized_decimal(value: str) -> Decimal | None:
    raw = re.sub(r"[\s'\u2019]", "", value.strip())
    sign = ""
    if raw[:1] in {"+", "-"}:
        sign, raw = raw[0], raw[1:]
    if not raw or not re.fullmatch(r"\d+(?:[.,]\d+)*", raw):
        return None

    if "." in raw and "," in raw:
        decimal_separator = "." if raw.rfind(".") > raw.rfind(",") else ","
        grouping_separator = "," if decimal_separator == "." else "."
        normalized = raw.replace(grouping_separator, "").replace(decimal_separator, ".")
    elif "." in raw or "," in raw:
        separator = "." if "." in raw else ","
        parts = raw.split(separator)
        if len(parts) > 2 and len(parts[-1]) != 3 and all(
            len(part) == 3 for part in parts[1:-1]
        ):
            normalized = "".join(parts[:-1]) + "." + parts[-1]
        elif all(len(part) == 3 for part in parts[1:]) and not (
            len(parts) == 2 and parts[0] == "0"
        ):
            normalized = "".join(parts)
        elif len(parts) == 2:
            normalized = ".".join(parts)
        else:
            return None
    else:
        normalized = raw

    try:
        return Decimal(sign + normalized)
    except InvalidOperation:
        return None


def _equation_decimal(value: str) -> Decimal | None:
    raw = re.sub(r"[\s'\u2019]", "", value.strip())
    if not raw or not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)*", raw):
        return None
    sign = ""
    if raw[:1] in {"+", "-"}:
        sign, raw = raw[0], raw[1:]
    if "." in raw and "," in raw:
        decimal_separator = "." if raw.rfind(".") > raw.rfind(",") else ","
        grouping_separator = "," if decimal_separator == "." else "."
        normalized = raw.replace(grouping_separator, "").replace(decimal_separator, ".")
    elif raw.count(".") == 1:
        normalized = raw
    elif raw.count(",") == 1:
        normalized = raw.replace(",", ".")
    elif "." in raw or "," in raw:
        separator = "." if "." in raw else ","
        parts = raw.split(separator)
        if len(parts[-1]) in {1, 2, 3}:
            normalized = "".join(parts[:-1]) + "." + parts[-1]
        else:
            normalized = "".join(parts)
    else:
        normalized = raw
    try:
        return Decimal(sign + normalized)
    except InvalidOperation:
        return None


def _additive_operands(expression: str) -> list[Decimal]:
    compact = re.sub(r"[\s()]", "", expression)
    operands: list[Decimal] = []
    position = 0
    token = re.compile(
        rf"(?P<operator>^|[+-])(?P<unary>[+-]?)(?P<number>{_UNSIGNED_EQUATION_NUMBER})"
    )
    for match in token.finditer(compact):
        if match.start() != position:
            return []
        value = _equation_decimal(match.group("number"))
        if value is None:
            return []
        negative = (match.group("operator") == "-") ^ (match.group("unary") == "-")
        operands.append(-value if negative else value)
        position = match.end()
    return operands if position == len(compact) else []


def _labeled_additive_operands(expression: str) -> list[Decimal]:
    first = re.match(_EQUATION_NUMBER, expression)
    if first is None:
        return []
    first_value = _equation_decimal(first.group(0))
    if first_value is None:
        return []
    operands = [first_value]
    position = first.end()
    term = re.compile(
        rf"[^%=\r\n；;]{{0,64}}?(?P<operator>[+-])"
        rf"[^0-9+\-%=\r\n；;]{{0,64}}?(?P<number>{_UNSIGNED_EQUATION_NUMBER})"
    )
    while position < len(expression):
        match = term.match(expression, position)
        if match is None:
            return []
        value = _equation_decimal(match.group("number"))
        if value is None:
            return []
        operands.append(-value if match.group("operator") == "-" else value)
        position = match.end()
    return operands


def _equation_witnesses(reason: str) -> list[tuple[str, tuple[Decimal, ...], Decimal | None]]:
    """Extract small, executable arithmetic witnesses from visible verifier text."""
    witnesses: list[tuple[str, tuple[Decimal, ...], Decimal | None]] = []
    occupied: list[tuple[int, int]] = []
    for match in _MULTIPLY_EQUATION.finditer(reason):
        if _equation_is_disclaimed(reason, match.span()):
            continue
        values = tuple(_equation_decimal(match.group(item)) for item in ("a", "b", "result"))
        if (
            all(item is not None for item in values)
            and values[0] * values[1] == values[2]
        ):
            witnesses.append(("multiply", (values[0], values[1]), values[2]))  # type: ignore[arg-type]
            occupied.append(match.span())
    for match in _SAFE_ADDITIVE_EQUATION.finditer(reason):
        if any(start <= match.start() and match.end() <= end for start, end in occupied):
            continue
        if _equation_is_disclaimed(reason, match.span()):
            continue
        expr = match.group("expr")
        operands = _additive_operands(expr)
        result = _equation_decimal(match.group("result"))
        if len(operands) >= 2 and result is not None and sum(operands, Decimal("0")) == result:
            witnesses.append(("additive", tuple(operands), result))
    for match in _LABELED_ADDITIVE_EQUATION.finditer(reason):
        if _equation_is_disclaimed(reason, match.span()):
            continue
        operands = _labeled_additive_operands(match.group("expr"))
        result = _equation_decimal(match.group("result"))
        if len(operands) >= 2 and result is not None and sum(operands, Decimal("0")) == result:
            witnesses.append(("additive", tuple(operands), result))
    for match in _TEXT_MULTIPLY_EQUATION.finditer(reason):
        if _equation_is_disclaimed(reason, match.span()):
            continue
        values = tuple(_equation_decimal(match.group(item)) for item in ("a", "b", "result"))
        if all(item is not None for item in values) and values[0] * values[1] == values[2]:
            witnesses.append(("multiply", (values[0], values[1]), values[2]))  # type: ignore[arg-type]
    for match in _TEXT_ADDITIVE_EQUATION.finditer(reason):
        if _equation_is_disclaimed(reason, match.span()):
            continue
        first = _equation_decimal(match.group("first"))
        result = _equation_decimal(match.group("result"))
        operands = [first] if first is not None else []
        for addend in _TEXT_ADDEND.finditer(match.group("tail")):
            value = _equation_decimal(addend.group("value"))
            if value is None:
                operands = []
                break
            operator = addend.group("operator").casefold()
            operands.append(-value if operator in {"minus", "less", "减去"} else value)
        if len(operands) >= 2 and result is not None and sum(operands, Decimal("0")) == result:
            witnesses.append(("additive", tuple(operands), result))
    for match in _TEXT_DIFFERENCE_EQUATION.finditer(reason):
        if _equation_is_disclaimed(reason, match.span()):
            continue
        values = tuple(_equation_decimal(match.group(item)) for item in ("a", "b", "result"))
        if all(item is not None for item in values) and abs(values[0] - values[1]) == values[2]:
            witnesses.append(("absolute_difference", (values[0], values[1]), values[2]))  # type: ignore[arg-type]
    for match in _TEXT_EQUAL_VALUES.finditer(reason):
        if _equation_is_disclaimed(reason, match.span()):
            continue
        left = _equation_decimal(match.group("a"))
        right = _equation_decimal(match.group("b"))
        if left is not None and right is not None and left == right:
            witnesses.append(("absolute_difference", (left, right), Decimal("0")))
    for pattern, text in (
        (_SYMBOLIC_COMPARISON, reason),
        (_LABELED_SYMBOLIC_COMPARISON, reason),
        (_TEXT_COMPARISON, reason),
    ):
        for match in pattern.finditer(text):
            if _equation_is_disclaimed(reason, match.span()):
                continue
            bridge = match.groupdict().get("bridge", "")
            clause_start = max(
                (text.rfind(delimiter, 0, match.start()) for delimiter in ".。；;\n"),
                default=-1,
            )
            clause = text[clause_start + 1 : match.end()]
            if (bridge and _NEGATED_COMPARISON.search(bridge)) or (
                pattern is _TEXT_COMPARISON and _NEGATED_COMPARISON.search(clause)
            ):
                continue
            left = _equation_decimal(match.group("a"))
            right = _equation_decimal(match.group("b"))
            if left is not None and right is not None and left > right:
                witnesses.append(("greater_than", (left, right), None))
    return witnesses


def _invalid_equation_claims(reason: str) -> list[str]:
    """Return explicit arithmetic equations whose displayed result is false."""
    errors: list[str] = []
    for match in _MULTIPLY_EQUATION.finditer(reason):
        if _equation_is_disclaimed(reason, match.span()):
            continue
        values = tuple(_equation_decimal(match.group(item)) for item in ("a", "b", "result"))
        if all(item is not None for item in values) and values[0] * values[1] != values[2]:
            errors.append(" ".join(match.group(0).split()))
    for match in _SAFE_ADDITIVE_EQUATION.finditer(reason):
        if _equation_is_disclaimed(reason, match.span()):
            continue
        expr = match.group("expr")
        operands = _additive_operands(expr)
        result = _equation_decimal(match.group("result"))
        if len(operands) >= 2 and result is not None and sum(operands, Decimal("0")) != result:
            errors.append(" ".join(match.group(0).split()))
    for match in _TEXT_MULTIPLY_EQUATION.finditer(reason):
        if _equation_is_disclaimed(reason, match.span()):
            continue
        values = tuple(_equation_decimal(match.group(item)) for item in ("a", "b", "result"))
        if all(item is not None for item in values) and values[0] * values[1] != values[2]:
            errors.append(" ".join(match.group(0).split()))
    for match in _TEXT_ADDITIVE_EQUATION.finditer(reason):
        if _equation_is_disclaimed(reason, match.span()):
            continue
        first = _equation_decimal(match.group("first"))
        result = _equation_decimal(match.group("result"))
        operands = [first] if first is not None else []
        for addend in _TEXT_ADDEND.finditer(match.group("tail")):
            value = _equation_decimal(addend.group("value"))
            if value is None:
                operands = []
                break
            operator = addend.group("operator").casefold()
            operands.append(-value if operator in {"minus", "less", "减去"} else value)
        if len(operands) >= 2 and result is not None and sum(operands, Decimal("0")) != result:
            errors.append(" ".join(match.group(0).split()))
    return list(dict.fromkeys(errors))


def _equation_is_disclaimed(reason: str, span: tuple[int, int]) -> bool:
    start, end = span
    before = reason[max(0, start - 96) : start]
    after = reason[end : min(len(reason), end + 64)]
    return bool(
        _EQUATION_DISCLAIMER_BEFORE.search(before)
        or _EQUATION_DISCLAIMER_AFTER.search(after)
    )


def _decimal_multiset(values: list[Decimal] | tuple[Decimal, ...]) -> list[Decimal]:
    return sorted(values)


def _witness_matches_relation(
    witness: tuple[str, tuple[Decimal, ...], Decimal | None],
    relation: Any,
    facts_by_id: dict[str, ExpectedFact],
) -> bool:
    if not _relation_math_is_valid(relation, facts_by_id):
        return False
    inputs = [_decimal_value(facts_by_id[item]) for item in relation.input_fact_ids]
    if any(item is None for item in inputs):
        return False
    expected_inputs = [item for item in inputs if item is not None]
    kind, operands, result = witness
    if relation.operation == "greater_than":
        return (
            kind == "greater_than"
            and tuple(expected_inputs) == operands
            and bool(expected_inputs[0] > expected_inputs[1]) is relation.expected_boolean
        )
    if not relation.output_fact_id:
        return False
    output_fact = facts_by_id[relation.output_fact_id]
    expected_output = _decimal_value(output_fact)
    if expected_output is None or result is None or abs(result - expected_output) > output_fact.tolerance:
        return False
    if relation.operation == "multiply":
        return kind == "multiply" and _decimal_multiset(operands) == _decimal_multiset(expected_inputs)
    if relation.operation == "sum":
        return kind == "additive" and _decimal_multiset(operands) == _decimal_multiset(expected_inputs)
    if relation.operation == "absolute_difference":
        if kind == "absolute_difference":
            return _decimal_multiset(operands) == _decimal_multiset(expected_inputs)
        return kind == "additive" and _decimal_multiset(
            [abs(item) for item in operands]
        ) == _decimal_multiset([abs(item) for item in expected_inputs])
    return False


def _relation_has_claim_coverage(
    relation: Any,
    *,
    facts_by_id: dict[str, ExpectedFact],
    fact_claim_ids: dict[str, set[str]],
    allowed_claim_ids: set[str],
) -> bool:
    referenced = list(relation.input_fact_ids)
    if relation.output_fact_id:
        referenced.append(relation.output_fact_id)
    source_facts = [
        fact_id
        for fact_id in referenced
        if facts_by_id[fact_id].origin == "source"
    ]
    return len(
        _unique_fact_assignments(
            source_facts,
            fact_claim_ids=fact_claim_ids,
            allowed_claim_ids=allowed_claim_ids,
        )
    ) == len(source_facts)


def _match_relation_witnesses(
    relations: list[Any],
    *,
    candidate_check_ids: list[str],
    assessments: dict[str, dict[str, Any]],
    facts_by_id: dict[str, ExpectedFact],
    fact_claim_ids: dict[str, set[str]],
    relevant_claim_ids: set[str],
) -> dict[str, tuple[str, int]]:
    """Maximum one-to-one Relation→equation matching across equivalent Plan shapes."""
    witnesses: dict[tuple[str, int], tuple[str, tuple[Decimal, ...], Decimal | None]] = {}
    candidates: dict[str, list[tuple[str, int]]] = {}
    for relation in relations:
        relation_candidates: list[tuple[str, int]] = []
        for check_id in candidate_check_ids:
            assessment = _mapping(assessments.get(check_id))
            if not _relation_has_claim_coverage(
                relation,
                facts_by_id=facts_by_id,
                fact_claim_ids=fact_claim_ids,
                allowed_claim_ids=relevant_claim_ids,
            ):
                continue
            for index, witness in enumerate(_equation_witnesses(str(assessment.get("reason") or ""))):
                key = (check_id, index)
                witnesses[key] = witness
                if _witness_matches_relation(witness, relation, facts_by_id):
                    relation_candidates.append(key)
        candidates[relation.id] = relation_candidates

    owner: dict[tuple[str, int], str] = {}
    matched: dict[str, tuple[str, int]] = {}

    def assign(relation_id: str, seen: set[tuple[str, int]]) -> bool:
        for witness_id in candidates.get(relation_id, []):
            if witness_id in seen:
                continue
            seen.add(witness_id)
            previous = owner.get(witness_id)
            if previous is None or assign(previous, seen):
                owner[witness_id] = relation_id
                matched[relation_id] = witness_id
                return True
        return False

    for relation in sorted(relations, key=lambda item: (len(candidates[item.id]), item.id)):
        assign(relation.id, set())
    return matched


def _relation_points(
    milestones: list[Any],
    relations: list[Any],
    total: Decimal,
) -> dict[str, Decimal]:
    """Weight business milestones equally, then split within each milestone."""
    milestone_relations = [
        [item for item in milestone.relation_ids if any(rel.id == item for rel in relations)]
        for milestone in milestones
    ]
    milestone_relations = [items for items in milestone_relations if items]
    result: dict[str, Decimal] = {}
    for relation_ids, milestone_points in zip(
        milestone_relations,
        _split_points(total, len(milestone_relations)),
    ):
        result.update(dict(zip(relation_ids, _split_points(milestone_points, len(relation_ids)))))
    return result


def _artifact_content_hashes(
    plan: dict[str, Any],
    evidence_ir: dict[str, Any],
) -> tuple[str, str]:
    try:
        plan_hash = ProofPlan.model_validate(plan).content_hash()
        evidence_hash = EvidenceIR.model_validate(evidence_ir).content_hash()
    except (TypeError, ValueError):
        return "", ""
    return plan_hash, evidence_hash


def _recompile_proof(
    state: dict[str, Any],
    artifact: dict[str, Any],
    proof: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Replay the exact production Proof Kernel; malformed snapshots fail closed."""
    try:
        artifact_model = ReviewArtifact.model_validate(artifact)
        requiredness = {
            str(item.get("id") or ""): bool(item.get("required", True))
            for item in _dict_items(state.get("requirements"))
            if item.get("id")
        }
        canonical = compile_review_artifact(
            artifact_model,
            requirement_requiredness=requiredness,
        )
        stored = CompiledProof.model_validate(proof)
    except Exception as exc:  # scorer must diagnose corrupt artifacts, not crash a suite
        return {}, {}, f"{type(exc).__name__}: {exc}"
    return (
        canonical.model_dump(mode="json"),
        stored.model_dump(mode="json"),
        "",
    )


def _canonical_projection_violations(
    canonical_proof: dict[str, Any],
    *,
    requirement_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in requirement_rows:
        rows_by_id.setdefault(str(row.get("id") or ""), []).append(row)
    results = {
        str(item.get("node_id") or ""): item
        for item in _dict_items(canonical_proof.get("node_results"))
        if item.get("node_id")
    }
    violations: list[dict[str, Any]] = []
    for decision in _dict_items(canonical_proof.get("decisions")):
        requirement_id = str(decision.get("requirement_id") or "")
        rows = rows_by_id.get(requirement_id, [])
        if len(rows) != 1:
            violations.append(
                {
                    "requirement_id": requirement_id,
                    "error": "missing" if not rows else "duplicate",
                }
            )
            continue
        status = str(decision.get("status") or "NOT_FOUND")
        leaf_field = {
            "SUPPORTED": "supporting_check_ids",
            "CONTRADICTED": "contradicting_check_ids",
            "NOT_FOUND": "unresolved_check_ids",
        }.get(status, "unresolved_check_ids")
        evidence_ids: list[str] = []
        for check_id in _list(decision.get(leaf_field)):
            for source_id in _list(_mapping(results.get(str(check_id))).get("source_ids")):
                normalized = str(source_id)
                if normalized and normalized not in evidence_ids:
                    evidence_ids.append(normalized)
        expected_status = {
            "SUPPORTED": "accepted" if requirement_owner(requirement_id) == "evidence" else "satisfied",
            "CONTRADICTED": "conflict",
            "NOT_FOUND": "weak" if evidence_ids else "missing",
        }.get(status, "missing")
        row = rows[0]
        observed_status = str(row.get("status") or "")
        observed_evidence_ids = sorted(str(item) for item in _list(row.get("evidence_ids")) if str(item))
        if observed_status != expected_status or observed_evidence_ids != sorted(evidence_ids):
            violations.append(
                {
                    "requirement_id": requirement_id,
                    "expected_status": expected_status,
                    "observed_status": observed_status or "缺失",
                    "expected_evidence_ids": sorted(evidence_ids),
                    "observed_evidence_ids": observed_evidence_ids,
                }
            )
    return violations


def _claim_is_grounded(
    claim: dict[str, Any],
    *,
    source_ids: set[str],
    source_content: dict[str, str],
) -> bool:
    claim_id = str(claim.get("id") or "").strip()
    source_id = str(claim.get("source_id") or "").strip()
    quote = str(claim.get("quote") or "").strip()
    locator = str(claim.get("locator") or "").strip()
    if not claim_id or not source_id or not quote or not locator:
        return False
    admitted_sources = source_ids or set(source_content)
    if source_id not in admitted_sources:
        return False
    content = source_content.get(source_id, "")
    if not content or quote not in content:
        return False
    if not _locator_supports_quote(content, locator=locator, quote=quote):
        return False
    return str(claim.get("confidence") or "medium").casefold() != "low"


def _relevant_claims_are_grounded(
    relevant_claim_ids: set[str],
    *,
    grounded_claim_ids: set[str],
    claims: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
) -> bool:
    claims_by_id = {str(item.get("id") or ""): item for item in claims}
    for assessment in assessments:
        if str(assessment.get("status") or "") not in STRONG_STATUSES:
            continue
        assessment_claims = {str(item) for item in _list(assessment.get("claim_ids"))}
        if not assessment_claims or not assessment_claims.issubset(grounded_claim_ids):
            return False
        cited_sources = {str(item) for item in _list(assessment.get("source_ids"))}
        required_sources = {
            str(claims_by_id[claim_id].get("source_id") or "")
            for claim_id in assessment_claims
            if claim_id in claims_by_id
        }
        if not required_sources or required_sources != cited_sources:
            return False
    return bool(relevant_claim_ids) and relevant_claim_ids.issubset(grounded_claim_ids)


def _reachable_ids(root_id: str, nodes: dict[str, dict[str, Any]]) -> set[str]:
    if not root_id or root_id not in nodes:
        return set()
    result: set[str] = set()
    pending = [root_id]
    while pending:
        node_id = pending.pop()
        if node_id in result or node_id not in nodes:
            continue
        result.add(node_id)
        pending.extend(str(item) for item in _list(nodes[node_id].get("depends_on")))
    return result


def _visible_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_visible_content(item) for item in value)))
    if not isinstance(value, dict):
        return ""
    block_type = str(value.get("type") or "").casefold()
    if "reasoning" in block_type or "thinking" in block_type:
        return ""
    for key in ("text", "content", "message"):
        if key in value:
            return _visible_content(value[key])
    return ""


def _conversation_text(
    conversation: list[dict[str, Any]],
    *,
    role: str,
    final_only: bool,
) -> str:
    messages = [
        _visible_content(item.get("content", item.get("text", ""))).strip()
        for item in conversation
        if isinstance(item, dict) and str(item.get("role") or "").casefold() == role
    ]
    messages = [item for item in messages if item]
    if final_only:
        return messages[-1] if messages else ""
    return "\n".join(messages)


def _meaning_groups_match(text: str, groups: list[list[str]]) -> bool:
    normalized = _semantic_normalized(text)
    return all(
        any(
            _semantic_normalized(option) in normalized
            for option in group
            if _semantic_normalized(option)
        )
        for group in groups
    )


def _affirmative_meaning_matches(text: str, meaning: Any) -> bool:
    clauses = [
        item.strip()
        for item in re.split(
            r"(?:\r?\n)+|(?<=[.!?。！？；;，])|\b(?:but|however)\b|但(?:是|而)?",
            text,
            flags=re.IGNORECASE,
        )
        if item.strip()
    ]
    return any(_affirmative_groups_match(clause, meaning) for clause in clauses)


def _affirmative_document_groups_match(text: str, groups: list[list[str]]) -> bool:
    """Match each semantic group affirmatively, allowing groups in different clauses."""
    clauses = [
        item.strip()
        for item in re.split(
            r"(?:\r?\n)+|(?<=[.!?。！？；;，])|\b(?:but|however)\b|但(?:是|而)?",
            text,
            flags=re.IGNORECASE,
        )
        if item.strip()
    ]
    return all(
        any(_affirmative_groups_match(clause, [group]) for clause in clauses)
        for group in groups
    )


def _affirmative_groups_match(text: str, meaning: Any) -> bool:
    normalized = _semantic_normalized(text)
    groups = meaning if isinstance(meaning, list) else getattr(meaning, "all_of", [])
    for group in list(groups):
        matched = False
        for option in group:
            needle = _semantic_normalized(option)
            if not needle:
                continue
            for occurrence in re.finditer(re.escape(needle), normalized):
                prefix = normalized[max(0, occurrence.start() - 16) : occurrence.start()]
                if re.search(
                    r"(?:\b(?:not|no|never|without)\s+|(?:不|未|无|并非|没有|并不)(?:完全|一定|能|是)?\s*)$",
                    prefix,
                    re.IGNORECASE,
                ):
                    continue
                matched = True
                break
            if matched:
                break
        if not matched:
            return False
    return True


def _boundary_output_assertions(text: str, meaning: Any) -> list[tuple[str, str]]:
    assertions: list[tuple[str, str]] = []
    clauses = [
        item.strip()
        for item in re.split(
            r"(?:\r?\n)+|(?<=[.!?。！？；;，])|\b(?:but|however)\b|但(?:是|而)?",
            text,
            flags=re.IGNORECASE,
        )
        if item.strip()
    ]
    for clause in clauses:
        if not _meaning_oracle_matches(clause, meaning):
            continue
        status = (
            "NOT_FOUND"
            if _UNCERTAINTY.search(clause)
            else "CONTRADICTED"
            if _CONTRADICTION.search(clause)
            else "SUPPORTED"
        )
        assertions.append((status, " ".join(clause.split())[:180]))
    return assertions


def _score_cap(
    vetoes: list[EvalVeto],
    failed_core: list[ScoreCheck],
) -> tuple[Decimal, str]:
    if vetoes:
        severe = [item.code for item in vetoes if item.code != "EPISTEMIC_BOUNDARY_VIOLATION"]
        if severe:
            return SEVERE_VETO_SCORE_CAP, f"严重 veto：{', '.join(severe)}"
        return EPISTEMIC_VETO_SCORE_CAP, "非目标证据边界越权"
    if failed_core:
        return CORE_FAILURE_SCORE_CAP, f"核心检查失败：{failed_core[0].id}"
    return Decimal("100"), ""


def _reply_references_report(reply: str, snapshot: EvalSnapshot) -> bool:
    normalized = reply.casefold()
    if any(token in normalized for token in (".pdf", ".md", "[报告", "下载报告", "/reports/")):
        return True
    return any(item.path and item.path.casefold() in normalized for item in snapshot.reports)


def _approved_tools(snapshot: EvalSnapshot) -> set[str]:
    approved = {str(item) for item in snapshot.approved_tools if str(item)}
    for event in snapshot.events:
        if not isinstance(event, dict) or event.get("kind") != "approval_decision":
            continue
        payload = _mapping(event.get("payload"))
        if bool(payload.get("approved")) and payload.get("tool"):
            approved.add(str(payload["tool"]))
    return approved


def _engineering_metrics(snapshot: EvalSnapshot) -> dict[str, Any]:
    provider_calls = [item for item in snapshot.events if item.get("kind") == "provider_call"]
    api_prompt_tokens = 0
    api_completion_tokens = 0
    api_total_tokens = 0
    api_cached_tokens = 0
    for event in provider_calls:
        usage = _mapping(_mapping(event.get("payload")).get("usage"))
        api_prompt_tokens += int(usage.get("prompt_tokens") or 0)
        api_completion_tokens += int(usage.get("completion_tokens") or 0)
        api_total_tokens += int(usage.get("total_tokens") or 0)
        api_cached_tokens += int(usage.get("cached_tokens") or 0)
    error_events = [item for item in snapshot.events if _event_is_error(item)]
    detailed_error = next(
        (
            item
            for item in snapshot.events
            if "failed:" in str(item.get("summary") or "").casefold()
            or "error:" in str(item.get("summary") or "").casefold()
        ),
        error_events[0] if error_events else {},
    )
    hook_rejections = sum(
        1
        for item in snapshot.events
        if bool(_mapping(item.get("payload")).get("hook_code"))
    )
    model_metrics = _mapping(_mapping(snapshot.trace.get("observability")).get("model_metrics"))
    trace_path = str(snapshot.trace.get("path") or "")
    if not trace_path and snapshot.transcript_path:
        suffix = f"/{snapshot.run_id}/deepseek_calls.txt"
        trace_path = (
            snapshot.transcript_path[: -len(suffix)] + f"/{snapshot.run_id}.json"
            if snapshot.transcript_path.endswith(suffix)
            else ""
        )
    return {
        "provider_calls": len(provider_calls),
        "role_calls": int(model_metrics.get("call_count") or 0),
        "api_prompt_tokens": api_prompt_tokens,
        "api_completion_tokens": api_completion_tokens,
        "api_total_tokens": api_total_tokens,
        "api_cached_tokens": api_cached_tokens,
        "role_total_tokens": int(model_metrics.get("total_tokens") or 0),
        "role_cached_tokens": int(model_metrics.get("cached_tokens") or 0),
        "duration_ms": _duration_ms(snapshot),
        "error_events": len(error_events),
        "first_error": {
            "seq": detailed_error.get("seq", 0),
            "kind": detailed_error.get("kind", ""),
            "name": detailed_error.get("name", ""),
            "summary": str(detailed_error.get("summary") or "")[:500],
        }
        if detailed_error
        else {},
        "blocked_actions": sum(1 for item in snapshot.events if item.get("kind") == "supervisor_decision_blocked"),
        "hook_rejections": hook_rejections,
        "report_count": len(snapshot.reports),
        "report_bytes": sum(item.bytes for item in snapshot.reports),
        "approved_tools": sorted(_approved_tools(snapshot)),
        "trace_path": trace_path,
        "transcript_path": snapshot.transcript_path,
    }


def _event_is_error(event: dict[str, Any]) -> bool:
    kind = str(event.get("kind") or "").casefold()
    payload = _mapping(event.get("payload"))
    status = str(payload.get("status") or event.get("status") or "").casefold()
    summary = str(event.get("summary") or "").casefold()
    return (
        kind in {"error", "provider_error", "tool_error"}
        or status in {"error", "failed"}
        or (kind == "role_call" and "failed" in summary)
    )


def _duration_ms(snapshot: EvalSnapshot) -> int:
    if snapshot.trace.get("duration_ms"):
        return int(snapshot.trace["duration_ms"])
    try:
        started = datetime.fromisoformat(snapshot.started_at)
        completed = datetime.fromisoformat(snapshot.completed_at)
    except (TypeError, ValueError):
        return 0
    return max(0, int((completed - started).total_seconds() * 1000))


def _assert_stage_weights(checks: list[ScoreCheck]) -> None:
    for stage, expected in STAGE_WEIGHTS.items():
        actual = sum((item.points for item in checks if item.stage == stage), Decimal("0"))
        if actual != expected:
            raise RuntimeError(f"{stage} checks total {actual}, expected {expected}")
