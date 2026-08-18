from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel

from app.domain.invoice_contracts import build_requirement_contracts
from app.domain.invoice_requirements import POLICY_PATH, REQUIREMENT_PACK
from app.domain.semantic_graph import (
    ClaimSelector,
    NodeSpec,
    SemanticGraphSpec,
    compile_proof_graph,
    lower_case_evidence_ir,
)
from app.state.schemas import CompiledProof, EvidenceItem, RequirementContract, VerificationRecord


class InvoiceProofPolicy(BaseModel):
    policy_version: str
    compiler_version: str
    amount_tolerance_percent: Decimal
    allowed_amount_basis_by_subject: dict[str, list[str]]
    comparable_tax_bases: list[str]
    comparable_coverages: list[str]
    amount_match_requirement: str
    semantic_judgment_min_confidence: Literal["low", "medium", "high"] = "high"


def load_invoice_proof_policy(path: Path = POLICY_PATH) -> InvoiceProofPolicy:
    return InvoiceProofPolicy.model_validate_json(path.read_text(encoding="utf-8"))


def contract_proof_program(
    contract: RequirementContract,
    *,
    unresolved_policy_keys: set[str] | None = None,
    policy_values: Mapping[str, Any] | None = None,
) -> SemanticGraphSpec:
    """Small generic view for policy contracts that do not need a specialized kernel."""
    prefix = re.sub(r"[^A-Z0-9]+", "_", contract.requirement_id.upper()).strip("_")
    missing_policy = unresolved_policy_keys or set()
    kernel_policy = policy_values or {}
    min_confidence = str(kernel_policy.get("semantic_judgment_min_confidence") or "high")
    if contract.proof_template == "evidence_support":
        root_id = f"REQ_{prefix}"
        nodes = (
            NodeSpec(
                root_id,
                contract.requirement_id,
                "evidence_support",
                params={"role": contract.requirement_id},
                missing_premise=f"trusted accepted evidence supporting {contract.requirement_id}",
                obligation_id=f"OBL_{prefix}_SOURCE",
            ),
        )
    else:
        selectors = tuple(
            ClaimSelector(item.subject, item.predicate)
            for item in contract.inputs
            if item.hole_kind in {"claim", "relation"}
        )
        source_nodes = tuple(
            NodeSpec(
                f"SRC_{prefix}_{re.sub(r'[^A-Z0-9]+', '_', role.upper()).strip('_')}",
                contract.requirement_id,
                "evidence_support",
                params={"role": role},
                missing_premise=f"trusted accepted evidence supporting {role}",
                obligation_id=f"OBL_{prefix}_{re.sub(r'[^A-Z0-9]+', '_', role.upper()).strip('_')}_SOURCE",
            )
            for role in contract.evidence_roles
        )
        policy_nodes = tuple(
            NodeSpec(
                f"POL_{prefix}_{re.sub(r'[^A-Z0-9]+', '_', key.upper()).strip('_')}",
                contract.requirement_id,
                "policy_present",
                params={"configured": key not in missing_policy, "policy_key": key},
                missing_premise=f"configured enterprise policy value {key}",
                obligation_id=f"OBL_{prefix}_{re.sub(r'[^A-Z0-9]+', '_', key.upper()).strip('_')}_POLICY",
            )
            for key in contract.policy_inputs
        )
        target = next(
            (item.predicate for item in contract.inputs if item.hole_kind == "judgment"),
            contract.target_predicate,
        )
        if contract.proof_template == "reconciliation":
            nodes, root_id = _reconciliation_contract_nodes(
                contract,
                prefix,
                source_nodes,
                policy_nodes,
                target,
                kernel_policy,
            )
        elif contract.proof_template == "entity_lifecycle":
            nodes, root_id = _lifecycle_contract_nodes(
                contract,
                prefix,
                source_nodes,
                policy_nodes,
                target,
                min_confidence=min_confidence,
            )
        else:
            root_id = f"JDG_{prefix}"
            nodes = (
                *source_nodes,
                *policy_nodes,
                NodeSpec(
                    root_id,
                    contract.requirement_id,
                    "llm_judgment",
                    selectors=selectors,
                    depends_on=tuple(item.id for item in (*source_nodes, *policy_nodes)),
                    params={"min_confidence": min_confidence},
                    executor="llm",
                    judgment_id=root_id,
                    target_predicate=target,
                    missing_premise=f"source-grounded semantic judgment for {target}",
                    obligation_id=f"OBL_{prefix}_JUDGMENT",
                ),
            )
    return SemanticGraphSpec(
        id=f"contract_view:{contract.requirement_id}",
        version=contract.version,
        requirement_id=contract.requirement_id,
        claim_bindings=(),
        nodes=nodes,
        root_id=root_id,
    )


def _reconciliation_contract_nodes(
    contract: RequirementContract,
    prefix: str,
    source_nodes: tuple[NodeSpec, ...],
    policy_nodes: tuple[NodeSpec, ...],
    target: str,
    policy_values: Mapping[str, Any],
) -> tuple[tuple[NodeSpec, ...], str]:
    typed_inputs = [item for item in contract.inputs if item.hole_kind in {"claim", "relation"}]
    selectors = [ClaimSelector(item.subject, item.predicate, required=item.required) for item in typed_inputs]
    required_rows = [(item, selector) for item, selector in zip(typed_inputs, selectors) if item.required]
    presence = tuple(
        NodeSpec(
            f"CHK_{prefix}_INPUT_{index}",
            contract.requirement_id,
            "present",
            (selector,),
            missing_premise=f"source-grounded {item.subject}.{item.predicate}",
            obligation_id=f"OBL_{prefix}_INPUT_{index}",
        )
        for index, (item, selector) in enumerate(required_rows, start=1)
    )
    numeric = [
        selector
        for item, selector in zip(typed_inputs, selectors)
        if item.value_type in {"decimal", "integer"}
    ]
    attribute_presence: list[NodeSpec] = []
    attribute_checks: list[NodeSpec] = []
    for attribute in ("unit", "currency", "basis", "tax_basis", "coverage"):
        selected = [
            selector
            for item, selector in zip(typed_inputs, selectors)
            if attribute in item.required_attributes
        ]
        if not selected:
            continue
        present = NodeSpec(
            f"CHK_{prefix}_{attribute.upper()}_PRESENT",
            contract.requirement_id,
            "attribute_present",
            tuple(selected),
            tuple(node.id for node in presence),
            attribute,
            missing_premise=f"source-grounded {attribute} for compared values",
            obligation_id=f"OBL_{prefix}_{attribute.upper()}",
        )
        attribute_presence.append(present)
        if attribute == "coverage":
            allowed_coverages = tuple(policy_values.get("comparable_coverages") or ("full",))
            attribute_checks.append(NodeSpec(
                f"CHK_{prefix}_COVERAGE_COMPATIBLE",
                contract.requirement_id,
                "allowed",
                tuple(selected),
                (present.id,),
                attribute,
                {"allowed": allowed_coverages, "on_disallowed": "INCOMPLETE"},
                missing_premise="full and comparable source coverage",
                obligation_id=f"OBL_{prefix}_COVERAGE",
            ))
        elif attribute == "basis" and contract.capability == "amount_reconciliation":
            attribute_checks.append(NodeSpec(
                f"CHK_{prefix}_BASIS_ALLOWED",
                contract.requirement_id,
                "allowed_by_subject",
                tuple(selected),
                (present.id,),
                attribute,
                {
                    "allowed_by_subject": policy_values.get("allowed_amount_basis_by_subject") or {},
                    "on_disallowed": "INCOMPLETE",
                },
                missing_premise="policy-compatible amount basis for each source role",
                obligation_id=f"OBL_{prefix}_BASIS",
            ))
        elif attribute in {"unit", "currency", "tax_basis"} and len(selected) > 1:
            same = NodeSpec(
                f"CHK_{prefix}_{attribute.upper()}_COMPATIBLE",
                contract.requirement_id,
                "same",
                tuple(selected),
                (present.id,),
                attribute,
                {"on_mismatch": "INCOMPLETE"},
                missing_premise=f"comparable {attribute} across source values",
                obligation_id=f"OBL_{prefix}_{attribute.upper()}_COMPATIBLE",
            )
            attribute_checks.append(same)
            if attribute == "tax_basis":
                attribute_checks.append(NodeSpec(
                    f"CHK_{prefix}_TAX_BASIS_ALLOWED",
                    contract.requirement_id,
                    "allowed",
                    tuple(selected),
                    (same.id,),
                    attribute,
                    {
                        "allowed": tuple(policy_values.get("comparable_tax_bases") or ()),
                        "on_disallowed": "INCOMPLETE",
                    },
                    missing_premise="policy-compatible tax basis",
                    obligation_id=f"OBL_{prefix}_TAX_BASIS_ALLOWED",
                ))
    gates = (*source_nodes, *policy_nodes, *presence, *attribute_presence, *attribute_checks)
    judgment_id = f"JDG_{prefix}"
    judgment = NodeSpec(
        judgment_id,
        contract.requirement_id,
        "llm_judgment",
        tuple(selectors),
        tuple(node.id for node in gates),
        params={"min_confidence": str(policy_values.get("semantic_judgment_min_confidence") or "high")},
        executor="llm",
        judgment_id=judgment_id,
        target_predicate=target,
        missing_premise=f"source-grounded comparability judgment for {target}",
        obligation_id=f"OBL_{prefix}_JUDGMENT",
    )
    numeric_check: tuple[NodeSpec, ...] = ()
    if len(numeric) > 1:
        threshold = Decimal("0")
        for key in contract.policy_inputs:
            if "tolerance" not in key:
                continue
            try:
                threshold = Decimal(str(policy_values[key]))
                break
            except (KeyError, ValueError, ArithmeticError):
                continue
        numeric_check = (NodeSpec(
            f"CHK_{prefix}_WITHIN_TOLERANCE",
            contract.requirement_id,
            "within_percent",
            tuple(numeric),
            (judgment.id,),
            params={"threshold_percent": threshold, "pairs": tuple((0, index) for index in range(1, len(numeric)))},
            missing_premise="configured tolerance and comparable numeric source values",
            obligation_id=f"OBL_{prefix}_TOLERANCE",
        ),)
    root_id = f"REQ_{prefix}"
    root = NodeSpec(
        root_id,
        contract.requirement_id,
        "all",
        depends_on=tuple(node.id for node in (*gates, judgment, *numeric_check)),
    )
    return (*gates, judgment, *numeric_check, root), root_id


def _lifecycle_contract_nodes(
    contract: RequirementContract,
    prefix: str,
    source_nodes: tuple[NodeSpec, ...],
    policy_nodes: tuple[NodeSpec, ...],
    target: str,
    *,
    min_confidence: str,
) -> tuple[tuple[NodeSpec, ...], str]:
    typed_inputs = [item for item in contract.inputs if item.hole_kind in {"claim", "relation"}]
    selectors = [ClaimSelector(item.subject, item.predicate, required=item.required) for item in typed_inputs]
    presence = tuple(
        NodeSpec(
            f"CHK_{prefix}_INPUT_{index}",
            contract.requirement_id,
            "present",
            (selector,),
            missing_premise=f"source-grounded {item.subject}.{item.predicate}",
            obligation_id=f"OBL_{prefix}_INPUT_{index}",
        )
        for index, (item, selector) in enumerate(zip(typed_inputs, selectors), start=1)
        if item.required
    )
    grouped = [
        (item.subject, item.predicate)
        for item in typed_inputs
        if item.binding_mode == "per_entity"
    ]
    grouped_by_subject: dict[str, list[tuple[str, str]]] = {}
    for subject, predicate in grouped:
        grouped_by_subject.setdefault(subject, []).append((subject, predicate))
    branch = next(
        ((item.subject, item.predicate) for item in typed_inputs if item.binding_mode == "global" and item.predicate in {"result", "search_result"}),
        (typed_inputs[0].subject, typed_inputs[0].predicate) if typed_inputs else ("", ""),
    )
    judgment_id = f"JDG_{prefix}"
    judgment = NodeSpec(
        judgment_id,
        contract.requirement_id,
        "llm_judgment",
        tuple(selectors),
        tuple(node.id for node in (*source_nodes, *presence)),
        params={
            "min_confidence": min_confidence,
            "grouped_contract": {
                "branch_selector": branch,
                "group_selectors": tuple(grouped),
                "complete_selector_groups": tuple(tuple(items) for items in grouped_by_subject.values()),
                "required_group_selectors": tuple(
                    (item.subject, item.predicate)
                    for item in typed_inputs
                    if item.binding_mode == "per_entity" and item.group_required
                ),
                "empty_values": ("none", "no_candidate", "no_hold", "clear"),
                "grouped_values": ("candidate_found", "hold_found", "active_hold_found", "records_found"),
            },
        },
        executor="llm",
        judgment_id=judgment_id,
        target_predicate=target,
        missing_premise=f"complete source-grounded lifecycle judgments for {target}",
        obligation_id=f"OBL_{prefix}_JUDGMENT",
    )
    root_id = f"REQ_{prefix}"
    root = NodeSpec(
        root_id,
        contract.requirement_id,
        "all",
        depends_on=tuple(node.id for node in (*policy_nodes, judgment)),
    )
    return (*source_nodes, *policy_nodes, *presence, judgment, root), root_id


def compile_evidence_proof(
    evidence_items: list[EvidenceItem],
    verification_records: list[VerificationRecord] | None = None,
    policy: InvoiceProofPolicy | None = None,
    *,
    active_requirement_ids: set[str],
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
    contracts: list[RequirementContract] | None = None,
    requirement_pack: Mapping[str, Any] | None = None,
) -> CompiledProof:
    pack = requirement_pack or REQUIREMENT_PACK
    policy = policy or InvoiceProofPolicy.model_validate(pack)
    kernel_policy = {**pack, **policy.model_dump(mode="python")}
    built_contracts, initial_holes = build_requirement_contracts(active_requirement_ids, pack=pack)
    contracts = contracts or built_contracts
    unresolved_policy = {hole.policy_key for hole in initial_holes if hole.kind == "policy"}
    programs = [
        contract_proof_program(
            contract,
            unresolved_policy_keys=unresolved_policy,
            policy_values=kernel_policy,
        )
        for contract in contracts
    ]
    return _compile_programs(
        programs,
        evidence_items,
        verification_records,
        policy,
        active_requirement_ids=active_requirement_ids,
        contracts=contracts,
        requirement_pack=pack,
        trusted_sources=trusted_sources,
    )


def _compile_programs(
    programs: list[SemanticGraphSpec],
    evidence_items: list[EvidenceItem],
    verification_records: list[VerificationRecord] | None,
    policy: InvoiceProofPolicy,
    *,
    active_requirement_ids: set[str] | None = None,
    contracts: list[RequirementContract] | None = None,
    requirement_pack: Mapping[str, Any] | None = None,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> CompiledProof:
    active_ids = active_requirement_ids or {
        requirement_id
        for program in programs
        for requirement_id in (*program.activation_requirements, program.requirement_id)
    }
    pack = requirement_pack or REQUIREMENT_PACK
    if contracts is None:
        contracts, _ = build_requirement_contracts(active_ids, pack=pack)
    evidence_ir, lowering_diagnostics = lower_case_evidence_ir(
        programs,
        evidence_items,
        compiler_version=policy.compiler_version,
        trusted_sources=trusted_sources,
        contracts=contracts,
    )
    results = [
        compile_proof_graph(
            program,
            evidence_items,
            policy_version=policy.policy_version,
            compiler_version=policy.compiler_version,
            verification_records=verification_records,
            trusted_sources=trusted_sources,
            evidence_ir=evidence_ir,
            contracts=contracts,
        )
        for program in programs
    ]
    proposals = _unique_models([item for result in results for item in result.proposals])
    checks = [item for result in results for item in result.checks]
    obligations = [item for result in results for item in result.obligations]
    decisions = [result.decision for result in results]
    rejected_contract_ids = {
        contract.contract_id
        for contract in contracts
        for result in results
        if result.decision.requirement_id == contract.requirement_id
        and any(
            check.operator == "llm_judgment"
            and check.status == "INCOMPLETE"
            and all(
                next(item for item in result.checks if item.id == dependency_id).status == "PROVED"
                for dependency_id in check.depends_on_check_ids
            )
            for check in result.checks
        )
    }
    contracts, holes = build_requirement_contracts(
        active_ids,
        evidence_ir=evidence_ir,
        proposals=[item for item in proposals if item.contract_id not in rejected_contract_ids],
        pack=pack,
    )
    incomplete_requirements = {
        decision.requirement_id
        for decision in decisions
        if decision.proof_status == "INCOMPLETE"
    }
    contract_requirements = {item.contract_id: item.requirement_id for item in contracts}
    for hole in holes:
        hole.contract_ids = [
            item
            for item in hole.contract_ids
            if contract_requirements.get(item) in incomplete_requirements
        ]
        hole.requirement_ids = [
            item for item in hole.requirement_ids if item in incomplete_requirements
        ]
    holes = [item for item in holes if item.contract_ids and item.requirement_ids]
    diagnostics = _unique_models([
        *lowering_diagnostics,
        *(item for result in results for item in result.diagnostics),
    ])
    return CompiledProof(
        evidence_ir=evidence_ir,
        contracts=contracts,
        holes=holes,
        proposals=proposals,
        diagnostics=diagnostics,
        checks=checks,
        obligations=obligations,
        decisions=decisions,
    )


def compile_invoice_proof(
    evidence_items: list[EvidenceItem],
    verification_records: list[VerificationRecord] | None = None,
    policy: InvoiceProofPolicy | None = None,
    *,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
    contracts: list[RequirementContract] | None = None,
) -> CompiledProof:
    return compile_evidence_proof(
        evidence_items,
        verification_records,
        policy or load_invoice_proof_policy(),
        active_requirement_ids={
            "invoice",
            "purchase_order",
            "goods_receipt_or_service_acceptance",
            "three_way_amount_match",
        },
        trusted_sources=trusted_sources,
        contracts=contracts,
    )


def compile_no_active_duplicate_proof(
    evidence_items: list[EvidenceItem],
    verification_records: list[VerificationRecord] | None = None,
    policy: InvoiceProofPolicy | None = None,
    *,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
    contracts: list[RequirementContract] | None = None,
) -> CompiledProof:
    return compile_evidence_proof(
        evidence_items,
        verification_records,
        policy or load_invoice_proof_policy(),
        active_requirement_ids={"invoice", "duplicate_payment_screen", "no_active_duplicate"},
        trusted_sources=trusted_sources,
        contracts=contracts,
    )


def _unique_models(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = value.model_dump_json(exclude_none=True)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


__all__ = [
    "InvoiceProofPolicy",
    "contract_proof_program",
    "compile_evidence_proof",
    "compile_invoice_proof",
    "compile_no_active_duplicate_proof",
    "load_invoice_proof_policy",
]
