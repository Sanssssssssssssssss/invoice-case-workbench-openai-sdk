from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from graphlib import CycleError, TopologicalSorter
from typing import Any, Literal, Mapping

from app.domain.invoice_contracts import contract_hole_id
from app.state.schemas import (
    CaseEvidenceIR,
    CheckResult,
    Claim,
    ClaimSource,
    CompilationDiagnostic,
    DecisionProof,
    EvidenceItem,
    ProofProposal,
    ProofObligation,
    ProposalRef,
    RequirementContract,
    SourceBinding,
    VerificationActionHint,
    VerificationRecord,
)


ISO_4217_CURRENCY_CODES = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND
    BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU
    CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS
    GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY
    KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA
    MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD
    OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK
    SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD
    TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU XBA
    XBB XBC XBD XCD XDR XOF XPD XPF XPT XSU XTS XUA XXX YER ZAR ZMW ZWG
    """.split()
)
CURRENCY_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("US$", "USD"),
    ("HK$", "HKD"),
    ("NZ$", "NZD"),
    ("A$", "AUD"),
    ("C$", "CAD"),
    ("S$", "SGD"),
    ("R$", "BRL"),
    ("\u00a3", "GBP"),
    ("\u20ac", "EUR"),
    ("\u20b9", "INR"),
    ("\u20a9", "KRW"),
    ("\u20bd", "RUB"),
    ("\u20ba", "TRY"),
    ("\u20ab", "VND"),
    ("\u0e3f", "THB"),
    ("\u20b4", "UAH"),
    ("\u20a6", "NGN"),
)


@dataclass(frozen=True)
class AttributeBinding:
    name: str
    field_aliases: tuple[str, ...]
    value_type: str = "enum"
    allowed_values: tuple[str, ...] = ()
    fallback_from_primary_quote: bool = False


@dataclass(frozen=True)
class ClaimBinding:
    subject: str
    predicate: str
    source_kind: Literal["extracted_field", "semantic_ir"]
    evidence_types: tuple[str, ...]
    support_requirement: str
    value_type: str
    field_aliases: tuple[str, ...] = ()
    allowed_values: tuple[str, ...] = ()
    attributes: tuple[AttributeBinding, ...] = ()
    quote_must_contain_value: bool = False


@dataclass(frozen=True)
class ClaimSelector:
    subject: str
    predicate: str
    required: bool = True


@dataclass(frozen=True)
class NodeSpec:
    id: str
    requirement_id: str
    operator: Literal[
        "present",
        "attribute_present",
        "same",
        "allowed",
        "allowed_by_subject",
        "within_percent",
        "llm_judgment",
        "evidence_support",
        "policy_present",
        "all",
    ]
    selectors: tuple[ClaimSelector, ...] = ()
    depends_on: tuple[str, ...] = ()
    attribute: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    executor: Literal["llm", "deterministic"] = "deterministic"
    judgment_id: str = ""
    target_predicate: str = ""
    required_attributes: tuple[str, ...] = ()
    missing_premise: str = ""
    obligation_id: str = ""


@dataclass(frozen=True)
class SemanticGraphSpec:
    id: str
    version: str
    requirement_id: str
    claim_bindings: tuple[ClaimBinding, ...]
    nodes: tuple[NodeSpec, ...]
    root_id: str
    activation_requirements: tuple[str, ...] = ()


@dataclass
class ProgramCompilation:
    claims: list[Claim]
    checks: list[CheckResult]
    obligations: list[ProofObligation]
    decision: DecisionProof
    proposals: list[ProofProposal] = field(default_factory=list)
    diagnostics: list[CompilationDiagnostic] = field(default_factory=list)


def lower_case_evidence_ir(
    specs: list[SemanticGraphSpec] | tuple[SemanticGraphSpec, ...],
    evidence_items: list[EvidenceItem],
    *,
    compiler_version: str,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
    contracts: list[RequirementContract] | None = None,
) -> tuple[CaseEvidenceIR, list[CompilationDiagnostic]]:
    """Lower the case packet once; proof programs consume filtered views of this IR."""
    diagnostics: list[CompilationDiagnostic] = []
    counts = Counter(item.id for item in evidence_items)
    for evidence_id, count in sorted(counts.items()):
        if count > 1:
            diagnostics.append(_diagnostic("DUPLICATE_EVIDENCE_ID", evidence_id=evidence_id, count=count))
    items = [item for item in evidence_items if counts[item.id] == 1]
    bindings = _catalog_bindings(specs)
    claims = _claims_from_bindings(bindings, items, trusted_sources, diagnostics)
    claims = _dedupe_models([
        *claims,
        *_contract_catalog_claims(
            items,
            claims,
            contracts or [],
            trusted_sources,
            diagnostics,
            specialized_predicates={(item.subject, item.predicate) for item in bindings},
        ),
    ])
    source_bindings = [
        _source_binding(item, trusted_sources, diagnostics)
        for item in sorted(items, key=lambda row: row.id)
    ]
    contract_catalog_rows = sorted(
        (
            {"requirement_id": contract.requirement_id, "input": item.model_dump(mode="json")}
            for contract in contracts or []
            for item in contract.inputs
            if item.hole_kind in {"claim", "relation"}
        ),
        key=_stable_value,
    )
    catalog_rows = [asdict(binding) for binding in bindings] + contract_catalog_rows
    catalog_hash = _hash(catalog_rows)
    source_snapshot_hash = _hash([item.model_dump(mode="json") for item in source_bindings])
    versions = sorted({f"{spec.id}:{spec.version}" for spec in specs})
    ir = CaseEvidenceIR(
        catalog_version="|".join(versions) or compiler_version,
        catalog_hash=catalog_hash,
        source_snapshot_hash=source_snapshot_hash,
        requested_predicates=sorted({
            *(f"{item.subject}.{item.predicate}" for item in bindings),
            *(
                f"{item.subject}.{item.predicate}"
                for contract in contracts or []
                for item in contract.inputs
                if item.hole_kind in {"claim", "relation"}
            ),
            *(f"{item.subject}.{item.predicate}" for item in claims),
        }),
        source_bindings=source_bindings,
        claims=claims,
    )
    return ir, diagnostics


def compile_proof_graph(
    spec: SemanticGraphSpec,
    evidence_items: list[EvidenceItem],
    *,
    policy_version: str,
    compiler_version: str,
    verification_records: list[VerificationRecord] | None = None,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
    evidence_ir: CaseEvidenceIR | None = None,
    contracts: list[RequirementContract] | None = None,
) -> ProgramCompilation:
    diagnostics: list[CompilationDiagnostic]
    if evidence_ir is None:
        evidence_ir, diagnostics = lower_case_evidence_ir(
            [spec],
            evidence_items,
            compiler_version=compiler_version,
            trusted_sources=trusted_sources,
            contracts=contracts,
        )
    else:
        diagnostics = []
    evidence_id_counts = Counter(item.id for item in evidence_items)
    unique_evidence_ids = {evidence_id for evidence_id, count in evidence_id_counts.items() if count == 1}
    evidence_items = [item for item in evidence_items if item.id in unique_evidence_ids]
    order = _graph_order(spec)
    relevant_contracts = [item for item in contracts or [] if item.requirement_id == spec.requirement_id]
    claims = _proof_view_claims(
        spec,
        evidence_ir.claims,
        relevant_contracts,
        evidence_ir.source_bindings,
    )
    claims, binding_diagnostics = _bind_proof_view_claims(claims, relevant_contracts)
    diagnostics.extend(binding_diagnostics)
    if any(item.blocking and item.category == "binding" for item in binding_diagnostics):
        proposals, admission_diagnostics = [], []
    else:
        proposals, admission_diagnostics = _admit_proposals(
            spec,
            evidence_items,
            claims,
            contracts or [],
            evidence_ir.source_snapshot_hash,
            trusted_sources,
        )
    diagnostics.extend(admission_diagnostics)
    checks: list[CheckResult] = []
    obligations: list[ProofObligation] = []
    by_id: dict[str, CheckResult] = {}
    nodes = {node.id: node for node in spec.nodes}

    for node_id in order:
        node = nodes[node_id]
        dependencies = [by_id[item] for item in node.depends_on]
        if node.operator == "all":
            status = _aggregate([item.status for item in dependencies])
            claim_ids = _unique(claim_id for item in dependencies for claim_id in item.input_claim_ids)
            evidence_ids = _unique(evidence_id for item in dependencies for evidence_id in item.input_evidence_ids)
            reason = "result follows from declared dependency checks"
        elif any(item.status == "DISPROVED" for item in dependencies):
            status, claim_ids, reason = "NOT_APPLICABLE", [], "a required dependency was disproved"
            evidence_ids = _unique(evidence_id for item in dependencies for evidence_id in item.input_evidence_ids)
            diagnostics.append(_diagnostic("DEPENDENCY_BLOCKED", stage="verification", candidate_id=node.id, reason=reason))
        elif any(item.status != "PROVED" for item in dependencies):
            status, claim_ids, reason = "INCOMPLETE", [], "required dependencies are incomplete"
            evidence_ids = _unique(evidence_id for item in dependencies for evidence_id in item.input_evidence_ids)
            diagnostics.append(_diagnostic("DEPENDENCY_BLOCKED", stage="verification", candidate_id=node.id, reason=reason))
        elif node.operator == "evidence_support":
            status, evidence_ids, reason = _execute_evidence_support(node, evidence_ir)
            claim_ids = []
        elif node.operator == "policy_present":
            status = "PROVED" if node.params.get("configured") is True else "INCOMPLETE"
            claim_ids, evidence_ids = [], []
            reason = "enterprise policy value is configured" if status == "PROVED" else "enterprise policy value is not configured"
        else:
            status, claim_ids, reason = _execute(node, claims, proposals)
            evidence_by_claim = {claim.id: claim.evidence_id for claim in claims}
            evidence_ids = _unique([
                *(evidence_id for item in dependencies for evidence_id in item.input_evidence_ids),
                *(evidence_by_claim.get(claim_id) for claim_id in claim_ids),
            ])
            evidence_ids = _unique([*evidence_ids, *_proposal_evidence_ids(node, proposals)])
        check = CheckResult(
            id=node.id,
            program_id=spec.id,
            requirement_id=node.requirement_id,
            status=status,
            input_claim_ids=claim_ids,
            input_evidence_ids=evidence_ids,
            depends_on_check_ids=list(node.depends_on),
            rule_id=f"{spec.id}:{node.id}",
            reason=reason,
            executor=node.executor,
            operator=node.operator,
        )
        checks.append(check)
        by_id[node.id] = check
        if "no entity_key" in reason:
            diagnostics.append(_diagnostic("ENTITY_KEY_MISSING", stage="verification", candidate_id=node.id, reason=reason))
        if status == "INCOMPLETE" and node.missing_premise and all(item.status == "PROVED" for item in dependencies):
            obligations.append(_obligation(node, relevant_contracts[0] if relevant_contracts else None))

    root = by_id[spec.root_id]
    if root.status == "DISPROVED":
        for obligation in obligations:
            obligation.blocking = False
    snapshot_hash = _snapshot_hash(
        spec,
        claims,
        policy_version=policy_version,
        compiler_version=compiler_version,
        trusted_sources=trusted_sources,
        proposals=proposals,
        contracts=relevant_contracts,
        source_bindings=[
            binding
            for binding in evidence_ir.source_bindings
            if binding.evidence_id in set(root.input_evidence_ids)
        ],
    )
    latest_records = {
        record.obligation_id: record
        for record in verification_records or []
        if record.proof_hash_before == snapshot_hash
    }
    exhausted = {
        obligation_id
        for obligation_id, record in latest_records.items()
        if not record.retry_allowed and not record.new_admissible_claim
    }
    for obligation in obligations:
        obligation.priority_shadow = _obligation_priority(obligation)
    blocking = [item.id for item in obligations if item.blocking]
    if root.status == "PROVED":
        outcome, stop_reason = "EVIDENCE_SUFFICIENT_FOR_REPORT", "all blocking checks were proved"
    elif root.status == "DISPROVED":
        outcome, stop_reason = "EVIDENCE_SUFFICIENT_FOR_REPORT", "a required check was disproved"
    elif blocking and all(item in exhausted for item in blocking):
        outcome, stop_reason = "ABSTAIN_OR_ESCALATE", "all available verification actions were exhausted"
    else:
        outcome, stop_reason = "HOLD_FOR_EVIDENCE", "blocking proof obligations remain unresolved"
    decision = DecisionProof(
        program_id=spec.id,
        requirement_id=spec.requirement_id,
        root_check_id=spec.root_id,
        outcome=outcome,
        proof_status=root.status,
        supporting_check_ids=[item.id for item in checks if item.status == "PROVED"],
        failing_check_ids=[item.id for item in checks if item.status == "DISPROVED"],
        incomplete_check_ids=[item.id for item in checks if item.status == "INCOMPLETE"],
        obligation_ids=blocking,
        policy_version=policy_version,
        compiler_version=compiler_version,
        evidence_snapshot_hash=snapshot_hash,
        stop_reason=stop_reason,
    )
    return ProgramCompilation(
        claims=claims,
        checks=checks,
        obligations=obligations,
        decision=decision,
        proposals=proposals,
        diagnostics=diagnostics,
    )


def _graph_order(spec: SemanticGraphSpec) -> tuple[str, ...]:
    nodes = {node.id: node for node in spec.nodes}
    if len(nodes) != len(spec.nodes):
        raise ValueError(f"Duplicate node id in graph {spec.id}")
    if spec.root_id not in nodes:
        raise ValueError(f"Unknown root node {spec.root_id} in graph {spec.id}")
    empty_aggregates = sorted(node.id for node in spec.nodes if node.operator == "all" and not node.depends_on)
    if empty_aggregates:
        raise ValueError(f"Aggregate nodes require dependencies in {spec.id}: {empty_aggregates}")
    missing = sorted({dependency for node in spec.nodes for dependency in node.depends_on if dependency not in nodes})
    if missing:
        raise ValueError(f"Missing graph dependencies in {spec.id}: {missing}")
    root_ancestors = {spec.root_id}
    pending = [spec.root_id]
    while pending:
        current = pending.pop()
        for dependency in nodes[current].depends_on:
            if dependency not in root_ancestors:
                root_ancestors.add(dependency)
                pending.append(dependency)
    disconnected = sorted(set(nodes) - root_ancestors)
    if disconnected:
        raise ValueError(f"Graph nodes must lead to root in {spec.id}: {disconnected}")
    try:
        return tuple(TopologicalSorter({node.id: set(node.depends_on) for node in spec.nodes}).static_order())
    except CycleError as exc:
        raise ValueError(f"Cycle in proof graph {spec.id}") from exc


def _claims(
    spec: SemanticGraphSpec,
    items: list[EvidenceItem],
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
) -> list[Claim]:
    return _claims_from_bindings(spec.claim_bindings, items, trusted_sources, [])


def _catalog_bindings(specs: list[SemanticGraphSpec] | tuple[SemanticGraphSpec, ...]) -> tuple[ClaimBinding, ...]:
    result: list[ClaimBinding] = []
    seen: set[str] = set()
    for spec in sorted(specs, key=lambda item: (item.id, item.version)):
        for binding in spec.claim_bindings:
            key = json.dumps(asdict(binding), ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                result.append(binding)
    return tuple(result)


def _claims_from_bindings(
    bindings: tuple[ClaimBinding, ...],
    items: list[EvidenceItem],
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
    diagnostics: list[CompilationDiagnostic],
) -> list[Claim]:
    claims: list[Claim] = []
    for binding in bindings:
        for item in sorted(items, key=lambda row: row.id):
            if not _admissible(item, binding):
                continue
            if binding.source_kind == "extracted_field":
                claim = _field_claim(binding, item, trusted_sources, diagnostics)
                if claim:
                    claims.append(claim)
            else:
                continue
    result = _dedupe_models(claims)
    counts = Counter(claim.id for claim in result)
    for claim_id, count in sorted(counts.items()):
        if count > 1:
            diagnostics.append(_diagnostic("CLAIM_ID_CONFLICT", candidate_id=claim_id, count=count))
    return [claim for claim in result if counts[claim.id] == 1]


def _proof_view_claims(
    spec: SemanticGraphSpec,
    claims: list[Claim],
    contracts: list[RequirementContract] | None = None,
    source_bindings: list[SourceBinding] | None = None,
) -> list[Claim]:
    predicates = {(binding.subject, binding.predicate) for binding in spec.claim_bindings}
    inputs = [
        item
        for contract in contracts or []
        for item in contract.inputs
        if item.hole_kind in {"claim", "relation"}
    ]
    bindings_by_id = {item.evidence_id: item for item in source_bindings or []}
    if inputs:
        return [
            claim
            for claim in claims
            if any(_claim_matches_contract_input(claim, item, bindings_by_id) for item in inputs)
        ]
    return [
        claim
        for claim in claims
        if (claim.subject, claim.predicate) in predicates
    ]


def _claim_matches_contract_input(
    claim: Claim,
    item: Any,
    bindings_by_id: Mapping[str, SourceBinding],
) -> bool:
    if claim.subject != item.subject or claim.predicate != item.predicate:
        return False
    binding = bindings_by_id.get(claim.evidence_id)
    if item.role and (
        not binding
        or not binding.trusted
        or not binding.accepted
        or binding.support_levels.get(item.role, "none") == "none"
    ):
        return False
    if item.value_type and claim.value_type != item.value_type:
        return False
    if item.allowed_values and _stable_value(claim.typed_value) not in {
        _stable_value(value) for value in item.allowed_values
    }:
        return False
    return all(
        getattr(claim, attribute, None) not in {None, ""}
        and attribute in claim.attribute_sources
        for attribute in item.required_attributes
    )


def _bind_proof_view_claims(
    claims: list[Claim],
    contracts: list[RequirementContract],
) -> tuple[list[Claim], list[CompilationDiagnostic]]:
    bound = [claim.model_copy(deep=True) for claim in claims]
    diagnostics: list[CompilationDiagnostic] = []
    for contract in contracts:
        for input_ in contract.inputs:
            if not input_.required or input_.binding_mode != "singleton_by_role":
                continue
            relevant = [
                claim
                for claim in bound
                if claim.subject == input_.subject and claim.predicate == input_.predicate
            ]
            if len(relevant) <= 1:
                continue
            diagnostics.append(_diagnostic(
                "SCOPE_CONFLICT",
                stage="verification",
                contract_id=contract.contract_id,
                category="binding",
                retry_owner="reviewer",
                requirement_id=contract.requirement_id,
                slot_id=input_.slot_id,
            ))
            rejected = {claim.id for claim in relevant}
            bound = [claim for claim in bound if claim.id not in rejected]
        groups = {
            item.binding_group
            for item in contract.inputs
            if item.binding_mode == "same_entity" and item.binding_group
        }
        for group in groups:
            inputs = [
                item
                for item in contract.inputs
                if item.binding_mode == "same_entity" and item.binding_group == group
            ]
            relevant = [
                claim
                for claim in bound
                if any(claim.subject == item.subject and claim.predicate == item.predicate for item in inputs)
            ]
            for claim in relevant:
                if not claim.entity_key:
                    claim.entity_key = f"source:{claim.evidence_id}:{group}"
            entity_keys = {claim.entity_key for claim in relevant if claim.entity_key}
            complete = [
                key
                for key in entity_keys
                if all(
                    any(
                        claim.entity_key == key
                        and claim.subject == item.subject
                        and claim.predicate == item.predicate
                        for claim in relevant
                    )
                    for item in inputs
                    if item.required
                )
            ]
            unique_per_slot = all(
                sum(
                    claim.entity_key == complete[0]
                    and claim.subject == item.subject
                    and claim.predicate == item.predicate
                    for claim in relevant
                ) == 1
                for item in inputs
                if item.required
            ) if len(complete) == 1 else False
            if len(entity_keys) <= 1 and len(complete) == 1 and unique_per_slot:
                continue
            if relevant and all(any(claim.subject == item.subject and claim.predicate == item.predicate for claim in relevant) for item in inputs if item.required):
                diagnostics.append(_diagnostic(
                    "SCOPE_AMBIGUOUS" if len(entity_keys) != 1 else "SCOPE_CONFLICT",
                    stage="verification",
                    contract_id=contract.contract_id,
                    category="binding",
                    retry_owner="reviewer",
                    requirement_id=contract.requirement_id,
                    binding_group=group,
                ))
                rejected = {claim.id for claim in relevant}
                bound = [claim for claim in bound if claim.id not in rejected]
    return bound, diagnostics


def _contract_catalog_claims(
    items: list[EvidenceItem],
    existing_claims: list[Claim],
    contracts: list[RequirementContract],
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
    diagnostics: list[CompilationDiagnostic],
    *,
    specialized_predicates: set[tuple[str, str]],
) -> list[Claim]:
    _ = existing_claims, specialized_predicates
    return _candidate_claims(items, contracts, trusted_sources, diagnostics)


def _candidate_claims(
    items: list[EvidenceItem],
    contracts: list[RequirementContract],
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
    diagnostics: list[CompilationDiagnostic],
) -> list[Claim]:
    slots: dict[str, list[tuple[RequirementContract, Any]]] = {}
    for contract in contracts:
        for input_ in contract.inputs:
            if input_.hole_kind not in {"claim", "relation"}:
                continue
            slots.setdefault(contract_hole_id(input_, contract), []).append((contract, input_))
    handles = Counter(candidate.handle for item in items for candidate in item.semantic_claims)
    claims: list[Claim] = []
    for item in sorted(items, key=lambda row: row.id):
        if not _base_admissible(item):
            continue
        supported = {support.requirement for support in item.supports if support.support_level != "none"}
        for candidate in item.semantic_claims:
            entries = slots.get(candidate.hole_id) or []
            if not entries:
                diagnostics.append(_diagnostic(
                    "CLAIM_SLOT_MISMATCH",
                    stage="admission",
                    candidate_id=candidate.handle,
                    evidence_id=item.id,
                    category="binding",
                    retry_owner="reviewer",
                ))
                continue
            signatures = {
                (
                    input_.subject,
                    input_.predicate,
                    input_.value_type,
                    tuple(input_.allowed_values),
                    tuple(input_.required_attributes),
                    input_.binding_mode,
                    input_.binding_group,
                )
                for _contract, input_ in entries
            }
            contract, input_ = entries[0]
            roles = {item.role for _owner, item in entries if item.role}
            if len(signatures) != 1 or handles[candidate.handle] != 1 or roles and not roles.intersection(supported):
                diagnostics.append(_diagnostic(
                    "CLAIM_SLOT_MISMATCH",
                    stage="admission",
                    candidate_id=candidate.handle,
                    evidence_id=item.id,
                    contract_id=contract.contract_id,
                    category="binding",
                    retry_owner="reviewer",
                    requirement_id=contract.requirement_id,
                ))
                continue
            typed_value = _generic_typed_value(candidate.typed_value, input_.value_type)
            attributes = {
                str(key): value
                for key, value in candidate.attributes.items()
                if value is not None and value != ""
            }
            error = "VALUE_PARSE_FAILED" if typed_value is None else _candidate_error(
                candidate.model_dump(mode="json"),
                item.id,
                candidate.source_quote,
                candidate.source_locator,
                candidate.confidence,
                trusted_sources,
            )
            if not error and not _generic_value_grounded(candidate.source_quote, typed_value, input_.value_type):
                error = "QUOTE_VALUE_MISMATCH"
            if not error and input_.allowed_values and _stable_value(typed_value) not in {
                _stable_value(value) for value in input_.allowed_values
            }:
                error = "VALUE_PARSE_FAILED"
            if not error and set(input_.required_attributes) - set(attributes):
                error = "VALUE_PARSE_FAILED"
            if not error and set(input_.required_attributes) - set(candidate.attribute_sources):
                error = "ATTRIBUTE_SOURCE_MISSING"
            if not error:
                for attribute in input_.required_attributes:
                    source = candidate.attribute_sources[attribute]
                    error = _candidate_error(
                        source.model_dump(mode="json"),
                        item.id,
                        source.source_quote,
                        source.source_locator,
                        candidate.confidence,
                        trusted_sources,
                    )
                    if not error and not _generic_value_grounded(
                        source.source_quote,
                        attributes[attribute],
                        "currency" if attribute == "currency" else "string",
                    ):
                        error = "ATTRIBUTE_VALUE_MISMATCH"
                    if error:
                        break
            if error:
                diagnostics.append(_diagnostic(
                    error,
                    candidate_id=candidate.handle,
                    evidence_id=item.id,
                    contract_id=contract.contract_id,
                    category="source",
                    retry_owner="reviewer",
                ))
                continue
            entity_key = candidate.entity_handle.strip() or None
            if input_.binding_mode == "same_entity" and not entity_key:
                entity_key = f"source:{item.id}:{input_.binding_group}"
            if input_.binding_mode == "per_entity" and not entity_key:
                diagnostics.append(_diagnostic(
                    "SCOPE_REQUIRED",
                    stage="admission",
                    candidate_id=candidate.handle,
                    evidence_id=item.id,
                    contract_id=contract.contract_id,
                    category="binding",
                    retry_owner="reviewer",
                    requirement_id=contract.requirement_id,
                ))
                continue
            binding = ClaimBinding(
                subject=input_.subject,
                predicate=input_.predicate,
                source_kind="semantic_ir",
                evidence_types=(str(item.type),),
                support_requirement=input_.role,
                value_type=input_.value_type,
            )
            claim_id = "CLM_" + _hash({
                "evidence_id": item.id,
                "hole_id": candidate.hole_id,
                "value": typed_value,
                "quote": candidate.source_quote,
                "locator": candidate.source_locator,
                "entity": entity_key,
            })[:24]
            claims.append(_claim(
                binding,
                item,
                typed_value,
                candidate.source_quote,
                candidate.source_locator,
                candidate.confidence,
                attributes,
                candidate.attribute_sources,
                claim_id=claim_id,
                entity_key=entity_key,
            ))
    return claims


def _source_binding(
    item: EvidenceItem,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
    diagnostics: list[CompilationDiagnostic],
) -> SourceBinding:
    trusted = trusted_sources.get(item.id) if trusted_sources else None
    manifest_trusted = bool(
        isinstance(trusted, Mapping)
        and str(trusted.get("sha256") or "")
        and str(trusted.get("original_ref") or "")
    )
    accepted = _source_accepted(item)
    user_texts = tuple(
        text
        for text in (str(item.content or "").strip(), str(item.summary or "").strip())
        if text
    )
    user_message_trusted = item.source == "user_message" and accepted and bool(user_texts)
    is_trusted = manifest_trusted or user_message_trusted
    support_levels: dict[str, str] = {}
    rank = {"none": 0, "partial": 1, "full": 2}
    for support in item.supports:
        if support.support_level != "none" and not is_trusted:
            diagnostics.append(_diagnostic(
                "SOURCE_NOT_BOUND",
                stage="admission",
                candidate_id=f"support:{item.id}:{support.requirement}",
                evidence_id=item.id,
                category="source",
                retry_owner="reviewer",
                requirement_id=support.requirement,
            ))
            continue
        current = support_levels.get(support.requirement, "none")
        if rank[support.support_level] > rank[current]:
            support_levels[support.requirement] = support.support_level
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    review = item.review_result if isinstance(item.review_result, dict) else {}
    return SourceBinding(
        evidence_id=item.id,
        evidence_type=item.type,
        source=item.source,
        credibility=item.credibility,
        source_ref=str(
            metadata.get("source_ref")
            or review.get("source_ref")
            or (trusted.get("original_ref") if manifest_trusted and isinstance(trusted, Mapping) else "")
            or (f"user_message:{item.id}" if user_message_trusted else "")
            or ""
        ),
        source_fingerprint=(
            str(trusted.get("sha256") or "")
            if manifest_trusted and isinstance(trusted, Mapping)
            else hashlib.sha256("\n".join(user_texts).encode("utf-8")).hexdigest()
            if user_message_trusted
            else ""
        ),
        trusted=is_trusted,
        accepted=accepted,
        supports=sorted(key for key, value in support_levels.items() if value != "none"),
        support_levels=support_levels,
    )


def _candidate_error(
    raw: Any,
    evidence_id: str,
    quote: str,
    locator: str,
    confidence: str,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
) -> str:
    if not quote:
        return "QUOTE_MISSING"
    trusted = trusted_sources.get(evidence_id) if trusted_sources else None
    if not isinstance(trusted, Mapping) or not str(trusted.get("sha256") or "") or not str(trusted.get("original_ref") or ""):
        return "SOURCE_NOT_BOUND"
    if not _quote_grounded(trusted_sources, evidence_id, quote):
        return "QUOTE_NOT_GROUNDED"
    if not locator:
        return "LOCATOR_MISSING"
    if confidence not in {"medium", "high"}:
        return "CONFIDENCE_TOO_LOW"
    if _explicitly_unusable(raw):
        return "CANDIDATE_UNUSABLE"
    return ""


def _claim_diagnostic(
    code: str,
    binding: ClaimBinding,
    item: EvidenceItem,
    candidate_id: str = "",
) -> CompilationDiagnostic:
    return _diagnostic(
        code,
        candidate_id=candidate_id,
        evidence_id=item.id,
        subject=binding.subject,
        predicate=binding.predicate,
    )


def _diagnostic(
    code: str,
    *,
    stage: str = "lowering",
    candidate_id: str = "",
    evidence_id: str = "",
    contract_id: str = "",
    category: str = "proof",
    retry_owner: str = "none",
    requirement_id: str = "",
    blocking: bool = True,
    **details: Any,
) -> CompilationDiagnostic:
    return CompilationDiagnostic(
        stage=stage,
        code=code,
        candidate_id=candidate_id,
        evidence_id=evidence_id,
        contract_id=contract_id,
        category=category,
        retry_owner=retry_owner,
        requirement_id=requirement_id,
        blocking=blocking,
        details=details,
    )


def _field_claim(
    binding: ClaimBinding,
    item: EvidenceItem,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
    diagnostics: list[CompilationDiagnostic] | None = None,
) -> Claim | None:
    diagnostics = diagnostics if diagnostics is not None else []
    fields = item.metadata.get("extracted_fields") if isinstance(item.metadata, dict) else None
    if not isinstance(fields, dict):
        return None
    candidates = [
        (fields[key], _typed_value(_value(fields[key]), binding.value_type, binding.allowed_values))
        for key in binding.field_aliases
        if key in fields
    ]
    if not candidates:
        return None
    values = {_stable_value(value) for _, value in candidates if value is not None}
    if not values:
        diagnostics.append(_claim_diagnostic("VALUE_PARSE_FAILED", binding, item))
        return None
    if len(values) != 1:
        diagnostics.append(_claim_diagnostic("VALUE_AMBIGUOUS", binding, item))
        return None
    raw, typed_value = next((field, value) for field, value in candidates if value is not None)
    quote = _source_quote(raw)
    if binding.quote_must_contain_value:
        quote = _matching_decimal_quote([quote], typed_value)
    locator = _source_locator(raw) or str(_value(fields.get("source_locator")) or "").strip()
    confidence = _attr(raw, "confidence").lower()
    error = _candidate_error(raw, item.id, quote, locator, confidence, trusted_sources)
    if error:
        diagnostics.append(_claim_diagnostic(error, binding, item))
        return None
    attributes: dict[str, Any] = {}
    sources: dict[str, ClaimSource] = {}
    for attribute in binding.attributes:
        value, source = _field_attribute(fields, attribute, item.id, trusted_sources)
        if value is None and attribute.fallback_from_primary_quote:
            value = _typed_value(quote, attribute.value_type, attribute.allowed_values)
            source = ClaimSource(source_quote=quote, source_locator=locator) if value is not None else None
        if value is not None and source is not None:
            attributes[attribute.name] = value
            sources[attribute.name] = source
    return _claim(binding, item, typed_value, quote, locator, confidence, attributes, sources)


def _claim(
    binding: ClaimBinding,
    item: EvidenceItem,
    typed_value: Any,
    quote: str,
    locator: str,
    confidence: str,
    attributes: dict[str, Any],
    sources: dict[str, ClaimSource],
    *,
    claim_id: str = "",
    entity_key: str | None = None,
) -> Claim:
    identity = json.dumps(
        {
            "evidence_id": item.id,
            "subject": binding.subject,
            "predicate": binding.predicate,
            "entity_key": entity_key,
            "typed_value": typed_value,
            "source_locator": locator,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    prefix = re.sub(r"[^A-Z0-9]+", "_", f"{binding.subject}_{binding.predicate}".upper()).strip("_")
    claim_id = claim_id or f"CLM_{prefix}_{hashlib.sha256(identity.encode()).hexdigest()}"
    return Claim(
        id=claim_id,
        subject=binding.subject,
        predicate=binding.predicate,
        entity_key=entity_key,
        value_type=binding.value_type,
        typed_value=str(typed_value) if isinstance(typed_value, Decimal) else typed_value,
        unit=attributes.get("unit") or ("money" if binding.value_type == "decimal" else None),
        currency=attributes.get("currency"),
        basis=attributes.get("basis"),
        tax_basis=attributes.get("tax_basis"),
        coverage=attributes.get("coverage"),
        evidence_id=item.id,
        source_quote=quote,
        source_locator=locator,
        confidence=confidence,
        attributes=attributes,
        attribute_sources=sources,
    )


def _field_attribute(
    fields: dict[str, Any],
    binding: AttributeBinding,
    evidence_id: str,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[Any | None, ClaimSource | None]:
    candidates: list[tuple[Any, ClaimSource]] = []
    for key in binding.field_aliases:
        field = fields.get(key)
        value = _typed_value(_value(field), binding.value_type, binding.allowed_values)
        quote = _source_quote(field)
        locator = _source_locator(field)
        confidence = _attr(field, "confidence").lower()
        if (
            value is not None
            and quote
            and _quote_grounded(trusted_sources, evidence_id, quote)
            and locator
            and confidence in {"medium", "high"}
            and not _explicitly_unusable(field)
        ):
            candidates.append((value, ClaimSource(source_quote=quote, source_locator=locator)))
    values = {_stable_value(value) for value, _ in candidates}
    if len(values) != 1:
        return None, None
    value = candidates[0][0]
    source = next(source for candidate, source in candidates if _stable_value(candidate) == _stable_value(value))
    return value, source


def _admit_proposals(
    spec: SemanticGraphSpec,
    items: list[EvidenceItem],
    claims: list[Claim],
    contracts: list[RequirementContract],
    evidence_snapshot_hash: str,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[list[ProofProposal], list[CompilationDiagnostic]]:
    nodes = {node.judgment_id: node for node in spec.nodes if node.judgment_id}
    targets = {(node.target_predicate or node.judgment_id): node.judgment_id for node in nodes.values()}
    contracts_by_id = {item.contract_id: item for item in contracts}
    view_contracts = [item for item in contracts if item.requirement_id == spec.requirement_id]
    active_evidence_ids = {item.id for item in items if _base_admissible(item)}
    proposals: list[ProofProposal] = []
    diagnostics: list[CompilationDiagnostic] = []
    for item in sorted(items, key=lambda row: row.id):
        if not _base_admissible(item):
            continue
        candidate_rows: list[dict[str, Any]] = []
        for candidate in item.semantic_proposals:
            owner = next(
                (
                    contract
                    for contract in contracts
                    for input_ in contract.inputs
                    if input_.hole_kind == "judgment"
                    and contract_hole_id(input_, contract) == candidate.hole_id
                ),
                None,
            )
            if owner is not None and owner.requirement_id != spec.requirement_id:
                continue
            row = _candidate_proposal_row(candidate, item, items, claims, contracts, diagnostics)
            if row is not None:
                candidate_rows.append(row)
        for raw in candidate_rows:
            if not isinstance(raw, dict):
                continue
            target = str(raw.get("target_predicate") or "").strip()
            contract_id = str(raw.get("contract_id") or "").strip()
            contract = contracts_by_id.get(contract_id)
            if contract is not None and contract.requirement_id != spec.requirement_id:
                continue
            judgment_id = targets.get(target)
            if not judgment_id:
                diagnostics.append(
                    _diagnostic(
                        "STALE_CONTRACT" if contract is None else "PROPOSAL_REJECTED",
                        stage="admission",
                        candidate_id=str(raw.get("id") or ""),
                        evidence_id=item.id,
                        contract_id=contract_id,
                        errors=["stale or inactive Contract" if contract is None else "target predicate has no active proof node"],
                    )
                )
                continue
            candidate_errors = _strings(raw.get("_validation_errors"))
            contract_errors: list[str] = []
            if contract is None:
                contract_errors.append("stale or inactive Contract")
            else:
                allowed_targets = {contract.target_predicate}.union(
                    input_.predicate for input_ in contract.inputs if input_.hole_kind == "judgment"
                )
                if str(raw.get("contract_hash") or "") != contract.contract_hash:
                    contract_errors.append("stale Contract hash")
                if target not in allowed_targets:
                    contract_errors.append("target predicate is outside the Contract")
            proposal = _admit_proposal(
                raw,
                carrier_id=item.id,
                judgment_id=judgment_id,
                claims=claims,
                active_evidence_ids=active_evidence_ids,
                contract=contract,
                target_predicate=target,
                evidence_snapshot_hash=evidence_snapshot_hash,
                extra_errors=[*candidate_errors, *contract_errors],
                trusted_sources=trusted_sources,
            )
            proposals.append(proposal)
            if not proposal.valid:
                diagnostics.append(
                    _diagnostic(
                        "STALE_CONTRACT"
                        if contract_errors
                        else "STALE_PROPOSAL"
                        if "stale input Claim reference" in proposal.validation_errors
                        else "PROPOSAL_REJECTED",
                        stage="admission",
                        candidate_id=proposal.id,
                        evidence_id=item.id,
                        contract_id=contract_id,
                        errors=proposal.validation_errors,
                    )
                )
    return _dedupe_models(proposals), diagnostics


def _candidate_proposal_row(
    candidate: Any,
    carrier: EvidenceItem,
    items: list[EvidenceItem],
    claims: list[Claim],
    contracts: list[RequirementContract],
    diagnostics: list[CompilationDiagnostic],
) -> dict[str, Any] | None:
    entry = next(
        (
            (contract, input_)
            for contract in contracts
            for input_ in contract.inputs
            if input_.hole_kind == "judgment" and contract_hole_id(input_, contract) == candidate.hole_id
        ),
        None,
    )
    if entry is None:
        diagnostics.append(_diagnostic(
            "STALE_CONTRACT",
            stage="admission",
            candidate_id=candidate.handle,
            evidence_id=carrier.id,
            category="binding",
            retry_owner="reviewer",
        ))
        return None
    contract, judgment_input = entry
    claim_inputs = [item for item in contract.inputs if item.hole_kind in {"claim", "relation"}]
    relevant = [
        claim
        for claim in claims
        if any(claim.subject == item.subject and claim.predicate == item.predicate for item in claim_inputs)
    ]
    if candidate.entity_handle:
        relevant = [
            claim
            for claim in relevant
            if not claim.entity_key or claim.entity_key == candidate.entity_handle
        ]
    elif any(item.binding_mode == "per_entity" for item in claim_inputs) and any(claim.entity_key for claim in relevant):
        diagnostics.append(_diagnostic(
            "SCOPE_REQUIRED",
            stage="admission",
            candidate_id=candidate.handle,
            evidence_id=carrier.id,
            contract_id=contract.contract_id,
            category="binding",
            retry_owner="reviewer",
            requirement_id=contract.requirement_id,
        ))
        return None
    input_refs, input_errors = _candidate_handle_refs(
        candidate.input_handles,
        relevant,
        contract,
        claim_inputs,
        items,
        label="input",
    )
    supporting_refs, supporting_errors = _candidate_handle_refs(
        candidate.supporting_handles,
        relevant,
        contract,
        claim_inputs,
        items,
        label="supporting",
    )
    opposing_refs, opposing_errors = _candidate_handle_refs(
        candidate.opposing_handles,
        relevant,
        contract,
        claim_inputs,
        items,
        label="opposing",
    )
    validation_errors = [*input_errors, *supporting_errors, *opposing_errors]
    if candidate.verdict in {"SUPPORTED", "REFUTED"} and not candidate.input_handles:
        validation_errors.append("strong Proposal requires explicit input_handles")
    return {
        "id": candidate.handle,
        "contract_id": contract.contract_id,
        "contract_hash": contract.contract_hash,
        "target_predicate": judgment_input.predicate,
        "verdict": candidate.verdict,
        "input_refs": input_refs,
        "supporting_refs": supporting_refs,
        "opposing_refs": opposing_refs,
        "open_questions": candidate.open_questions,
        "confidence": candidate.confidence,
        "reason": candidate.reason,
        "_validation_errors": validation_errors,
    }


def _candidate_handle_refs(
    raw_handles: list[str],
    relevant: list[Claim],
    contract: RequirementContract,
    claim_inputs: list[Any],
    items: list[EvidenceItem],
    *,
    label: str,
) -> tuple[list[dict[str, str]], list[str]]:
    resolved: list[Claim] = []
    errors: list[str] = []
    seen: set[str] = set()
    relevant_by_id = {claim.id: claim for claim in relevant}
    for raw_handle in raw_handles:
        handle = str(raw_handle).strip()
        if not handle:
            errors.append(f"empty {label} handle")
            continue
        if handle in seen:
            errors.append(f"duplicate {label} handle {handle}")
            continue
        seen.add(handle)
        matches = {handle: relevant_by_id[handle]} if handle in relevant_by_id else {}
        declarations = [
            (source, candidate)
            for source in items
            for candidate in source.semantic_claims
            if candidate.handle == handle
        ]
        for source, candidate in declarations:
            slots = [
                input_
                for input_ in claim_inputs
                if contract_hole_id(input_, contract) == candidate.hole_id
            ]
            for input_ in slots:
                entity_key = candidate.entity_handle.strip() or None
                if input_.binding_mode == "same_entity" and not entity_key:
                    entity_key = f"source:{source.id}:{input_.binding_group}"
                claim_id = "CLM_" + _hash({
                    "evidence_id": source.id,
                    "hole_id": candidate.hole_id,
                    "value": _generic_typed_value(candidate.typed_value, input_.value_type),
                    "quote": candidate.source_quote,
                    "locator": candidate.source_locator,
                    "entity": entity_key,
                })[:24]
                if claim_id in relevant_by_id:
                    matches[claim_id] = relevant_by_id[claim_id]
        if len(declarations) > 1:
            errors.append(f"ambiguous {label} handle {handle}")
        elif len(matches) == 1:
            resolved.append(next(iter(matches.values())))
        elif len(matches) > 1:
            errors.append(f"ambiguous {label} handle {handle}")
        else:
            errors.append(f"dangling {label} handle {handle}")
    return [
        {
            "claim_id": claim.id,
            "evidence_id": claim.evidence_id,
            "source_quote": claim.source_quote,
            "source_locator": claim.source_locator,
        }
        for claim in _dedupe_models(resolved)
    ], errors


def _admit_proposal(
    raw: dict[str, Any],
    *,
    carrier_id: str,
    judgment_id: str,
    claims: list[Claim],
    active_evidence_ids: set[str],
    contract: RequirementContract | None,
    target_predicate: str,
    evidence_snapshot_hash: str,
    extra_errors: list[str] | None = None,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> ProofProposal:
    input_refs, input_errors = _proposal_refs(raw.get("input_refs") or raw.get("considered_refs"), claims, active_evidence_ids, trusted_sources)
    supporting_refs, supporting_errors = _proposal_refs(raw.get("supporting_refs"), claims, active_evidence_ids, trusted_sources)
    opposing_refs, opposing_errors = _proposal_refs(raw.get("opposing_refs"), claims, active_evidence_ids, trusted_sources)
    validation_errors = [*input_errors, *supporting_errors, *opposing_errors]
    input_keys = {_stable_value(item.model_dump(mode="json")) for item in input_refs}
    supporting_keys = {_stable_value(item.model_dump(mode="json")) for item in supporting_refs}
    opposing_keys = {_stable_value(item.model_dump(mode="json")) for item in opposing_refs}
    if not input_refs:
        validation_errors.append("no resolved input refs")
    if not supporting_keys.union(opposing_keys) <= input_keys:
        validation_errors.append("supporting/opposing refs must be input refs")
    if supporting_keys.intersection(opposing_keys):
        validation_errors.append("supporting/opposing refs must be disjoint")
    verdict = str(raw.get("verdict") or "UNKNOWN")
    if verdict == "SUPPORTED" and (not supporting_refs or opposing_refs):
        validation_errors.append("SUPPORTED requires supporting refs and no opposing refs")
    if verdict == "REFUTED" and not opposing_refs:
        validation_errors.append("REFUTED requires opposing refs")
    if verdict in {"SUPPORTED", "REFUTED"} and raw.get("open_questions"):
        validation_errors.append(f"{verdict} requires no open questions")
    if extra_errors:
        validation_errors.extend(extra_errors)
    validation_errors = _unique(validation_errors)

    contract_id = str(raw.get("contract_id") or (contract.contract_id if contract else f"legacy:{judgment_id}"))
    contract_hash = str(raw.get("contract_hash") or (contract.contract_hash if contract else "legacy"))
    proposal_id = str(raw.get("id") or "").strip() or f"proposal:{_hash([carrier_id, judgment_id, raw])[:24]}"
    return ProofProposal(
        id=proposal_id,
        contract_id=contract_id,
        contract_hash=contract_hash,
        target_predicate=target_predicate,
        entity_scope={str(key): str(value) for key, value in (raw.get("entity_scope") or {}).items()},
        verdict=verdict,
        input_refs=input_refs,
        supporting_refs=supporting_refs,
        opposing_refs=opposing_refs,
        open_questions=_strings(raw.get("open_questions")),
        confidence=str(raw.get("confidence") or "low"),
        reason=str(raw.get("reason") or "").strip(),
        proposed_by_evidence_id=carrier_id,
        evidence_snapshot_hash=evidence_snapshot_hash,
        valid=not validation_errors,
        validation_errors=validation_errors,
    )


def _proposal_refs(
    raw_refs: Any,
    claims: list[Claim],
    active_evidence_ids: set[str],
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[list[ProposalRef], list[str]]:
    result: list[ProposalRef] = []
    errors: list[str] = []
    for raw in raw_refs if isinstance(raw_refs, list) else []:
        if not isinstance(raw, dict):
            errors.append("invalid Proposal ref")
            continue
        evidence_id = str(raw.get("evidence_id") or "").strip()
        claim_id = str(raw.get("claim_id") or "").strip()
        subject = str(raw.get("subject") or "").strip()
        predicate = str(raw.get("predicate") or raw.get("field") or "").strip()
        quote = str(raw.get("source_quote") or "").strip()
        locator = str(raw.get("source_locator") or "").strip()
        matches = _matching_ref_claims(raw, claims)
        if (claim_id or subject or predicate) and len(matches) == 1 and matches[0].evidence_id in active_evidence_ids:
            claim = matches[0]
            result.append(ProposalRef(
                claim_id=claim.id,
                evidence_id=claim.evidence_id,
                source_quote=claim.source_quote,
                source_locator=claim.source_locator,
            ))
            continue
        errors.append(f"unresolved Proposal ref {evidence_id}:{claim_id or subject}:{predicate}")
    return _dedupe_models(result), errors


def _matching_ref_claims(raw: Mapping[str, Any], claims: list[Claim]) -> list[Claim]:
    evidence_id = str(raw.get("evidence_id") or "").strip()
    claim_id = str(raw.get("claim_id") or "").strip()
    subject = str(raw.get("subject") or "").strip()
    predicate = str(raw.get("predicate") or raw.get("field") or "").strip()
    quote = " ".join(str(raw.get("source_quote") or "").split())
    locator = str(raw.get("source_locator") or "").strip()

    def matching(*, require_claim_id: bool) -> list[Claim]:
        return [
            claim
            for claim in claims
            if (not evidence_id or claim.evidence_id == evidence_id)
            and (not require_claim_id or not claim_id or claim.id == claim_id)
            and (not subject or claim.subject == subject)
            and (not predicate or claim.predicate == predicate)
            and (not quote or " ".join(claim.source_quote.split()) == quote)
            and (not locator or claim.source_locator == locator)
        ]

    matches = matching(require_claim_id=True)
    if not matches and claim_id and evidence_id and subject and predicate and quote and locator:
        matches = matching(require_claim_id=False)
    return matches


def _execute(node: NodeSpec, claims: list[Claim], judgments: list[ProofProposal]) -> tuple[str, list[str], str]:
    if node.operator == "present":
        value, claim_ids, error = _selector_value(node.selectors[0], claims)
        return ("INCOMPLETE", claim_ids, error) if error else ("PROVED", claim_ids, f"claim value is present: {value}")
    if node.operator == "attribute_present":
        claim_ids: list[str] = []
        for selector in node.selectors:
            _value, ids, error = _selector_value(selector, claims, node.attribute)
            claim_ids.extend(ids)
            if error:
                return "INCOMPLETE", _unique(claim_ids), error
        return "PROVED", _unique(claim_ids), f"source-linked {node.attribute} is present"
    if node.operator in {"same", "allowed", "allowed_by_subject", "within_percent"}:
        return _execute_values(node, claims)
    if node.operator == "llm_judgment":
        return _execute_judgment(node, claims, judgments)
    raise ValueError(f"Unsupported proof operator: {node.operator}")


def _execute_evidence_support(
    node: NodeSpec,
    evidence_ir: CaseEvidenceIR,
) -> tuple[str, list[str], str]:
    role = str(node.params.get("role") or node.requirement_id)
    bindings = [
        item
        for item in evidence_ir.source_bindings
        if item.trusted and item.accepted and item.support_levels.get(role, "none") != "none"
    ]
    full = [item.evidence_id for item in bindings if item.support_levels.get(role) == "full"]
    if full:
        return "PROVED", sorted(set(full)), f"trusted accepted evidence fully supports {role}"
    partial = sorted({item.evidence_id for item in bindings})
    return "INCOMPLETE", partial, f"no trusted accepted evidence fully supports {role}"


def _proposal_evidence_ids(node: NodeSpec, proposals: list[ProofProposal]) -> list[str]:
    target = node.target_predicate or node.judgment_id
    return _unique(
        ref.evidence_id
        for proposal in proposals
        if proposal.valid and proposal.target_predicate == target
        for ref in proposal.input_refs
    )


def _execute_values(node: NodeSpec, claims: list[Claim]) -> tuple[str, list[str], str]:
    values: list[Any] = []
    claim_ids: list[str] = []
    for selector in node.selectors:
        value, ids, error = _selector_value(selector, claims, node.attribute)
        claim_ids.extend(ids)
        if error:
            return "INCOMPLETE", _unique(claim_ids), error
        values.append(value)
    if node.operator == "same":
        if len({_stable_value(item) for item in values}) == 1:
            return "PROVED", _unique(claim_ids), f"{node.attribute or 'claim values'} are equal"
        return str(node.params.get("on_mismatch") or "DISPROVED"), _unique(claim_ids), f"{node.attribute or 'claim values'} differ"
    if node.operator == "allowed":
        allowed = {_stable_value(item) for item in node.params.get("allowed", ())}
        unknown = {_stable_value(item) for item in node.params.get("unknown", ("unknown",))}
        if any(_stable_value(item) in unknown for item in values):
            return "INCOMPLETE", _unique(claim_ids), f"{node.attribute or 'claim value'} is unknown"
        return ("PROVED", _unique(claim_ids), f"{node.attribute or 'claim values'} satisfy allowed values") if all(_stable_value(item) in allowed for item in values) else (str(node.params.get("on_disallowed") or "DISPROVED"), _unique(claim_ids), f"{node.attribute or 'claim values'} are outside allowed values")
    if node.operator == "allowed_by_subject":
        allowed_by_subject = node.params.get("allowed_by_subject") or {}
        unknown = {_stable_value(item) for item in node.params.get("unknown", ("unknown",))}
        if any(_stable_value(item) in unknown for item in values):
            return "INCOMPLETE", _unique(claim_ids), f"{node.attribute} is unknown"
        permitted = all(_stable_value(value) in {_stable_value(item) for item in allowed_by_subject.get(selector.subject, ())} for selector, value in zip(node.selectors, values))
        return ("PROVED", _unique(claim_ids), f"{node.attribute} satisfies subject policy") if permitted else (str(node.params.get("on_disallowed") or "DISPROVED"), _unique(claim_ids), f"{node.attribute} violates subject policy")
    decimals = [_decimal(value) for value in values]
    if any(value is None for value in decimals):
        return "INCOMPLETE", _unique(claim_ids), "numeric claim value is invalid"
    threshold = Decimal(str(node.params["threshold_percent"]))
    pairs = node.params.get("pairs") or ((0, 1),)
    maximum = max(_variance(decimals[left], decimals[right]) for left, right in pairs)  # type: ignore[index]
    status = "PROVED" if maximum <= threshold else "DISPROVED"
    return status, _unique(claim_ids), f"maximum variance {maximum}% against inclusive {threshold}% tolerance"


def _execute_judgment(node: NodeSpec, claims: list[Claim], judgments: list[ProofProposal]) -> tuple[str, list[str], str]:
    target = node.target_predicate or node.judgment_id
    relevant: list[Claim] = []
    if node.selectors:
        for selector in node.selectors:
            matches = _matching_claims(selector, claims)
            if selector.required and not matches:
                return "INCOMPLETE", [claim.id for claim in relevant], f"missing semantic claim {selector.subject}:{selector.predicate}"
            relevant.extend(matches)
    else:
        proposed_ids = {
            claim_id
            for judgment in judgments
            if judgment.target_predicate == target
            for claim_id in judgment.input_claim_ids
        }
        relevant.extend(claim for claim in claims if claim.id in proposed_ids)
    relevant = _dedupe_models(relevant)
    for claim in relevant:
        for attribute in node.required_attributes:
            if claim.attributes.get(attribute) in {None, "", "unknown"} or attribute not in claim.attribute_sources:
                return "INCOMPLETE", [item.id for item in relevant], f"missing source-linked {attribute} on {claim.id}"
    relevant_ids = {claim.id for claim in relevant}
    minimum = {"low": 0, "medium": 1, "high": 2}[str(node.params.get("min_confidence") or "high")]
    proposals = [
        item
        for item in judgments
        if item.target_predicate == target and "stale input Claim reference" not in item.validation_errors
    ]
    if not proposals:
        return "INCOMPLETE", sorted(relevant_ids), "no semantic judgment proposal"
    if any(not item.valid for item in proposals):
        return "INCOMPLETE", sorted(relevant_ids), "an active semantic judgment proposal is invalid"
    if any({"low": 0, "medium": 1, "high": 2}[item.confidence] < minimum for item in proposals):
        return "INCOMPLETE", sorted(relevant_ids), "semantic judgment confidence is below policy"
    entity_config = node.params.get("entity_judgments")
    if isinstance(entity_config, Mapping):
        return _execute_entity_judgments(relevant, proposals, entity_config)
    grouped_contract = node.params.get("grouped_contract")
    if isinstance(grouped_contract, Mapping):
        return _execute_grouped_contract_judgments(relevant, proposals, grouped_contract)
    if any(set(item.input_claim_ids) != relevant_ids for item in proposals):
        return "INCOMPLETE", sorted(relevant_ids), "semantic judgment does not cover the current Claim set"
    verdicts = {item.verdict for item in proposals}
    if len(verdicts) != 1:
        return "INCOMPLETE", sorted(relevant_ids), "semantic judgments conflict"
    verdict = next(iter(verdicts))
    constraints = node.params.get("verdict_values", {}).get(verdict, ())
    if constraints and not _constraints_match(relevant, constraints):
        return "INCOMPLETE", sorted(relevant_ids), "semantic judgment conflicts with required Claim values"
    options = node.params.get("verdict_value_options", {}).get(verdict, ())
    if options and not any(_constraints_match(relevant, option) for option in options):
        return "INCOMPLETE", sorted(relevant_ids), "semantic judgment matches no allowed Claim-value option"
    chosen = sorted(proposals, key=lambda item: item.model_dump_json())[0]
    status = {"SUPPORTED": "PROVED", "REFUTED": "DISPROVED", "UNKNOWN": "INCOMPLETE"}[verdict]
    return status, sorted(relevant_ids), f"LLM judgment {verdict}: {chosen.reason or 'no narrative reason supplied'}"


def _execute_grouped_contract_judgments(
    relevant: list[Claim],
    proposals: list[ProofProposal],
    config: Mapping[str, Any],
) -> tuple[str, list[str], str]:
    relevant_ids = {claim.id for claim in relevant}
    group_selectors = {tuple(item) for item in config.get("group_selectors") or ()}
    required_group_selectors = {tuple(item) for item in config.get("required_group_selectors") or ()}
    branch_selector = tuple(config.get("branch_selector") or ())
    branch = _single_claim_value(relevant, branch_selector)
    if branch is None:
        return "INCOMPLETE", sorted(relevant_ids), "entity lifecycle branch is missing or ambiguous"

    global_ids = {
        claim.id
        for claim in relevant
        if (claim.subject, claim.predicate) not in group_selectors
    }
    groups: dict[str, set[str]] = {}
    for claim in relevant:
        if (claim.subject, claim.predicate) not in group_selectors:
            if claim.entity_key:
                return "INCOMPLETE", sorted(relevant_ids), f"global Claim {claim.id} unexpectedly has an entity_key"
            continue
        if not claim.entity_key:
            return "INCOMPLETE", sorted(relevant_ids), f"entity Claim {claim.id} has no entity_key"
        groups.setdefault(claim.entity_key, set()).add(claim.id)

    claims_by_id = {claim.id: claim for claim in relevant}
    for entity_key, claim_ids in groups.items():
        present = {
            (claims_by_id[claim_id].subject, claims_by_id[claim_id].predicate)
            for claim_id in claim_ids
        }
        if not required_group_selectors.issubset(present):
            return "INCOMPLETE", sorted(relevant_ids), f"lifecycle entity {entity_key} is missing required Claim coverage"
        for selector_group in config.get("complete_selector_groups") or (group_selectors,):
            expected = {tuple(item) for item in selector_group}
            if present.intersection(expected) and not expected.issubset(present):
                return "INCOMPLETE", sorted(relevant_ids), f"lifecycle entity {entity_key} has incomplete Claim coverage"

    normalized_branch = _stable_value(branch)
    empty_values = {_stable_value(item) for item in config.get("empty_values") or ()}
    grouped_values = {_stable_value(item) for item in config.get("grouped_values") or ()}
    if normalized_branch in empty_values:
        if groups or len(proposals) != 1 or set(proposals[0].input_claim_ids) != global_ids:
            return "INCOMPLETE", sorted(relevant_ids), "empty lifecycle branch has entity Claims or incomplete Proposal coverage"
        verdict = proposals[0].verdict
        status = "PROVED" if verdict == "SUPPORTED" else "INCOMPLETE"
        return status, sorted(relevant_ids), f"empty lifecycle branch judgment is {verdict}"
    if normalized_branch not in grouped_values or not groups:
        return "INCOMPLETE", sorted(relevant_ids), "entity lifecycle branch has no complete entity groups"

    by_group: dict[str, list[ProofProposal]] = {}
    for proposal in proposals:
        keys = {
            claims_by_id[claim_id].entity_key
            for claim_id in proposal.input_claim_ids
            if claim_id in claims_by_id and claims_by_id[claim_id].entity_key
        }
        if len(keys) != 1:
            return "INCOMPLETE", sorted(relevant_ids), "lifecycle Proposal is not scoped to one entity"
        key = next(iter(keys))
        if set(proposal.input_claim_ids) != global_ids.union(groups.get(key, set())):
            return "INCOMPLETE", sorted(relevant_ids), f"lifecycle Proposal does not cover entity {key}"
        by_group.setdefault(key, []).append(proposal)
    if set(by_group) != set(groups) or any(len(items) != 1 for items in by_group.values()):
        return "INCOMPLETE", sorted(relevant_ids), "each lifecycle entity requires exactly one Proposal"
    verdicts = {key: items[0].verdict for key, items in by_group.items()}
    if "REFUTED" in verdicts.values():
        return "DISPROVED", sorted(relevant_ids), "at least one lifecycle entity refutes the target predicate"
    if set(verdicts.values()) == {"SUPPORTED"}:
        return "PROVED", sorted(relevant_ids), "all lifecycle entities support the target predicate"
    return "INCOMPLETE", sorted(relevant_ids), "at least one lifecycle entity remains unknown"


def _execute_entity_judgments(
    relevant: list[Claim],
    proposals: list[ProofProposal],
    config: Mapping[str, Any],
) -> tuple[str, list[str], str]:
    relevant_ids = {claim.id for claim in relevant}
    branch = _single_claim_value(relevant, config.get("branch_selector"))
    if branch is None:
        return "INCOMPLETE", sorted(relevant_ids), "entity judgment branch is missing or ambiguous"

    empty_value = config.get("empty_value")
    grouped_value = config.get("grouped_value")
    group_selectors = {tuple(selector) for selector in config.get("group_selectors") or ()}
    if _stable_value(branch) == _stable_value(empty_value):
        if any((claim.subject, claim.predicate) in group_selectors for claim in relevant):
            return "INCOMPLETE", sorted(relevant_ids), "empty entity branch contains candidate Claims"
        if any(set(item.input_claim_ids) != relevant_ids for item in proposals):
            return "INCOMPLETE", sorted(relevant_ids), "semantic judgment does not cover the current Claim set"
        verdicts = {item.verdict for item in proposals}
        if verdicts != {str(config.get("empty_verdict") or "SUPPORTED")}:
            return "INCOMPLETE", sorted(relevant_ids), "empty entity branch has an incompatible semantic verdict"
        chosen = sorted(proposals, key=lambda item: item.model_dump_json())[0]
        return "PROVED", sorted(relevant_ids), f"LLM judgment SUPPORTED: {chosen.reason or 'no narrative reason supplied'}"
    if _stable_value(branch) != _stable_value(grouped_value):
        return "INCOMPLETE", sorted(relevant_ids), "entity judgment branch value is unresolved"

    groups: dict[str, list[Claim]] = {}
    for claim in relevant:
        is_group_claim = (claim.subject, claim.predicate) in group_selectors
        if is_group_claim and not claim.entity_key:
            return "INCOMPLETE", sorted(relevant_ids), f"candidate Claim {claim.id} has no entity_key"
        if not is_group_claim and claim.entity_key:
            return "INCOMPLETE", sorted(relevant_ids), f"global Claim {claim.id} unexpectedly has an entity_key"
        if claim.entity_key:
            groups.setdefault(claim.entity_key, []).append(claim)
    if not groups:
        return "INCOMPLETE", sorted(relevant_ids), "candidate search has no entity-keyed Claim group"

    anchor_selector = tuple(config.get("anchor_selector") or ())
    for entity_key, group in groups.items():
        if not any((claim.subject, claim.predicate) == anchor_selector for claim in group):
            return "INCOMPLETE", sorted(relevant_ids), f"candidate group {entity_key} has no anchor Claim"

    proposals_by_entity: dict[str, list[ProofProposal]] = {}
    claims_by_id = {claim.id: claim for claim in relevant}
    for proposal in proposals:
        proposal_claims = [claims_by_id[claim_id] for claim_id in proposal.input_claim_ids if claim_id in claims_by_id]
        entity_keys = {claim.entity_key for claim in proposal_claims}
        if len(entity_keys) != 1 or None in entity_keys:
            return "INCOMPLETE", sorted(relevant_ids), "semantic judgment is not scoped to exactly one entity group"
        entity_key = next(iter(entity_keys))
        group_ids = {claim.id for claim in groups.get(entity_key, [])}
        if set(proposal.input_claim_ids) != group_ids:
            return "INCOMPLETE", sorted(relevant_ids), f"semantic judgment does not cover entity group {entity_key}"
        proposals_by_entity.setdefault(entity_key, []).append(proposal)

    if set(proposals_by_entity) != set(groups) or any(len(items) != 1 for items in proposals_by_entity.values()):
        return "INCOMPLETE", sorted(relevant_ids), "each entity group requires exactly one semantic judgment"

    verdict_options = config.get("verdict_value_options") or {}
    verdict_equalities = config.get("verdict_equalities") or {}
    verdicts: dict[str, str] = {}
    for entity_key, group in groups.items():
        proposal = proposals_by_entity[entity_key][0]
        verdict = proposal.verdict
        options = verdict_options.get(verdict, ())
        if options and not any(_constraints_match(group, option) for option in options):
            return "INCOMPLETE", sorted(relevant_ids), f"semantic judgment for {entity_key} matches no allowed Claim-value option"
        equalities = verdict_equalities.get(verdict, ())
        if equalities and not _equalities_match(group, equalities):
            return "INCOMPLETE", sorted(relevant_ids), f"semantic judgment for {entity_key} has an invalid entity relation"
        verdicts[entity_key] = verdict

    if "REFUTED" in verdicts.values():
        status = "DISPROVED"
    elif "UNKNOWN" in verdicts.values():
        status = "INCOMPLETE"
    else:
        status = "PROVED"
    summary = ", ".join(f"{key}={verdicts[key]}" for key in sorted(verdicts))
    return status, sorted(relevant_ids), f"entity LLM judgments: {summary}"


def _single_claim_value(relevant: list[Claim], selector: Any) -> Any | None:
    if not isinstance(selector, (list, tuple)) or len(selector) != 2:
        return None
    values = {
        _stable_value(claim.typed_value): claim.typed_value
        for claim in relevant
        if (claim.subject, claim.predicate) == tuple(selector)
    }
    return next(iter(values.values())) if len(values) == 1 else None


def _equalities_match(relevant: list[Claim], equalities: Any) -> bool:
    for equality in equalities:
        values: set[str] = set()
        for subject, predicate in equality:
            matches = [
                claim.typed_value
                for claim in relevant
                if claim.subject == subject and claim.predicate == predicate
            ]
            distinct = {_stable_value(value) for value in matches}
            if len(distinct) != 1:
                return False
            values.update(distinct)
        if len(values) != 1:
            return False
    return True


def _constraints_match(relevant: list[Claim], constraints: Any) -> bool:
    for subject, predicate, allowed_values in constraints:
        constrained = [
            claim for claim in relevant if claim.subject == subject and claim.predicate == predicate
        ]
        allowed = {_stable_value(value) for value in allowed_values}
        if not constrained or any(_stable_value(claim.typed_value) not in allowed for claim in constrained):
            return False
    return True


def _selector_value(selector: ClaimSelector, claims: list[Claim], attribute: str = "") -> tuple[Any | None, list[str], str]:
    matches = _matching_claims(selector, claims)
    if not matches:
        return None, [], f"missing claim {selector.subject}:{selector.predicate}"
    values = [claim.attributes.get(attribute) if attribute else claim.typed_value for claim in matches]
    if attribute and any(value in {None, ""} or attribute not in claim.attribute_sources for value, claim in zip(values, matches)):
        return None, [claim.id for claim in matches], f"missing source-linked {attribute}"
    distinct = {
        _decimal(value) if not attribute and claim.value_type == "decimal" else _stable_value(value)
        for value, claim in zip(values, matches)
    }
    if len(distinct) != 1:
        return None, [claim.id for claim in matches], f"ambiguous claim {selector.subject}:{selector.predicate}{'.' + attribute if attribute else ''}"
    return values[0], [claim.id for claim in matches], ""


def _matching_claims(selector: ClaimSelector, claims: list[Claim]) -> list[Claim]:
    return [claim for claim in claims if claim.subject == selector.subject and claim.predicate == selector.predicate]


def _obligation(node: NodeSpec, contract: RequirementContract | None = None) -> ProofObligation:
    obligation_id = node.obligation_id or f"OBL_{node.id.removeprefix('CHK_').removeprefix('JDG_')}"
    action_kinds = contract.candidate_actions if contract and contract.candidate_actions else ["review_or_request_evidence"]
    return ProofObligation(
        id=obligation_id,
        check_id=node.id,
        missing_premise=node.missing_premise,
        candidate_actions=[
            VerificationActionHint(id=f"ACT_{kind.upper()}", kind=kind, target=node.missing_premise)
            for kind in action_kinds
        ],
    )


def _obligation_priority(obligation: ProofObligation) -> float:
    if not obligation.blocking:
        return 0.0
    level = {"low": 1, "medium": 2, "high": 3}
    action_value = max(
        (level[action.resolvability] / level[action.cost] for action in obligation.candidate_actions),
        default=1.0,
    )
    return float(level[obligation.decision_impact] * level[obligation.uncertainty] * action_value)


def _aggregate(statuses: list[str]) -> str:
    if not statuses:
        raise ValueError("Aggregate proof node requires at least one dependency")
    if "DISPROVED" in statuses:
        return "DISPROVED"
    if "INCOMPLETE" in statuses:
        return "INCOMPLETE"
    return "NOT_APPLICABLE" if "NOT_APPLICABLE" in statuses else "PROVED"


def _snapshot_hash(
    spec: SemanticGraphSpec,
    claims: list[Claim],
    *,
    policy_version: str,
    compiler_version: str,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
    proposals: list[ProofProposal],
    contracts: list[RequirementContract],
    source_bindings: list[SourceBinding],
) -> str:
    claim_rows = [item.model_dump(mode="json") for item in claims]
    payload = {
        "graph": asdict(spec),
        "claims": sorted(claim_rows, key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True)),
        "proposals": sorted(
            (_canonical_proposal_row(item) for item in proposals),
            key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True),
        ),
        "contracts": sorted(
            (item.model_dump(mode="json") for item in contracts),
            key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True),
        ),
        "source_bindings": sorted(
            (item.model_dump(mode="json") for item in source_bindings),
            key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True),
        ),
        "sources": _source_fingerprints(
            trusted_sources,
            {claim.evidence_id for claim in claims},
        ),
        "policy_version": policy_version,
        "compiler_version": compiler_version,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _canonical_proposal_row(item: ProofProposal) -> dict[str, Any]:
    row = item.model_dump(
        mode="json",
        exclude={"id", "reason", "evidence_snapshot_hash"},
    )
    for key in ("input_refs", "supporting_refs", "opposing_refs"):
        row[key] = sorted(
            row.get(key) or [],
            key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True),
        )
    row["open_questions"] = sorted(set(row.get("open_questions") or []))
    row["validation_errors"] = sorted(set(row.get("validation_errors") or []))
    return row


def _admissible(item: EvidenceItem, binding: ClaimBinding) -> bool:
    return (
        _base_admissible(item)
        and str(item.type) in binding.evidence_types
        and any(support.requirement == binding.support_requirement and support.support_level != "none" for support in item.supports)
    )


def _base_admissible(item: EvidenceItem) -> bool:
    return item.source == "attachment" and _source_accepted(item)


def _source_accepted(item: EvidenceItem) -> bool:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    review_result = item.review_result if isinstance(item.review_result, dict) else {}
    classification = str(
        metadata.get("classification")
        or ("business_evidence" if item.source == "user_message" else "")
    ).lower()
    boundary = str(metadata.get("boundary") or review_result.get("boundary") or "").lower()
    truth_status = str(metadata.get("truth_status") or review_result.get("truth_status") or "").lower()
    source_ref = str(metadata.get("source_ref") or review_result.get("source_ref") or "").lower()
    return (
        item.credibility != "low"
        and review_result.get("should_accept") is True
        and classification == "business_evidence"
        and boundary != "memory_hint_only_not_case_truth"
        and truth_status != "advisory"
        and "memory_hint_only_not_case_truth" not in source_ref
    )


def _quote_grounded(
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
    evidence_id: str,
    quote: str,
) -> bool:
    expected = " ".join(quote.split())
    source = trusted_sources.get(evidence_id) if trusted_sources else None
    if (
        not isinstance(source, Mapping)
        or not str(source.get("sha256") or "")
        or not str(source.get("original_ref") or "")
    ):
        return False
    texts = source.get("texts") if isinstance(source, Mapping) else None
    candidates = [texts] if isinstance(texts, str) else texts if isinstance(texts, (list, tuple)) else []
    return bool(expected) and any(expected in " ".join(str(text or "").split()) for text in candidates)


def _source_fingerprints(
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
    evidence_ids: set[str],
) -> list[dict[str, str]]:
    if not trusted_sources:
        return []
    return [
        {
            "evidence_id": str(evidence_id),
            "sha256": str(source.get("sha256") or ""),
            "original_ref": str(source.get("original_ref") or ""),
            "extraction_sha256": str(source.get("extraction_sha256") or ""),
        }
        for evidence_id, source in sorted(trusted_sources.items(), key=lambda item: str(item[0]))
        if evidence_id in evidence_ids and isinstance(source, Mapping)
    ]


def _typed_value(value: Any, value_type: str, allowed_values: tuple[str, ...]) -> Any | None:
    if value_type == "decimal":
        return _decimal(value)
    text = str(value or "").strip()
    if not text:
        return None
    if value_type == "currency":
        upper = text.upper()
        currencies = {
            match.group(0)
            for match in re.finditer(r"\b[A-Z]{3}\b", upper)
            if match.group(0) in ISO_4217_CURRENCY_CODES
        }
        currencies.update(
            code
            for symbol, code in CURRENCY_SYMBOLS
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}", text, flags=re.IGNORECASE)
        )
        return next(iter(currencies)) if len(currencies) == 1 else None
    if value_type == "enum":
        text = text.lower()
        return text if not allowed_values or text in allowed_values else None
    return text


def _generic_typed_value(value: Any, value_type: str) -> Any | None:
    if value_type == "integer":
        decimal = _decimal(value)
        return int(decimal) if decimal is not None and decimal == decimal.to_integral_value() else None
    if value_type == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        return {"true": True, "false": False}.get(normalized)
    return _typed_value(value, value_type, ())


def _generic_value_grounded(quote: str, value: Any, value_type: str) -> bool:
    if value_type in {"decimal", "integer"}:
        return bool(_matching_decimal_quote([quote], value))
    if value_type == "currency":
        return _typed_value(quote, "currency", ()) == value
    if value_type == "enum":
        return bool(quote.strip())
    expected = re.sub(r"[^\w]+", " ", str(value).replace("_", " "), flags=re.UNICODE).strip()
    actual = re.sub(r"[^\w]+", " ", quote.replace("_", " "), flags=re.UNICODE)
    return bool(expected) and re.search(rf"(?<!\w){re.escape(expected)}(?!\w)", actual, flags=re.IGNORECASE) is not None


def _decimal(value: Any) -> Decimal | None:
    text = re.sub(r"\s+", "", str(value or "")).strip()
    text = re.sub(r"[^0-9,.-]", "", text)
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".") if len(text.rsplit(",", 1)[1]) <= 2 else text.replace(",", "")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _matching_decimal_quote(quotes: list[str], value: Any) -> str:
    amount = _decimal(value)
    if amount is None:
        return ""
    for quote in quotes:
        if any(_decimal(token) == amount for token in re.findall(r"[+-]?\d(?:[\d\s,.]*\d)?", str(quote or ""))):
            return str(quote)
    return ""


def _quote_contains_value(quote: str, value: Any) -> bool:
    identity = str(value).split(":", 1)[-1]
    parts = re.findall(r"[^\W_]+", identity, flags=re.UNICODE)
    normalized = "".join(parts)
    if len(normalized) < 3 or not any(character.isdigit() for character in normalized):
        return False
    pattern = r"(?<!\w)" + r"[\W_]*".join(re.escape(part) for part in parts) + r"(?!\w)"
    return re.search(pattern, quote, flags=re.IGNORECASE | re.UNICODE) is not None


def _variance(observed: Decimal, reference: Decimal) -> Decimal:
    return Decimal("Infinity") if reference == 0 and observed else (Decimal("0") if reference == 0 else abs(observed - reference) / abs(reference) * 100)


def _value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) else value


def _attr(value: Any, key: str) -> str:
    return str(value.get(key) or "").strip() if isinstance(value, dict) else ""


def _source_quote(value: Any) -> str:
    return _attr(value, "source_quote") or _attr(value, "quote")


def _source_locator(value: Any) -> str:
    return _attr(value, "source_locator") or _attr(value, "locator")


def _explicitly_unusable(value: Any) -> bool:
    status = str(value.get("status") or "").lower() if isinstance(value, dict) else ""
    return bool(status) and status != "present"


def _stable_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    payload = value if isinstance(value, str) else json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strings(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value] if value else []
    return [str(item).strip() for item in items if str(item).strip()]


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value is not None and str(value)))


def _dedupe_models(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = value.model_dump_json(exclude_none=True)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


__all__ = [
    "AttributeBinding",
    "ClaimBinding",
    "ClaimSelector",
    "NodeSpec",
    "ProgramCompilation",
    "SemanticGraphSpec",
    "compile_proof_graph",
    "lower_case_evidence_ir",
]
