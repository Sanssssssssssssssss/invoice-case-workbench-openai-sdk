from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from app.compiler_runtime.graph_walk import reachable_ids as walk_reachable_ids
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
    FrameworkCheck,
    FrameworkOracle,
    OutcomeKind,
    ScoreCheck,
)


SCORER_VERSION = "business_eval_scorer_v3.3"

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
    r"待配置|未决|未配置|需(?:要)?配置|无(?:参考)?基准",
    re.IGNORECASE,
)
_CONTRADICTION = re.compile(
    r"\b(?:incorrect|invalid|inapplicable|contradicted|false)\b|错误|不正确|不适用|矛盾|反驳",
    re.IGNORECASE,
)
_EXPLICIT_RELATION_SYNTAX = re.compile(r"等于|之和|加上|减去|[=≠≤≥<>×÷+]")


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
    reachable_ids = (
        walk_reachable_ids(
            root_id,
            lambda node_id: (
                _list(nodes[node_id].get("depends_on")) if node_id in nodes else ()
            ),
        )
        if root_id and root_id in nodes
        else set()
    )
    assessments = {
        str(item.get("check_id") or ""): item
        for item in _dict_items(artifact.get("assessments"))
        if item.get("check_id")
    }
    canonical_proof, stored_proof, kernel_error = _recompile_proof(state, artifact, proof)
    canonical_node_results = {
        str(item.get("node_id") or ""): item
        for item in _dict_items(canonical_proof.get("node_results"))
        if item.get("node_id")
    }
    stored_node_results = {
        str(item.get("node_id") or ""): item
        for item in _dict_items(stored_proof.get("node_results"))
        if item.get("node_id")
    }
    # Business credit is granted only to the intersection of the NodeResult
    # persisted by the run and the current Kernel replay.  A stale/forged
    # stored result cannot earn points, and a later Kernel implementation
    # cannot retroactively upgrade what the evaluated run actually admitted.
    admitted_node_results = {
        node_id: (
            canonical_result
            if _mapping(stored_node_results.get(node_id)) == canonical_result
            else {
                "node_id": node_id,
                "kind": canonical_result.get("kind", "CHECK"),
                "status": "",
                "claim_ids": [],
                "binding_ids": [],
                "witness_ids": [],
                "source_ids": [],
            }
        )
        for node_id, canonical_result in canonical_node_results.items()
    }
    canonical_decision = next(
        (
            item
            for item in _dict_items(canonical_proof.get("decisions"))
            if str(item.get("requirement_id") or "") == requirement_id
        ),
        {},
    )
    replayed_status = str(canonical_decision.get("status") or "")
    diagnostics = _dict_items(canonical_proof.get("diagnostics"))
    plan_check_ids = {
        node_id
        for node_id, node in nodes.items()
        if str(node.get("kind") or "") == "CHECK"
    }
    global_integrity_rejected = bool(
        kernel_error
        or any(
            not (node_id := str(item.get("node_id") or ""))
            or node_id not in plan_check_ids
            for item in diagnostics
        )
        or (canonical_proof and stored_proof != canonical_proof)
    )
    check_outcomes = {
        check_id: _check_outcome(
            check_id,
            artifact=artifact,
            nodes=nodes,
            assessments=assessments,
            node_results=canonical_node_results,
            diagnostics=diagnostics,
            global_integrity_rejected=global_integrity_rejected,
        )
        for check_id in reachable_ids
        if str(_mapping(nodes.get(check_id)).get("kind") or "") == "CHECK"
    }
    target_outcome = _target_outcome(
        root_id=root_id,
        nodes=nodes,
        canonical_decision=canonical_decision,
        canonical_node_results=canonical_node_results,
        check_outcomes=check_outcomes,
        global_integrity_rejected=global_integrity_rejected,
    )

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
    # Facets route a freely-shaped ProofPlan into Oracle milestones.  They are
    # structural types, not semantic truth: statement meaning is scored below
    # as an independent compiler-understanding diagnostic.  Evidence and
    # arithmetic scoring deliberately continue from the facet route even when
    # that diagnostic fails, so wording cannot zero an otherwise grounded
    # typed proof graph.
    milestone_matches = _match_milestone_facets(required_milestones, target_checks)
    frozen_objective = _normalized(plan.get("objective")) == _normalized(case.user_message)
    typed_objective = (
        frozen_objective
        and target_covered
        and all(milestone_matches.get(item.id) for item in required_milestones)
    )
    objective_ok = _meaning_oracle_matches(
        objective_text,
        oracle.intent.objective,
    ) or typed_objective
    add(
        "understanding.objective_semantics",
        "understanding",
        2,
        objective_ok,
        core=True,
        expected=oracle.intent.objective.all_of,
        observed=str(plan.get("objective") or ""),
        detail=(
            "核对冻结用户目标与目标 root 下的 typed milestone 路由；"
            "不要求 Plan 重复某个算术字面词。"
        ),
    )
    for milestone, points in zip(
        required_milestones,
        _split_points(Decimal("5"), len(required_milestones)),
    ):
        facet_ids = sorted(milestone_matches.get(milestone.id, set()))
        statement_hits = {
            check_id: _meaning_group_indexes(
                str(target_checks[check_id].get("statement") or ""),
                milestone.statement_meaning,
            )
            for check_id in facet_ids
        }
        matched_ids = [check_id for check_id in facet_ids if statement_hits[check_id]]
        covered_groups = set().union(*statement_hits.values()) if statement_hits else set()
        group_count = len(milestone.statement_meaning.all_of)
        required_group_count = max(1, (group_count * 3 + 4) // 5)
        # One typed facet may legitimately be implemented by several CHECKs,
        # including distinct business paths under the same facet.  Extra
        # CHECKs do not have to repeat every milestone phrase; collectively
        # they must still express the milestone's business meaning.
        relation_syntax_ok = bool(milestone.relation_ids) and any(
            _relational_statement_matches(
                str(target_checks[check_id].get("statement") or ""),
                milestone.statement_meaning,
            )
            for check_id in facet_ids
        )
        semantic_ok = bool(facet_ids) and (
            len(covered_groups) >= required_group_count or relation_syntax_ok
        )
        facet_points = (points * Decimal("0.60")).quantize(Decimal("0.01"))
        semantic_points = points - facet_points
        add(
            f"understanding.milestone.{milestone.id}",
            "understanding",
            facet_points,
            bool(facet_ids),
            core=True,
            expected="target-root facet route",
            observed={
                "facet_check_ids": facet_ids,
            },
            detail="目标 root 下必须有 facet_refs 对应该里程碑的 CHECK。",
        )
        add(
            f"understanding.milestone_semantics.{milestone.id}",
            "understanding",
            semantic_points,
            semantic_ok,
            core=False,
            expected="CHECK statement matches milestone.statement_meaning",
            observed={
                "facet_check_ids": facet_ids,
                "relevant_check_ids": matched_ids,
                "covered_groups": len(covered_groups),
                "required_groups": required_group_count,
                **(
                    {"relation_syntax_fallback": True}
                    if relation_syntax_ok
                    else {}
                ),
            },
            detail="CHECK 文案仅作诊断；typed facet、proof terms 与 Kernel outcome 决定核心语义。",
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
    relevant_node_results = [
        admitted_node_results[node_id]
        for node_id in reachable_ids
        if node_id in admitted_node_results
        and str(_mapping(nodes.get(node_id)).get("kind") or "") == "CHECK"
    ]
    relevant_claim_ids = {
        str(claim_id)
        for result in relevant_node_results
        for claim_id in _list(result.get("claim_ids"))
        if str(claim_id)
    }
    accepted_witness_ids = {
        str(witness_id)
        for result in relevant_node_results
        for witness_id in _list(result.get("witness_ids"))
        if str(witness_id)
    }
    calculation_witnesses = _dict_items(artifact.get("calculation_witnesses"))
    witnesses_by_id = {
        str(item.get("id") or ""): item
        for item in calculation_witnesses
        if item.get("id")
    }
    subtracted_claim_ids = {
        str(_mapping(operands[1].get("ref")).get("ref_id") or "")
        for witness in calculation_witnesses
        if str(witness.get("id") or "") in accepted_witness_ids
        and str(witness.get("operation") or "") == "SUBTRACT"
        and len(operands := _dict_items(witness.get("operands"))) == 2
        and str(_mapping(operands[1].get("ref")).get("kind") or "") == "CLAIM"
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
        subtracted_claim_ids=subtracted_claim_ids,
    )
    subtracted_fact_ids = {
        fact_id
        for fact_id, claim_ids in fact_claim_ids.items()
        if claim_ids & subtracted_claim_ids
    }
    fact_owner_claim_ids = {
        fact.id: {
            str(claim_id)
            for milestone in required_milestones
            if fact.id in milestone.fact_ids
            for check_id in _upstream_check_closure(
                set(milestone_matches.get(milestone.id, set())),
                nodes=nodes,
            )
            for claim_id in _list(
                _mapping(admitted_node_results.get(check_id)).get("claim_ids")
            )
            if str(claim_id)
        }
        for fact in evidence_facts
    }
    evidence_fact_claim_ids = {
        fact.id: fact_claim_ids.get(fact.id, set())
        & fact_owner_claim_ids.get(fact.id, set())
        for fact in evidence_facts
    }
    evidence_assignments = _unique_fact_assignments(
        [fact.id for fact in evidence_facts],
        fact_claim_ids=evidence_fact_claim_ids,
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
            detail=(
                "只计目标 facet 的 Kernel Claim；核对值、币种、来源定位与业务语义。"
            ),
        )
    if not evidence_facts:
        add("evidence.no_required_facts", "evidence", 12, True)

    facts_by_id = {item.id: item for item in oracle.facts}
    relation_owner = {
        relation_id: milestone
        for milestone in oracle.milestones
        for relation_id in milestone.relation_ids
    }
    relation_matches, relation_witness_outputs = _match_typed_relation_witnesses(
        oracle.relations,
        relation_owner=relation_owner,
        milestone_check_ids=milestone_matches,
        canonical_node_results=admitted_node_results,
        calculation_witnesses=calculation_witnesses,
        facts_by_id=facts_by_id,
        fact_claim_ids=fact_claim_ids,
    )
    scoring_milestone_matches = _refine_shared_facet_matches(
        required_milestones,
        milestone_matches,
        relation_matches=relation_matches,
        checks=target_checks,
    )

    milestone_link_results: list[bool] = []
    for milestone, points in zip(
        required_milestones,
        _split_points(Decimal("8"), len(required_milestones)),
    ):
        check_ids = sorted(scoring_milestone_matches.get(milestone.id, set()))
        lineage_check_ids = _upstream_check_closure(set(check_ids), nodes=nodes)
        accepted_claim_ids = {
            str(claim_id)
            for check_id in lineage_check_ids
            for claim_id in _list(
                _mapping(admitted_node_results.get(check_id)).get("claim_ids")
            )
            if str(claim_id)
        }
        milestone_witness_ids = {
            str(witness_id)
            for check_id in lineage_check_ids
            for witness_id in _list(
                _mapping(admitted_node_results.get(check_id)).get("witness_ids")
            )
            if str(witness_id)
        }
        lineage_witness_ids, lineage_claim_ids = _accepted_witness_lineage(
            milestone_witness_ids,
            accepted_witness_ids=accepted_witness_ids,
            witnesses_by_id=witnesses_by_id,
        )
        accepted_claim_ids.update(lineage_claim_ids)
        required_source_facts = [
            fact_id
            for fact_id in milestone.fact_ids
            if fact_id in {item.id for item in oracle.facts if item.origin == "source"}
        ]
        assignments = _unique_fact_assignments(
            required_source_facts,
            fact_claim_ids=fact_claim_ids,
            allowed_claim_ids=accepted_claim_ids,
        )
        relation_covered_facts = {
            fact_id
            for witness_id in lineage_witness_ids
            for fact_id in relation_witness_outputs.get(witness_id, set())
            if fact_id
        }
        missing = [
            fact_id
            for fact_id in required_source_facts
            if fact_id not in assignments and fact_id not in relation_covered_facts
        ]
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
            detail="里程碑 facet 的 Kernel NodeResult 必须接纳所需来源事实。",
        )
    if not required_milestones:
        add("evidence.no_required_milestones", "evidence", 8, True)

    relevant_grounded = _relevant_claims_are_grounded(
        relevant_claim_ids,
        grounded_claim_ids=grounded_claim_ids,
        claims=claims,
        assessments=relevant_node_results,
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
        observed_outcome = _aggregate_milestone_outcome(
            check_ids,
            check_outcomes,
            root_id=root_id,
            nodes=nodes,
        )
        expected_outcome = _oracle_outcome(milestone.expected_status)
        passed = bool(check_ids) and observed_outcome == expected_outcome
        milestone_status_results.append(passed)
        add(
            f"reasoning.milestone_status.{milestone.id}",
            "reasoning",
            points,
            passed,
            core=True,
            expected=expected_outcome,
            observed={
                "check_ids": check_ids,
                "outcome": observed_outcome,
            },
            detail="按目标 facet 的实际 ALL/ANY 拓扑比较因果 outcome；执行缺口不能伪装成业务 NOT_FOUND。",
        )

    relation_results: list[bool] = []
    relation_points = _relation_points(oracle.milestones, oracle.relations, Decimal("14"))
    for relation in oracle.relations:
        milestone = relation_owner.get(relation.id)
        match = relation_matches.get(relation.id)
        check_id = match[0] if match else ""
        witness_id = match[1] if match else ""
        witness = next(
            (
                item
                for item in calculation_witnesses
                if str(item.get("id") or "") == witness_id
            ),
            {},
        )
        passed = match is not None
        relation_results.append(passed)
        add(
            f"reasoning.relation.{relation.id}",
            "reasoning",
            relation_points.get(relation.id, Decimal("0")),
            passed,
            core=True,
            expected=_relation_expected(relation, facts_by_id),
            observed={
                "check_id": check_id,
                "witness_id": witness_id,
                "operation": str(witness.get("operation") or ""),
            },
            detail=(
                "只认 Kernel 接纳 Witness 的 operation、递归 operands 与 result；不读 reason。"
            ),
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

    canonical_markdown_text = "\n".join(
        item.text for item in snapshot.reports if item.kind == "markdown" and item.text
    )
    canonical_pdf_text = _canonical_pdf_body(
        "\n".join(
            item.text for item in snapshot.reports if item.kind == "pdf" and item.text
        )
    )
    # Business assertions come from the canonical Markdown.  The PDF may carry
    # a clearly labelled raw-material appendix for human inspection; OCR labels
    # in that appendix are evidence previews, not system conclusions.
    report_text = canonical_markdown_text
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
            if any(
                _meaning_oracle_matches(proposition, boundary.statement_meaning)
                for text in (
                    str(node.get("statement") or ""),
                    str(_mapping(assessments.get(check_id)).get("reason") or ""),
                )
                for proposition in _boundary_propositions(text)
            )
        ]
        bad = [
            check_id
            for check_id in matched_checks
            if str(_mapping(assessments.get(check_id)).get("status") or "")
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
    replayed_by_node = {
        node_id: str(_mapping(canonical_node_results.get(node_id)).get("status") or "缺失")
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
    expected_target_outcome = _oracle_outcome(oracle.requirement.decision_status)
    target_truth_ok = target_outcome == expected_target_outcome
    add(
        "proof.target_decision_truth",
        "proof",
        0,
        target_truth_ok,
        core=True,
        expected=expected_target_outcome,
        observed=target_outcome,
        detail="强结论先由 Kernel 拓扑决定；仅 canonical 根未决时才区分业务证据缺口与执行/完整性失败。",
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
        bool(canonical_decision)
        and blocking == oracle.requirement.blocking_obligations,
        core=True,
        expected=oracle.requirement.blocking_obligations,
        observed=blocking,
    )
    proof_integrity_ok = (
        not global_integrity_rejected
        and stored_proof == canonical_proof
        and hash_integrity
        and node_results_ok
        and decision_kernel_ok
    )

    report_text_by_kind = {
        "markdown": canonical_markdown_text,
        "pdf": canonical_pdf_text,
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
                kind: _fact_matches(
                    fact,
                    report_text_by_kind[kind],
                    allow_subtracted_value=fact.id in subtracted_fact_ids,
                )
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
        missing_milestones = [
            milestone
            for milestone in required_milestones
            if milestone.expected_status == "NOT_FOUND"
            and milestone.missing_meaning is not None
        ]
        meaning_points = _split_points(
            Decimal("4"),
            1 + len(missing_milestones),
        )
        report_meaning_ok = _required_business_meanings_match(
            canonical_markdown_text,
            oracle=oracle,
            facts_by_id=facts_by_id,
        )
        add(
            "report.business_meaning",
            "report",
            meaning_points[0],
            report_meaning_ok,
            core=True,
            expected=oracle.communication.required_meanings,
            observed={"canonical_markdown": report_meaning_ok},
            detail="业务结论只认 canonical Markdown；PDF 原始材料附录不参与结论评分。",
        )
        for milestone, points in zip(missing_milestones, meaning_points[1:]):
            missing_match = _scoped_missing_meaning_matches(
                canonical_markdown_text,
                milestone.missing_meaning,
            )
            add(
                f"report.missing_meaning.{milestone.id}",
                "report",
                points,
                missing_match,
                core=True,
                expected=milestone.missing_meaning.all_of,
                observed={"canonical_markdown": missing_match},
                detail="NOT_FOUND 报告必须说明阻断该里程碑的具体证据缺口。",
            )
    else:
        add("report.not_required", "report", 15, True)

    linked_report = (
        canonical_markdown_text
        if _reply_references_report(reply_text, snapshot)
        else ""
    )
    communication_text = f"{reply_text}\n{linked_report}".strip()
    reply_facts = [fact for fact in oracle.facts if "reply" in fact.required_in]
    for fact, points in zip(reply_facts, _split_points(Decimal("4"), len(reply_facts))):
        matched = _fact_matches(
            fact,
            communication_text,
            allow_subtracted_value=fact.id in subtracted_fact_ids,
        )
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
    meanings_ok = _required_business_meanings_match(
        communication_text,
        oracle=oracle,
        facts_by_id=facts_by_id,
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
                    f"canonical outcome 为 {target_outcome}，"
                    f"Oracle 要求 {expected_target_outcome}。"
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
        expected_target_outcome == "BUSINESS_EVIDENCE_GAP"
        or target_outcome in {"BUSINESS_EVIDENCE_GAP", "EXECUTION_INCOMPLETE"}
    ):
        vetoes.append(
            EvalVeto(
                code="NOT_FOUND_UPGRADED",
                detail="业务证据缺口或未完成执行被升级为强结论。",
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
            if any(item.core and not item.passed for item in checks if item.stage == stage)
        ),
        "",
    )
    business_passed = not vetoes and not failed_core and score >= Decimal("90")
    framework_score, framework_passed, framework_checks = _score_framework_protocol(
        oracle.framework,
        snapshot,
    )
    passed = business_passed and (framework_passed if oracle.framework is not None else True)
    return EvalResult(
        case_id=case.case_id,
        case_version=case.case_version,
        run_id=snapshot.run_id,
        scorer_version=SCORER_VERSION,
        oracle_version=oracle.oracle_version,
        passed=passed,
        business_passed=business_passed,
        score=score,
        raw_score=raw_score,
        score_cap=score_cap,
        score_cap_reason=score_cap_reason,
        first_failed_stage=first_failed_stage,
        checks=checks,
        framework_enabled=oracle.framework is not None,
        framework_score=framework_score,
        framework_passed=framework_passed,
        framework_checks=framework_checks,
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
    text = text.replace("行扩展金额", "行金额")
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


def _meaning_group_indexes(text: str, meaning: Any) -> set[int]:
    """Return the Oracle meaning groups touched by one atomic CHECK statement."""
    normalized = _semantic_normalized(text)
    return {
        index
        for index, group in enumerate(list(getattr(meaning, "all_of", [])))
        if any(
            (needle := _semantic_normalized(option)) and needle in normalized
            for option in group
        )
    }


def _relational_statement_matches(text: str, meaning: Any) -> bool:
    """Recognize a relation between two distinct business meanings."""
    clauses = [
        item.strip()
        for item in re.split(r"(?:\r?\n)+|[。！？；;，,]", text)
        if item.strip()
    ]
    for clause in clauses:
        if not _EXPLICIT_RELATION_SYNTAX.search(clause):
            continue
        normalized = _semantic_normalized(clause)
        group_spans: dict[int, set[tuple[int, int]]] = {}
        for index, group in enumerate(list(getattr(meaning, "all_of", []))):
            for option in group:
                needle = _semantic_normalized(option)
                if not needle:
                    continue
                for match in re.finditer(re.escape(needle), normalized):
                    group_spans.setdefault(index, set()).add(match.span())
        matches = list(group_spans.items())
        if any(
            left_end <= right_start or right_end <= left_start
            for left_offset, (_, left_spans) in enumerate(matches)
            for _, right_spans in matches[left_offset + 1 :]
            for left_start, left_end in left_spans
            for right_start, right_end in right_spans
        ):
            return True
    return False


def _identifier_terms(value: Any) -> set[str]:
    """Normalize structural ids without introducing a business alias table."""
    terms: set[str] = set()
    for raw in re.findall(r"[a-z]+|\d+", str(value or "").casefold()):
        if raw == "including":
            raw = "inclusive"
        if raw.endswith("ies") and len(raw) > 4:
            raw = f"{raw[:-3]}y"
        elif raw.endswith("s") and not raw.endswith("ss") and len(raw) > 3:
            raw = raw[:-1]
        terms.add(raw)
    return terms


def _facet_corresponds_to_milestone(facet_ref: str, milestone_id: str) -> bool:
    """Match a tiny Signature facet to a possibly more descriptive Oracle id.

    Exact ids are preferred.  A two-or-more-token structural id may also be a
    subset of the other side, which covers e.g. ``final_total`` →
    ``final_total_reconciliation`` and ``stated_components`` →
    ``stated_component_rate_base_validation`` without a case-specific alias
    registry.  A generic one-token overlap such as ``total`` is never enough.
    """
    facet = _semantic_normalized(facet_ref)
    milestone = _semantic_normalized(milestone_id)
    if not facet or not milestone:
        return False
    if facet == milestone:
        return True
    facet_terms = _identifier_terms(facet_ref)
    milestone_terms = _identifier_terms(milestone_id)
    smaller = min(len(facet_terms), len(milestone_terms))
    return smaller >= 2 and (
        facet_terms.issubset(milestone_terms)
        or milestone_terms.issubset(facet_terms)
    )


def _match_milestone_facets(
    milestones: list[Any],
    checks: dict[str, dict[str, Any]],
) -> dict[str, set[str]]:
    """Route target-root CHECKs by typed facet; never inspect prose here."""
    return {
        milestone.id: {
            check_id
            for check_id, node in checks.items()
            if any(
                (
                    _semantic_normalized(facet_ref)
                    == _semantic_normalized(milestone.facet_ref)
                    if str(getattr(milestone, "facet_ref", ""))
                    else _facet_corresponds_to_milestone(
                        str(facet_ref),
                        milestone.id,
                    )
                )
                for facet_ref in _list(node.get("facet_refs"))
            )
        }
        for milestone in milestones
    }


def _refine_shared_facet_matches(
    milestones: list[Any],
    initial: dict[str, set[str]],
    *,
    relation_matches: dict[str, tuple[str, str]],
    checks: dict[str, dict[str, Any]],
) -> dict[str, set[str]]:
    """Separate multiple milestone paths that intentionally share one facet.

    Facets remain the only routing type.  When the Oracle has two semantic
    paths under that type, the accepted relation Witness owners identify the
    path without comparing against the expected status.  Relation-free
    semantic milestones may use their CHECK meaning; if neither signal exists
    the complete facet route is retained rather than guessed.
    """
    result = {milestone_id: set(check_ids) for milestone_id, check_ids in initial.items()}
    by_facet: dict[str, list[Any]] = {}
    for milestone in milestones:
        facet_ref = str(getattr(milestone, "facet_ref", "") or milestone.id)
        by_facet.setdefault(_semantic_normalized(facet_ref), []).append(milestone)
    for shared in by_facet.values():
        if len(shared) < 2:
            continue
        for milestone in shared:
            relation_check_ids = {
                relation_matches[relation_id][0]
                for relation_id in milestone.relation_ids
                if relation_id in relation_matches
            }
            if relation_check_ids:
                result[milestone.id] = relation_check_ids
                continue
            semantic_check_ids = {
                check_id
                for check_id in initial.get(milestone.id, set())
                if _meaning_oracle_matches(
                    str(_mapping(checks.get(check_id)).get("statement") or ""),
                    milestone.statement_meaning,
                )
            }
            if semantic_check_ids:
                result[milestone.id] = semantic_check_ids
    return result


def _oracle_outcome(status: str) -> OutcomeKind:
    try:
        return {
            "SUPPORTED": "SUPPORTED",
            "CONTRADICTED": "CONTRADICTED",
            "NOT_FOUND": "BUSINESS_EVIDENCE_GAP",
        }[status]
    except KeyError as exc:
        raise ValueError(f"unknown Oracle status: {status!r}") from exc


def _check_outcome(
    check_id: str,
    *,
    artifact: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    assessments: dict[str, dict[str, Any]],
    node_results: dict[str, dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    global_integrity_rejected: bool,
) -> OutcomeKind:
    if global_integrity_rejected:
        return "INTEGRITY_REJECTED"
    node = _mapping(nodes.get(check_id))
    assessment = _mapping(assessments.get(check_id))
    result = _mapping(node_results.get(check_id))
    submitted = any(
        check_id in _mapping(artifact.get(field))
        for field in (
            "submitted_claim_refs",
            "submitted_binding_refs",
            "submitted_witness_refs",
        )
    )
    if (
        str(node.get("kind") or "") != "CHECK"
        or not submitted
        or not assessment
        or not result
    ):
        return "EXECUTION_INCOMPLETE"
    check_diagnostics = [
        item
        for item in diagnostics
        if str(item.get("node_id") or "") == check_id
    ]
    diagnostic_codes = {str(item.get("code") or "") for item in check_diagnostics}
    assessment_status = str(assessment.get("status") or "")
    result_status = str(result.get("status") or "")
    assessment_gap = str(assessment.get("gap_code") or "")
    result_gap = str(result.get("gap_code") or "")
    has_missing_fact = bool(str(assessment.get("missing_fact") or "").strip())
    blocked_policy_refs = (
        set(_list(node.get("policy_refs")))
        & set(_list(artifact.get("unconfigured_policy_refs")))
        & set(_list(_mapping(artifact.get("plan")).get("policy_refs")))
    )
    if (
        diagnostic_codes == {"POLICY_NOT_CONFIGURED"}
        and blocked_policy_refs
        and assessment_status == result_status == "NOT_FOUND"
        and assessment_gap == result_gap == "POLICY_UNCONFIGURED"
        and has_missing_fact
    ):
        return "BUSINESS_EVIDENCE_GAP"
    if check_diagnostics:
        return "EXECUTION_INCOMPLETE"
    if result_status in STRONG_STATUSES and assessment_status == result_status:
        return result_status  # type: ignore[return-value]
    if (
        result_status == assessment_status == "NOT_FOUND"
        and assessment_gap == result_gap
        and assessment_gap
        and has_missing_fact
    ):
        return "BUSINESS_EVIDENCE_GAP"
    return "EXECUTION_INCOMPLETE"


def _combine_unresolved_outcomes(outcomes: list[OutcomeKind]) -> OutcomeKind:
    if "INTEGRITY_REJECTED" in outcomes:
        return "INTEGRITY_REJECTED"
    if "EXECUTION_INCOMPLETE" in outcomes:
        return "EXECUTION_INCOMPLETE"
    if outcomes and all(item == "BUSINESS_EVIDENCE_GAP" for item in outcomes):
        return "BUSINESS_EVIDENCE_GAP"
    return "EXECUTION_INCOMPLETE"


def _target_outcome(
    *,
    root_id: str,
    nodes: dict[str, dict[str, Any]],
    canonical_decision: dict[str, Any],
    canonical_node_results: dict[str, dict[str, Any]],
    check_outcomes: dict[str, OutcomeKind],
    global_integrity_rejected: bool,
) -> OutcomeKind:
    if global_integrity_rejected:
        return "INTEGRITY_REJECTED"
    if not root_id or not canonical_decision:
        return "EXECUTION_INCOMPLETE"
    root_result = _mapping(canonical_node_results.get(root_id))
    root_status = str(root_result.get("status") or "")
    if root_status != str(canonical_decision.get("status") or ""):
        return "EXECUTION_INCOMPLETE"
    projected = _project_outcome(root_id, nodes=nodes, check_outcomes=check_outcomes)
    if root_status in STRONG_STATUSES and projected == root_status:
        return root_status  # type: ignore[return-value]
    if root_status == "NOT_FOUND" and projected not in STRONG_STATUSES:
        return projected or "EXECUTION_INCOMPLETE"
    return "EXECUTION_INCOMPLETE"


def _project_outcome(
    root_id: str,
    *,
    nodes: dict[str, dict[str, Any]],
    check_outcomes: dict[str, OutcomeKind],
    selected: set[str] | None = None,
) -> OutcomeKind | None:
    memo: dict[str, OutcomeKind | None] = {}

    def project(node_id: str) -> OutcomeKind | None:
        if node_id in memo:
            return memo[node_id]
        node = _mapping(nodes.get(node_id))
        kind = str(node.get("kind") or "")
        if kind == "CHECK":
            result = (
                check_outcomes.get(node_id, "EXECUTION_INCOMPLETE")
                if selected is None or node_id in selected
                else None
            )
            memo[node_id] = result
            return result
        child_statuses = [
            status
            for child_id in _list(node.get("depends_on"))
            if (status := project(str(child_id))) is not None
        ]
        if not child_statuses:
            memo[node_id] = None
            return None
        decisive = "SUPPORTED" if kind == "ANY" else "CONTRADICTED"
        unanimous = "CONTRADICTED" if kind == "ANY" else "SUPPORTED"
        if decisive in child_statuses:
            result = decisive
        elif all(item == unanimous for item in child_statuses):
            result = unanimous
        else:
            result = _combine_unresolved_outcomes(
                [item for item in child_statuses if item not in STRONG_STATUSES]
            )
        memo[node_id] = result
        return result

    return project(root_id)


def _aggregate_milestone_outcome(
    check_ids: list[str],
    check_outcomes: dict[str, OutcomeKind],
    *,
    root_id: str,
    nodes: dict[str, dict[str, Any]],
) -> OutcomeKind:
    """Project selected facet CHECKs through the plan's real topology."""
    if not check_ids:
        return "EXECUTION_INCOMPLETE"
    return _project_outcome(
        root_id,
        nodes=nodes,
        check_outcomes=check_outcomes,
        selected=set(check_ids),
    ) or "EXECUTION_INCOMPLETE"


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
    return currencies == {expected}


def _claim_semantics_match_source_fact(
    fact: ExpectedFact,
    claim: dict[str, Any],
) -> bool:
    """Require a business role, while tolerating compact runtime predicates.

    Runtime Claims commonly use compact predicates such as ``has_total`` or
    ``has_amount``.  The latter is meaningful only together with a subject such
    as ``line item 1``.  We therefore first require the normal predicate match,
    then permit a narrow token-composition fallback across subject+predicate.
    Exact Oracle quote/source/locator/value/currency checks remain mandatory in
    the caller, so a weather predicate or a similarly-valued foreign Claim
    cannot pass merely by sharing a number.
    """
    if not fact.predicate_options:
        return True
    if _predicate_matches_options(claim.get("predicate"), fact.predicate_options):
        return True
    observed = _identifier_terms(
        f"{claim.get('subject', '')} {claim.get('predicate', '')}"
    ) - {"has", "have", "is", "invoice", "document"}
    if not observed:
        return False
    for option in fact.predicate_options:
        expected = _identifier_terms(option)
        if "inclusive" in expected:
            expected -= {"price", "treatment"}
        if not expected:
            continue
        # Multi-token roles may be composed from subject + predicate
        # (``line item`` + ``amount``).  For a one-token role, exact token
        # identity is still required; substrings never count.
        if expected.issubset(observed):
            return True
        predicate_terms = _identifier_terms(claim.get("predicate")) - {
            "has",
            "have",
            "is",
        }
        if predicate_terms and predicate_terms.issubset(expected):
            return True
    return False


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
    allow_subtracted_value: bool = False,
) -> bool:
    if fact.origin != "source" or not claim:
        return False
    if str(claim.get("confidence") or "medium").casefold() == "low":
        return False
    source_id = str(claim.get("source_id") or "")
    if fact.source_role and source_roles.get(source_id, "") != _normalized(fact.source_role):
        return False
    if not _claim_semantics_match_source_fact(fact, claim):
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
    atomic_numeric_alternative = bool(
        not quote_matches
        and expected_decimal is not None
        and actual_quote in expected_quote
        and len(
            re.findall(
                rf"(?<!\d){re.escape(actual_quote)}(?!\d)",
                expected_quote,
            )
        )
        == 1
        and _text_has_decimal(actual_quote, expected_decimal, fact.tolerance)
    )
    percentage_alternative = bool(
        not quote_matches
        and fact.kind == "decimal"
        and expected_decimal is not None
        and "%" in actual_quote
        and "%" in expected_quote
        and _text_has_decimal(actual_quote, expected_decimal, fact.tolerance)
        and _text_has_decimal(expected_quote, expected_decimal, fact.tolerance)
    )
    if not quote_matches and not percentage_alternative and not atomic_numeric_alternative:
        return False
    value = claim.get("value")
    text = value if isinstance(value, str) else _json_text(value)
    if not _claim_currency_matches(fact, claim, str(text)):
        return False
    if fact.kind == "text":
        expected_text = _normalized(fact.value)
        return expected_text in actual_quote
    expected = expected_decimal
    return bool(
        expected is not None
        and (
            _text_has_decimal(str(text), expected, fact.tolerance)
            or (
                allow_subtracted_value
                and _text_has_decimal(str(text), -expected, fact.tolerance)
            )
        )
        and (
            actual_quote == expected_quote
            or _text_has_decimal(actual_quote, expected, fact.tolerance)
        )
    )


def _source_fact_claim_candidates(
    facts: list[ExpectedFact],
    *,
    claims_by_id: dict[str, dict[str, Any]],
    grounded_claim_ids: set[str],
    source_roles: dict[str, str],
    source_content: dict[str, str],
    subtracted_claim_ids: set[str] | None = None,
) -> dict[str, set[str]]:
    subtracted_claim_ids = subtracted_claim_ids or set()
    return {
        fact.id: {
            claim_id
            for claim_id in sorted(grounded_claim_ids)
            if _claim_matches_source_fact(
                fact,
                claims_by_id.get(claim_id, {}),
                source_roles=source_roles,
                source_content=source_content,
                allow_subtracted_value=claim_id in subtracted_claim_ids,
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


def _fact_matches(
    fact: ExpectedFact,
    text: str,
    *,
    allow_subtracted_value: bool = False,
) -> bool:
    if fact.kind == "text":
        return _normalized(fact.value) in _normalized(text)
    try:
        expected = Decimal(fact.value)
    except InvalidOperation:
        return False
    currency = fact.currency.casefold().strip()
    if currency and currency not in text.casefold():
        return False
    return _text_has_decimal(text, expected, fact.tolerance) or (
        allow_subtracted_value
        and _text_has_decimal(text, -expected, fact.tolerance)
    )


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


_ORACLE_TO_WITNESS_OPERATION = {
    "multiply": "MULTIPLY",
    "sum": "SUM",
    "absolute_difference": "ABS_DIFF",
    "greater_than": "GREATER_THAN",
}


def _typed_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None or isinstance(value, float):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _typed_value_matches_fact(
    fact: ExpectedFact,
    value: Any,
    *,
    currency: Any,
) -> bool:
    expected = _decimal_value(fact)
    observed = _typed_decimal(value)
    if expected is None or observed is None or abs(observed - expected) > fact.tolerance:
        return False
    expected_currency = fact.currency.strip().upper()
    observed_currency = str(currency or "").strip().upper()
    return not expected_currency or observed_currency == expected_currency


def _accepted_witness_lineage(
    witness_ids: set[str],
    *,
    accepted_witness_ids: set[str],
    witnesses_by_id: dict[str, dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """Return accepted Witnesses and Claim leaves reachable from a frontier."""
    claims: set[str] = set()
    pending = list(witness_ids & accepted_witness_ids)
    visited: set[str] = set()
    while pending:
        witness_id = pending.pop()
        if witness_id in visited:
            continue
        visited.add(witness_id)
        for operand in _dict_items(
            _mapping(witnesses_by_id.get(witness_id)).get("operands")
        ):
            ref = _mapping(operand.get("ref"))
            ref_id = str(ref.get("ref_id") or "")
            if str(ref.get("kind") or "") == "CLAIM" and ref_id:
                claims.add(ref_id)
            elif str(ref.get("kind") or "") == "WITNESS" and ref_id in accepted_witness_ids:
                pending.append(ref_id)
    return visited, claims


def _upstream_check_closure(
    check_ids: set[str],
    *,
    nodes: dict[str, dict[str, Any]],
) -> set[str]:
    """Return CHECKs whose committed frontier is declared as upstream dataflow."""
    result = set(check_ids)
    pending = list(check_ids)
    while pending:
        check_id = pending.pop()
        for upstream_id in _list(_mapping(nodes.get(check_id)).get("upstream_check_ids")):
            upstream_id = str(upstream_id or "")
            if upstream_id and upstream_id not in result:
                result.add(upstream_id)
                pending.append(upstream_id)
    return result


def _operand_fact_candidates(
    operand: dict[str, Any],
    *,
    facts_by_id: dict[str, ExpectedFact],
    source_assignments: dict[str, str],
    witness_outputs: dict[str, set[str]],
    accepted_witness_ids: set[str],
    witnesses_by_id: dict[str, dict[str, Any]],
    fact_equivalences: dict[str, set[str]],
    visiting: frozenset[str] = frozenset(),
) -> set[str]:
    ref = _mapping(operand.get("ref"))
    kind = str(ref.get("kind") or "")
    ref_id = str(ref.get("ref_id") or "")
    candidates: set[str] = set()
    if kind == "CLAIM":
        candidates = {
            fact_id
            for fact_id, claim_id in source_assignments.items()
            if claim_id == ref_id
        }
    elif kind == "POLICY":
        candidates = {
            fact_id
            for fact_id, fact in facts_by_id.items()
            if fact.origin == "policy" and fact.policy_ref == ref_id
        }
    elif kind == "WITNESS" and ref_id in accepted_witness_ids:
        candidates = set(witness_outputs.get(ref_id, set()))
        # SUM(x) is an identity expression.  This matters for a single-line
        # subtotal that is used as an operand of an ABS_DIFF without requiring
        # the Oracle to prescribe that intermediate Witness.
        witness = _mapping(witnesses_by_id.get(ref_id))
        if ref_id not in visiting and str(witness.get("operation") or "") == "SUM":
            leaves = _flatten_associative_operands(
                witness,
                operation="SUM",
                witnesses_by_id=witnesses_by_id,
                accepted_witness_ids=accepted_witness_ids,
                visiting=set(visiting),
            )
            if len(leaves) == 1:
                candidates.update(
                    _operand_fact_candidates(
                        leaves[0],
                        facts_by_id=facts_by_id,
                        source_assignments=source_assignments,
                        witness_outputs=witness_outputs,
                        accepted_witness_ids=accepted_witness_ids,
                        witnesses_by_id=witnesses_by_id,
                        fact_equivalences=fact_equivalences,
                        visiting=visiting | {ref_id},
                    )
                )
    candidates = {
        equivalent
        for fact_id in candidates
        for equivalent in fact_equivalences.get(fact_id, {fact_id})
    }
    return {
        fact_id
        for fact_id in candidates
        if fact_id in facts_by_id
        and _typed_value_matches_fact(
            facts_by_id[fact_id],
            operand.get("value"),
            currency=operand.get("currency"),
        )
    }


def _flatten_associative_operands(
    witness: dict[str, Any],
    *,
    operation: str,
    witnesses_by_id: dict[str, dict[str, Any]],
    accepted_witness_ids: set[str],
    visiting: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Flatten only Kernel-admitted SUM/MULTIPLY parents of the same type."""
    seen = set(visiting or set())
    witness_id = str(witness.get("id") or "")
    if witness_id:
        if witness_id in seen:
            return []
        seen.add(witness_id)
    flattened: list[dict[str, Any]] = []
    for operand in _dict_items(witness.get("operands")):
        ref = _mapping(operand.get("ref"))
        parent_id = str(ref.get("ref_id") or "")
        parent = _mapping(witnesses_by_id.get(parent_id))
        if (
            str(ref.get("kind") or "") == "WITNESS"
            and parent_id in accepted_witness_ids
            and parent_id not in seen
            and str(parent.get("operation") or "") == operation
        ):
            flattened.extend(
                _flatten_associative_operands(
                    parent,
                    operation=operation,
                    witnesses_by_id=witnesses_by_id,
                    accepted_witness_ids=accepted_witness_ids,
                    visiting=seen,
                )
            )
        else:
            flattened.append(operand)
    return flattened


def _operands_match_relation_inputs(
    operands: list[dict[str, Any]],
    input_fact_ids: list[str],
    *,
    ordered: bool,
    facts_by_id: dict[str, ExpectedFact],
    source_assignments: dict[str, str],
    witness_outputs: dict[str, set[str]],
    accepted_witness_ids: set[str],
    witnesses_by_id: dict[str, dict[str, Any]],
    fact_equivalences: dict[str, set[str]],
) -> bool:
    if len(operands) != len(input_fact_ids):
        return False
    candidates = [
        _operand_fact_candidates(
            operand,
            facts_by_id=facts_by_id,
            source_assignments=source_assignments,
            witness_outputs=witness_outputs,
            accepted_witness_ids=accepted_witness_ids,
            witnesses_by_id=witnesses_by_id,
            fact_equivalences=fact_equivalences,
        )
        for operand in operands
    ]
    if ordered:
        return all(fact_id in candidates[index] for index, fact_id in enumerate(input_fact_ids))

    owner: dict[int, str] = {}

    def assign(fact_id: str, seen: set[int]) -> bool:
        for index, options in enumerate(candidates):
            if index in seen or fact_id not in options:
                continue
            seen.add(index)
            previous = owner.get(index)
            if previous is None or assign(previous, seen):
                owner[index] = fact_id
                return True
        return False

    return all(assign(fact_id, set()) for fact_id in input_fact_ids)


def _typed_witness_matches_relation(
    witness: dict[str, Any],
    relation: Any,
    *,
    milestone_facet_ref: str,
    explicit_facet: bool,
    source_assignments: dict[str, str],
    facts_by_id: dict[str, ExpectedFact],
    witness_outputs: dict[str, set[str]],
    accepted_witness_ids: set[str],
    witnesses_by_id: dict[str, dict[str, Any]],
    fact_equivalences: dict[str, set[str]],
) -> bool:
    operation = str(witness.get("operation") or "")
    expected_operation = _ORACLE_TO_WITNESS_OPERATION.get(relation.operation)
    sum_via_subtract = expected_operation == "SUM" and operation == "SUBTRACT"
    if operation != expected_operation and not sum_via_subtract:
        return False
    witness_facet = str(witness.get("facet_ref") or "")
    if (
        _semantic_normalized(witness_facet) != _semantic_normalized(milestone_facet_ref)
        if explicit_facet
        else not _facet_corresponds_to_milestone(witness_facet, milestone_facet_ref)
    ):
        return False
    operands = _dict_items(witness.get("operands"))
    if sum_via_subtract:
        if len(operands) != 2 or (right := _typed_decimal(operands[1].get("value"))) is None:
            return False
        operands = [operands[0], {**operands[1], "value": str(-right)}]
    operand_variants = [operands]
    if operation in {"SUM", "MULTIPLY"}:
        operand_variants.append(
            _flatten_associative_operands(
                witness,
                operation=operation,
                witnesses_by_id=witnesses_by_id,
                accepted_witness_ids=accepted_witness_ids,
            )
        )
    if not any(
        _operands_match_relation_inputs(
            candidate,
            list(relation.input_fact_ids),
            ordered=relation.operation == "greater_than",
            facts_by_id=facts_by_id,
            source_assignments=source_assignments,
            witness_outputs=witness_outputs,
            accepted_witness_ids=accepted_witness_ids,
            witnesses_by_id=witnesses_by_id,
            fact_equivalences=fact_equivalences,
        )
        for candidate in operand_variants
    ):
        return False
    if relation.operation == "greater_than":
        return isinstance(witness.get("result"), bool) and (
            witness.get("result") is relation.expected_boolean
        )
    if not relation.output_fact_id or relation.output_fact_id not in facts_by_id:
        return False
    output_fact = facts_by_id[relation.output_fact_id]
    if output_fact.origin == "source" and relation.output_fact_id not in source_assignments:
        return False
    return _typed_value_matches_fact(
        output_fact,
        witness.get("result"),
        currency=witness.get("currency"),
    )


def _merge_fact_equivalence(
    fact_ids: list[str],
    equivalences: dict[str, set[str]],
) -> None:
    merged = set(fact_ids)
    for fact_id in tuple(merged):
        merged.update(equivalences.get(fact_id, {fact_id}))
    for fact_id in merged:
        equivalences[fact_id] = set(merged)


def _witness_proves_within_tolerance(
    witness_id: str,
    *,
    witnesses_by_id: dict[str, dict[str, Any]],
    accepted_witness_ids: set[str],
    facts_by_id: dict[str, ExpectedFact],
) -> bool:
    witness = _mapping(witnesses_by_id.get(witness_id))
    result = _typed_decimal(witness.get("result"))
    if result is None:
        return False
    if result == 0:
        return True
    for candidate_id in accepted_witness_ids:
        candidate = _mapping(witnesses_by_id.get(candidate_id))
        operands = _dict_items(candidate.get("operands"))
        tolerance_ref = (
            str(_mapping(operands[1].get("ref")).get("ref_id") or "")
            if len(operands) == 2
            else ""
        )
        declared_tolerance = any(
            fact.origin == "policy" and fact.policy_ref == tolerance_ref
            for fact in facts_by_id.values()
        )
        if (
            str(candidate.get("operation") or "") == "GREATER_THAN"
            and candidate.get("result") is False
            and len(operands) == 2
            and _mapping(operands[0].get("ref")).get("kind") == "WITNESS"
            and str(_mapping(operands[0].get("ref")).get("ref_id") or "") == witness_id
            and _mapping(operands[1].get("ref")).get("kind") == "POLICY"
            and declared_tolerance
        ):
            return True
    return False


def _match_typed_relation_witnesses(
    relations: list[Any],
    *,
    relation_owner: dict[str, Any],
    milestone_check_ids: dict[str, set[str]],
    canonical_node_results: dict[str, dict[str, Any]],
    calculation_witnesses: list[dict[str, Any]],
    facts_by_id: dict[str, ExpectedFact],
    fact_claim_ids: dict[str, set[str]],
) -> tuple[dict[str, tuple[str, str]], dict[str, set[str]]]:
    """Match Oracle relations against the Kernel-admitted typed proof graph.

    The graph starts at grounded source Claims and policy refs.  Each matched
    CalculationWitness contributes its typed output fact, allowing later
    Witness refs to prove dependent relations.  Free-form assessment prose is
    intentionally absent from this function.
    """
    witnesses_by_id = {
        str(item.get("id") or ""): item
        for item in calculation_witnesses
        if item.get("id")
    }
    all_target_check_ids = {
        check_id for check_ids in milestone_check_ids.values() for check_id in check_ids
    }
    accepted_witness_ids = {
        str(witness_id)
        for check_id in all_target_check_ids
        for witness_id in _list(
            _mapping(canonical_node_results.get(check_id)).get("witness_ids")
        )
        if str(witness_id)
    }

    source_assignments_by_milestone: dict[str, dict[str, str]] = {}
    for milestone_id, check_ids in milestone_check_ids.items():
        milestone_witness_ids = {
            str(witness_id)
            for check_id in check_ids
            for witness_id in _list(
                _mapping(canonical_node_results.get(check_id)).get("witness_ids")
            )
            if str(witness_id)
        }
        _, accepted_claim_ids = _accepted_witness_lineage(
            milestone_witness_ids,
            accepted_witness_ids=accepted_witness_ids,
            witnesses_by_id=witnesses_by_id,
        )
        # A valid alternative path may use a source fact not named as an input
        # in this Oracle milestone (for example a printed subtotal substituted
        # for an independently proven line-derived subtotal).  It still has to
        # be an admitted Claim of a candidate CHECK and match a declared fact.
        source_fact_ids = [
            fact_id
            for fact_id in facts_by_id
            if fact_id in facts_by_id and facts_by_id[fact_id].origin == "source"
        ]
        source_assignments_by_milestone[milestone_id] = _unique_fact_assignments(
            source_fact_ids,
            fact_claim_ids=fact_claim_ids,
            allowed_claim_ids=accepted_claim_ids,
        )

    unresolved = {relation.id: relation for relation in relations}
    used_witness_ids: set[str] = set()
    witness_outputs: dict[str, set[str]] = {}
    fact_equivalences = {fact_id: {fact_id} for fact_id in facts_by_id}
    matched: dict[str, tuple[str, str]] = {}

    while unresolved:
        candidates: dict[str, list[tuple[str, str]]] = {}
        for relation_id, relation in unresolved.items():
            milestone = relation_owner.get(relation_id)
            if milestone is None:
                continue
            milestone_id = str(milestone.id)
            milestone_facet_ref = str(
                getattr(milestone, "facet_ref", "") or milestone_id
            )
            explicit_facet = bool(str(getattr(milestone, "facet_ref", "")))
            source_assignments = source_assignments_by_milestone.get(milestone_id, {})
            relation_candidates: list[tuple[str, str]] = []
            for check_id in sorted(milestone_check_ids.get(milestone_id, set())):
                admitted = {
                    str(item)
                    for item in _list(
                        _mapping(canonical_node_results.get(check_id)).get("witness_ids")
                    )
                    if str(item)
                }
                for witness_id in sorted(admitted - used_witness_ids):
                    witness = witnesses_by_id.get(witness_id, {})
                    if str(witness.get("check_id") or "") != check_id:
                        continue
                    if _typed_witness_matches_relation(
                        witness,
                        relation,
                        milestone_facet_ref=milestone_facet_ref,
                        explicit_facet=explicit_facet,
                        source_assignments=source_assignments,
                        facts_by_id=facts_by_id,
                        witness_outputs=witness_outputs,
                        accepted_witness_ids=accepted_witness_ids,
                        witnesses_by_id=witnesses_by_id,
                        fact_equivalences=fact_equivalences,
                    ):
                        relation_candidates.append((check_id, witness_id))
            if relation_candidates:
                candidates[relation_id] = relation_candidates
        if not candidates:
            break

        owner_by_witness: dict[str, str] = {}
        layer_matches: dict[str, tuple[str, str]] = {}

        def assign(relation_id: str, seen: set[str]) -> bool:
            for check_id, witness_id in candidates.get(relation_id, []):
                if witness_id in seen:
                    continue
                seen.add(witness_id)
                previous = owner_by_witness.get(witness_id)
                if previous is None or assign(previous, seen):
                    owner_by_witness[witness_id] = relation_id
                    layer_matches[relation_id] = (check_id, witness_id)
                    return True
            return False

        for relation_id in sorted(candidates, key=lambda item: (len(candidates[item]), item)):
            assign(relation_id, set())
        if not layer_matches:
            break
        for relation_id, match in layer_matches.items():
            relation = unresolved.pop(relation_id)
            matched[relation_id] = match
            witness_id = match[1]
            used_witness_ids.add(witness_id)
            if relation.output_fact_id:
                for _, candidate_id in candidates.get(relation_id, []):
                    witness_outputs.setdefault(candidate_id, set()).add(
                        relation.output_fact_id
                    )
            if (
                relation.operation == "absolute_difference"
                and _witness_proves_within_tolerance(
                    witness_id,
                    witnesses_by_id=witnesses_by_id,
                    accepted_witness_ids=accepted_witness_ids,
                    facts_by_id=facts_by_id,
                )
            ):
                _merge_fact_equivalence(
                    list(relation.input_fact_ids),
                    fact_equivalences,
                )

    return matched, witness_outputs


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
        status = str(decision.get("status") or "")
        if status not in {"SUPPORTED", "CONTRADICTED", "NOT_FOUND"}:
            violations.append(
                {
                    "requirement_id": requirement_id,
                    "error": "missing_decision_status" if not status else "invalid_decision_status",
                    "observed_status": status or "缺失",
                }
            )
            continue
        leaf_field = {
            "SUPPORTED": "supporting_check_ids",
            "CONTRADICTED": "contradicting_check_ids",
            "NOT_FOUND": "unresolved_check_ids",
        }[status]
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
        }[status]
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


_RAW_PDF_APPENDIX_HEADING = "原始材料附录"


def _canonical_pdf_body(text: str) -> str:
    """Exclude the renderer's explicit raw-material appendix boundary."""
    match = re.search(
        rf"(?m)^\s*(?:#+\s*)?{re.escape(_RAW_PDF_APPENDIX_HEADING)}\s*$",
        text,
    )
    return text[: match.start()].rstrip() if match else text


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


def _scoped_missing_meaning_matches(text: str, meaning: Any) -> bool:
    """Require subject, missing input and uncertainty in one sentence/line."""
    scopes = [
        item.strip()
        for item in re.split(
            r"(?:\r?\n)+|[。！？；;]|(?<!\d)[.!?](?!\d)",
            text,
        )
        if item.strip()
    ]
    return any(_affirmative_groups_match(scope, meaning) for scope in scopes)


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


_CONFLICT_SIGNAL = re.compile(
    r"不一致|不符|冲突|错误|异常|inconsisten(?:t|cy)|conflict|incorrect|"
    r"(?:does|do|did)\s+not\s+match|not\s+equal",
    re.IGNORECASE,
)
_CONFLICT_DENIAL = re.compile(
    r"(?:未发现|没有发现|不存在|没有|并无|并未发现).{0,24}$|"
    r"\b(?:no|never|without)\b.{0,24}$|\bnot\b.{0,16}$",
    re.IGNORECASE,
)


def _affirmative_conflict_signal(text: str) -> bool:
    for match in _CONFLICT_SIGNAL.finditer(text):
        signal = match.group(0).casefold()
        if "not match" in signal or "not equal" in signal:
            return True
        prefix = text[max(0, match.start() - 32) : match.start()]
        if not _CONFLICT_DENIAL.search(prefix):
            return True
    return False


def _conflict_proposition_matches(
    text: str,
    facts_by_id: dict[str, ExpectedFact],
) -> bool:
    required = [
        facts_by_id.get("printed_total"),
        facts_by_id.get("recomputed_total"),
        facts_by_id.get("total_difference"),
    ]
    if any(fact is None for fact in required):
        return False
    propositions = [
        item.strip()
        for item in re.split(
            r"(?:\r?\n)+|[。！？；;]|(?<!\d)[.!?](?!\d)",
            text,
        )
        if item.strip()
    ]
    return any(
        _affirmative_conflict_signal(proposition)
        and all(_fact_matches(fact, proposition) for fact in required if fact is not None)
        for proposition in propositions
    )


def _required_business_meanings_match(
    text: str,
    *,
    oracle: BusinessEvalOracle,
    facts_by_id: dict[str, ExpectedFact],
) -> bool:
    groups = oracle.communication.required_meanings
    if _affirmative_document_groups_match(text, groups):
        return True
    if (
        oracle.requirement.decision_status == "CONTRADICTED"
        and groups
        and _conflict_proposition_matches(text, facts_by_id)
    ):
        return _affirmative_document_groups_match(text, groups[1:])
    return False


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
                    r"(?:\b(?:not|no|never|without)\s+|"
                    r"(?:未发现|没有发现|不存在|并无)\s*|"
                    r"(?:不|未|无|并非|没有|并不)(?:完全|一定|能|是)?\s*)$",
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
    for clause in _boundary_propositions(text):
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


def _boundary_propositions(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(
            r"(?:\r?\n)+|(?<=[.!?。！？；;，])|\b(?:but|however)\b|但(?:是|而)?",
            text,
            flags=re.IGNORECASE,
        )
        if item.strip()
    ]


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


def _score_framework_protocol(
    oracle: FrameworkOracle | None,
    snapshot: EvalSnapshot,
) -> tuple[Decimal, bool, list[FrameworkCheck]]:
    if oracle is None:
        return Decimal("100"), True, []

    protocol = _framework_observation(snapshot)
    calls = protocol["calls"]
    successful = [item for item in calls if not item["error"]]
    successful_names = [str(item["name"]) for item in successful]
    all_names = [str(item["name"]) for item in calls]
    role_calls = protocol["roles"]
    successful_role_names = [
        str(item["name"]) for item in role_calls if not item["error"]
    ]
    approved_names = protocol["approved_names"]
    definitions: list[tuple[str, bool, Any, Any, str]] = []

    for requirement in oracle.required_tools:
        name = _canonical_tool_name(requirement.name)
        observed = sum(1 for item in successful_names if item == name)
        definitions.append(
            (
                f"framework.required_tool.{name}",
                observed >= requirement.min_calls,
                {"tool": name, "min_successful_calls": requirement.min_calls},
                {
                    "successful_calls": observed,
                    "total_calls": sum(1 for item in all_names if item == name),
                },
                "必需工具按成功调用次数核对；失败尝试不能满足最低调用要求。",
            )
        )

    for requirement in oracle.required_roles:
        name = _canonical_tool_name(requirement.name)
        observed = sum(1 for item in successful_role_names if item == name)
        definitions.append(
            (
                f"framework.required_role.{name}",
                observed >= requirement.min_calls,
                {"role": name, "min_successful_calls": requirement.min_calls},
                {"successful_calls": observed},
                "必需角色按成功调用次数核对；不绑定角色内部实现路径。",
            )
        )

    for raw_name in oracle.required_approved_tools:
        name = _canonical_tool_name(raw_name)
        definitions.append(
            (
                f"framework.required_approval.{name}",
                name in approved_names,
                {"approved_tool": name},
                {"approved": name in approved_names},
                "有副作用工具必须存在显式批准记录，工具调用本身不能替代批准。",
            )
        )

    for raw_name in oracle.forbidden_tools:
        name = _canonical_tool_name(raw_name)
        observed = sum(1 for item in all_names if item == name)
        definitions.append(
            (
                f"framework.forbidden_tool.{name}",
                observed == 0,
                {"tool": name, "max_calls": 0},
                {"calls": observed},
                "禁用工具一经尝试即失败，不以调用是否成功为转移。",
            )
        )

    if oracle.max_tool_errors is not None:
        errors = [item for item in calls if item["error"]]
        definitions.append(
            (
                "framework.max_tool_errors",
                len(errors) <= oracle.max_tool_errors,
                {"max_tool_errors": oracle.max_tool_errors},
                {
                    "tool_errors": len(errors),
                    "tools": [item["name"] for item in errors],
                },
                "工具拒绝、失败以及只有开始事件而没有完成事件都计入错误预算。",
            )
        )

    if oracle.max_total_calls is not None:
        definitions.append(
            (
                "framework.max_total_calls",
                len(calls) <= oracle.max_total_calls,
                {"max_total_calls": oracle.max_total_calls},
                {"total_calls": len(calls), "tools": all_names},
                "总调用预算只约束工具尝试数量，不限制未被禁止且仍在预算内的辅助工具。",
            )
        )

    if oracle.ordered_milestones:
        expected = [
            [_canonical_milestone_token(item) for item in alternatives]
            for alternatives in oracle.ordered_milestones
        ]
        successful_tokens = protocol["successful_sequence"]
        orderable = bool(protocol["orderable"])
        matched: list[dict[str, Any]] = []
        cursor = 0
        for alternatives in expected:
            match_index = next(
                (
                    index
                    for index in range(cursor, len(successful_tokens))
                    if successful_tokens[index] in alternatives
                ),
                None,
            )
            if match_index is None:
                break
            matched.append(
                {"milestone": successful_tokens[match_index], "successful_index": match_index}
            )
            cursor = match_index + 1
        definitions.append(
            (
                "framework.ordered_milestones",
                orderable and len(matched) == len(expected),
                expected,
                {
                    "orderable": orderable,
                    "matched": matched,
                    "successful_sequence": successful_tokens,
                },
                (
                    "只要求基本工具/角色里程碑按顺序出现；中间允许插入其他无害步骤。"
                    "跨 events/trace 无时间戳时无法证明顺序，必须 fail-closed。"
                ),
            )
        )

    weights = _split_points(Decimal("100"), len(definitions))
    checks = [
        FrameworkCheck(
            id=definition[0],
            points=points,
            earned=points if definition[1] else Decimal("0"),
            passed=definition[1],
            expected=definition[2],
            observed=definition[3],
            detail=definition[4],
        )
        for definition, points in zip(definitions, weights)
    ]
    score = sum((item.earned for item in checks), Decimal("0"))
    return score, all(item.passed for item in checks), checks


def _framework_observation(snapshot: EvalSnapshot) -> dict[str, Any]:
    """Normalize the protocol once: events are canonical, trace is fallback."""

    calls: list[dict[str, Any]] = []
    roles: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    if snapshot.events:
        for event_index, event in enumerate(snapshot.events):
            if not isinstance(event, dict):
                continue
            kind = str(event.get("kind") or event.get("type") or "").strip().casefold()
            if kind in {
                "tool_started", "provider_tool_started", "tool_called",
                "tool_finished", "provider_tool_finished", "tool_output",
                "tool_error", "provider_tool_error",
                "tool_call", "tool", "manager_tool", "manager_tool_call",
            }:
                name = _event_tool_name(event)
                if not name:
                    continue
                if kind in {"tool_started", "provider_tool_started", "tool_called"}:
                    call = _protocol_step("tool", name, error=True, event_index=event_index)
                    calls.append(call)
                    steps.append(call)
                    pending.append(call)
                elif kind in {"tool_finished", "provider_tool_finished", "tool_output"}:
                    call = next((item for item in reversed(pending) if item["name"] == name), None)
                    if call is None:
                        call = _protocol_step(
                            "tool", name, error=_tool_record_is_error(event), event_index=event_index
                        )
                        calls.append(call)
                        steps.append(call)
                    else:
                        call["error"] = _tool_record_is_error(event)
                        pending.remove(call)
                elif kind in {"tool_error", "provider_tool_error"}:
                    call = next((item for item in reversed(pending) if item["name"] == name), None)
                    if call is None:
                        call = _protocol_step("tool", name, error=True, event_index=event_index)
                        calls.append(call)
                        steps.append(call)
                    else:
                        call["error"] = True
                        pending.remove(call)
                else:
                    call = _protocol_step(
                        "tool", name, error=_tool_record_is_error(event), event_index=event_index
                    )
                    calls.append(call)
                    steps.append(call)
                continue
            if kind in {"role_call", "specialist_call", "agent_call"}:
                name = _event_role_name(event)
                if name:
                    role = _protocol_step(
                        "role", name, error=_tool_record_is_error(event), event_index=event_index
                    )
                    roles.append(role)
                    steps.append(role)
                continue
            if kind == "approval_decision":
                payload = _mapping(event.get("payload"))
                name = _canonical_tool_name(payload.get("tool") or event.get("tool"))
                if name and bool(payload.get("approved")):
                    steps.append(_protocol_step("approval", name, event_index=event_index))
        orderable = True
    else:
        groups: set[str] = set()
        for keys, step_kind in (
            (("tool_calls", "manager_tool_calls"), "tool"),
            (("provider_tool_calls", "sandbox_tool_calls"), "tool"),
            (("role_calls",), "role"),
        ):
            key = next((item for item in keys if _dict_items(snapshot.trace.get(item))), "")
            if not key:
                continue
            groups.add(key)
            for trace_index, row in enumerate(_dict_items(snapshot.trace.get(key))):
                name = _event_tool_name(row) if step_kind == "tool" else _event_role_name(row)
                if not name:
                    continue
                step = _protocol_step(
                    step_kind,
                    name,
                    error=_tool_record_is_error(row),
                    ts=_tool_record_timestamp(row),
                    trace_group=key,
                    trace_index=trace_index,
                )
                (calls if step_kind == "tool" else roles).append(step)
                steps.append(step)
        if steps and all(item["ts"] for item in steps):
            steps.sort(key=lambda item: item["ts"])
            orderable = True
        elif len(groups) == 1:
            steps.sort(key=lambda item: int(item["trace_index"]))
            orderable = bool(steps)
        else:
            orderable = False

    return {
        "calls": calls,
        "roles": roles,
        "approved_names": {_canonical_tool_name(item) for item in _approved_tools(snapshot)},
        "orderable": orderable,
        "successful_sequence": [item["token"] for item in steps if not item["error"]],
    }


def _protocol_step(
    kind: str,
    name: str,
    *,
    error: bool = False,
    event_index: int | None = None,
    ts: str = "",
    trace_group: str = "",
    trace_index: int | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "token": f"{kind}:{name}",
        "error": error,
        "event_index": event_index,
        "ts": ts,
        "trace_group": trace_group,
        "trace_index": trace_index,
    }


def _event_tool_name(value: dict[str, Any]) -> str:
    payload = _mapping(value.get("payload"))
    candidates: list[Any] = [
        payload.get("tool"),
        value.get("tool"),
        _mapping(payload.get("function")).get("name"),
        _mapping(value.get("function")).get("name"),
        payload.get("tool_name"),
        value.get("tool_name"),
        payload.get("name"),
        value.get("name"),
    ]
    for candidate in candidates:
        name = _canonical_tool_name(candidate)
        if name and name not in {
            "tool_call",
            "tool_started",
            "tool_finished",
            "tool_error",
            "executor",
        }:
            return name
    return ""


def _canonical_tool_name(value: Any) -> str:
    name = str(value or "").strip().casefold()
    if not name:
        return ""
    name = re.sub(r"^(?:call_tool|tool|function)\s*[:/]\s*", "", name)
    if "__" in name:
        name = name.rsplit("__", 1)[-1]
    if any(separator in name for separator in (".", "/", ":")):
        name = re.split(r"[./:]", name)[-1]
    return re.sub(r"[^a-z0-9_]+", "_", name).strip("_")


def _tool_record_is_error(value: dict[str, Any]) -> bool:
    payload = _mapping(value.get("payload"))
    error = payload.get("error", value.get("error"))
    if error not in (None, "", False, {}, []):
        return True
    result = _mapping(payload.get("result"))
    result_error = result.get("error")
    if result_error not in (None, "", False, {}, []):
        return True
    status = str(
        payload.get("status") or result.get("status") or value.get("status") or ""
    ).casefold()
    if status in {"error", "failed", "rejected", "blocked", "cancelled", "canceled"}:
        return True
    return bool(payload.get("hook_code") or value.get("hook_code"))


def _tool_record_timestamp(value: dict[str, Any]) -> str:
    payload = _mapping(value.get("payload"))
    return str(value.get("ts") or payload.get("ts") or "")


def _event_role_name(value: dict[str, Any]) -> str:
    payload = _mapping(value.get("payload"))
    for candidate in (
        payload.get("role"),
        value.get("role"),
        payload.get("agent"),
        value.get("agent"),
        payload.get("name"),
        value.get("name"),
    ):
        name = _canonical_tool_name(candidate)
        if name and name not in {"role_call", "specialist_call", "agent_call"}:
            return name
    return ""


def _canonical_milestone_token(value: Any) -> str:
    raw = str(value or "").strip().casefold()
    if ":" in raw:
        kind, name = raw.split(":", 1)
        if kind in {"tool", "role", "approval"}:
            return f"{kind}:{_canonical_tool_name(name)}"
    return f"tool:{_canonical_tool_name(raw)}"


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
    tool_calls = _framework_observation(snapshot)["calls"]
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
        "tool_calls": len(tool_calls),
        "tool_error_calls": sum(1 for item in tool_calls if item["error"]),
        "tool_names": [item["name"] for item in tool_calls],
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
