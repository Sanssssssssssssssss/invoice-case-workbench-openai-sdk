from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from graphlib import CycleError, TopologicalSorter
from typing import Any, Literal, Mapping

from app.state.schemas import (
    CheckResult,
    Claim,
    ClaimSource,
    DecisionProof,
    EvidenceItem,
    ProofObligation,
    SemanticJudgment,
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
    operator: Literal["present", "same", "allowed", "allowed_by_subject", "within_percent", "llm_judgment", "all"]
    selectors: tuple[ClaimSelector, ...] = ()
    depends_on: tuple[str, ...] = ()
    attribute: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    executor: Literal["llm", "deterministic"] = "deterministic"
    judgment_id: str = ""
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
    judgments: list[SemanticJudgment]
    checks: list[CheckResult]
    obligations: list[ProofObligation]
    decision: DecisionProof


def compile_proof_graph(
    spec: SemanticGraphSpec,
    evidence_items: list[EvidenceItem],
    *,
    policy_version: str,
    compiler_version: str,
    verification_records: list[VerificationRecord] | None = None,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> ProgramCompilation:
    evidence_id_counts = Counter(item.id for item in evidence_items)
    evidence_items = [item for item in evidence_items if evidence_id_counts[item.id] == 1]
    order = _graph_order(spec)
    claims = _claims(spec, evidence_items, trusted_sources)
    judgments = _judgments(spec, evidence_items, claims)
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
            reason = "result follows from declared dependency checks"
        elif any(item.status == "DISPROVED" for item in dependencies):
            status, claim_ids, reason = "NOT_APPLICABLE", [], "a required dependency was disproved"
        elif any(item.status != "PROVED" for item in dependencies):
            status, claim_ids, reason = "INCOMPLETE", [], "required dependencies are incomplete"
        else:
            status, claim_ids, reason = _execute(node, claims, judgments)
        check = CheckResult(
            id=node.id,
            program_id=spec.id,
            requirement_id=node.requirement_id,
            status=status,
            input_claim_ids=claim_ids,
            depends_on_check_ids=list(node.depends_on),
            rule_id=f"{spec.id}:{node.id}",
            reason=reason,
            executor=node.executor,
            operator=node.operator,
        )
        checks.append(check)
        by_id[node.id] = check
        if status == "INCOMPLETE" and node.missing_premise and all(item.status == "PROVED" for item in dependencies):
            obligations.append(_obligation(node))

    root = by_id[spec.root_id]
    if root.status == "DISPROVED":
        for obligation in obligations:
            obligation.blocking = False
    snapshot_hash = _snapshot_hash(
        spec,
        claims,
        judgments,
        policy_version=policy_version,
        compiler_version=compiler_version,
        trusted_sources=trusted_sources,
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
    return ProgramCompilation(claims, judgments, checks, obligations, decision)


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
    claims: list[Claim] = []
    declared_ids = Counter(
        str(row.get("id") or "").strip()
        for item in items
        if _base_admissible(item) and isinstance(item.metadata, dict)
        for row in item.metadata.get("claim_to_source_refs") or []
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    )
    for binding in spec.claim_bindings:
        for item in sorted(items, key=lambda row: row.id):
            if not _admissible(item, binding):
                continue
            if binding.source_kind == "extracted_field":
                claim = _field_claim(binding, item, trusted_sources)
                if claim:
                    claims.append(claim)
            else:
                claims.extend(_semantic_claims(binding, item, trusted_sources, declared_ids))
    result = _dedupe_models(claims)
    counts = Counter(claim.id for claim in result)
    return [claim for claim in result if counts[claim.id] == 1]


def _field_claim(
    binding: ClaimBinding,
    item: EvidenceItem,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
) -> Claim | None:
    fields = item.metadata.get("extracted_fields") if isinstance(item.metadata, dict) else None
    if not isinstance(fields, dict):
        return None
    candidates = [
        (fields[key], _typed_value(_value(fields[key]), binding.value_type, binding.allowed_values))
        for key in binding.field_aliases
        if key in fields
    ]
    values = {_stable_value(value) for _, value in candidates if value is not None}
    if len(values) != 1:
        return None
    raw, typed_value = next((field, value) for field, value in candidates if value is not None)
    quote = _source_quote(raw)
    if binding.quote_must_contain_value:
        quote = _matching_decimal_quote([quote], typed_value)
    locator = _source_locator(raw) or str(_value(fields.get("source_locator")) or "").strip()
    confidence = _attr(raw, "confidence").lower()
    if (
        not quote
        or not _quote_grounded(trusted_sources, item.id, quote)
        or not locator
        or confidence not in {"medium", "high"}
        or _explicitly_unusable(raw)
    ):
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


def _semantic_claims(
    binding: ClaimBinding,
    item: EvidenceItem,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
    declared_ids: Counter[str],
) -> list[Claim]:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    result: list[Claim] = []
    for row in metadata.get("claim_to_source_refs") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("subject") or "") != binding.subject or str(row.get("predicate") or "") != binding.predicate:
            continue
        typed_value = _typed_value(row.get("typed_value", row.get("value")), binding.value_type, binding.allowed_values)
        quote = str(row.get("source_quote") or row.get("quote") or "").strip()
        locator = str(row.get("source_locator") or row.get("locator") or row.get("block_or_table_or_region") or "").strip()
        confidence = str(row.get("confidence") or "").lower()
        claim_id = str(row.get("id") or "").strip()
        entity_key = str(row.get("entity_key") or "").strip() or None
        if claim_id and not re.fullmatch(r"CLM_[A-Za-z0-9_.:-]{1,120}", claim_id):
            continue
        if claim_id and declared_ids[claim_id] != 1:
            continue
        if entity_key and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", entity_key):
            continue
        if (
            typed_value is None
            or not quote
            or not _quote_grounded(trusted_sources, item.id, quote)
            or (binding.quote_must_contain_value and not _quote_contains_value(quote, typed_value))
            or not locator
            or confidence not in {"medium", "high"}
            or _explicitly_unusable(row)
        ):
            continue
        result.append(
            _claim(
                binding,
                item,
                typed_value,
                quote,
                locator,
                confidence,
                {},
                {},
                claim_id=claim_id,
                entity_key=entity_key,
            )
        )
    return result


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
        unit="money" if binding.value_type == "decimal" else None,
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


def _judgments(spec: SemanticGraphSpec, items: list[EvidenceItem], claims: list[Claim]) -> list[SemanticJudgment]:
    allowed_ids = {node.judgment_id for node in spec.nodes if node.judgment_id}
    active_evidence_ids = {item.id for item in items if _base_admissible(item)}
    result: list[SemanticJudgment] = []
    for item in sorted(items, key=lambda row: row.id):
        if not _base_admissible(item):
            continue
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        for raw in metadata.get("semantic_judgments") or []:
            if not isinstance(raw, dict) or str(raw.get("id") or "") not in allowed_ids:
                continue
            result.append(_judgment(raw, item.id, claims, active_evidence_ids))
    return _dedupe_models(result)


def _judgment(
    raw: dict[str, Any],
    carrier_id: str,
    claims: list[Claim],
    active_evidence_ids: set[str],
) -> SemanticJudgment:
    errors: list[str] = []
    input_ids, stale_input = _resolve_refs(
        raw.get("input_refs") or raw.get("considered_refs"),
        claims,
        active_evidence_ids,
        errors,
        "input",
    )
    supporting_ids, _ = _resolve_refs(raw.get("supporting_refs"), claims, active_evidence_ids, errors, "supporting")
    opposing_ids, _ = _resolve_refs(raw.get("opposing_refs"), claims, active_evidence_ids, errors, "opposing")
    verdict = str(raw.get("verdict") or "UNKNOWN").upper()
    if verdict not in {"SUPPORTED", "REFUTED", "UNKNOWN"}:
        errors.append("invalid verdict")
        verdict = "UNKNOWN"
    if not input_ids:
        errors.append("no resolved input claims")
    if not set(supporting_ids + opposing_ids) <= set(input_ids):
        errors.append("supporting/opposing refs must be input refs")
    open_questions = _strings(raw.get("open_questions"))
    confidence = str(raw.get("confidence") or "low").lower()
    if confidence not in {"low", "medium", "high"}:
        errors.append("invalid confidence")
        confidence = "low"
    if verdict in {"SUPPORTED", "REFUTED"} and open_questions:
        errors.append(f"{verdict} requires no open questions")
    if verdict == "SUPPORTED" and (not supporting_ids or opposing_ids):
        errors.append("SUPPORTED requires supporting refs and no opposing refs")
    if verdict == "REFUTED" and not opposing_ids:
        errors.append("REFUTED requires opposing refs")
    if stale_input:
        errors = ["stale input Claim reference"]
    return SemanticJudgment(
        id=str(raw.get("id") or ""),
        verdict=verdict,
        input_claim_ids=input_ids,
        supporting_claim_ids=supporting_ids,
        opposing_claim_ids=opposing_ids,
        open_questions=open_questions,
        confidence=confidence,
        reason=str(raw.get("reason") or "").strip(),
        proposed_by_evidence_id=carrier_id,
        valid=not errors,
        validation_errors=errors,
    )


def _resolve_refs(
    raw_refs: Any,
    claims: list[Claim],
    active_evidence_ids: set[str],
    errors: list[str],
    label: str,
) -> tuple[list[str], bool]:
    result: list[str] = []
    referenced_evidence_ids: list[str] = []
    inactive_input_errors: list[str] = []
    for raw in raw_refs if isinstance(raw_refs, list) else []:
        if not isinstance(raw, dict):
            errors.append(f"invalid {label} ref")
            continue
        evidence_id = str(raw.get("evidence_id") or "")
        claim_id = str(raw.get("claim_id") or "")
        subject = str(raw.get("subject") or "")
        predicate = str(raw.get("predicate") or raw.get("field") or "")
        if not evidence_id:
            errors.append(f"missing evidence_id in {label} ref")
            continue
        referenced_evidence_ids.append(evidence_id)
        if evidence_id not in active_evidence_ids:
            error = f"unresolved {label} ref {evidence_id}:{claim_id or subject}:{predicate}"
            (inactive_input_errors if label == "input" else errors).append(error)
            continue
        matches = [
            claim
            for claim in claims
            if claim.evidence_id == evidence_id
            and (not claim_id or claim.id == claim_id)
            and (not subject or claim.subject == subject)
            and (not predicate or claim.predicate == predicate)
        ]
        if len(matches) != 1:
            errors.append(f"unresolved {label} ref {evidence_id}:{claim_id or subject}:{predicate}")
            continue
        result.append(matches[0].id)
    stale = (
        label == "input"
        and bool(referenced_evidence_ids)
        and all(evidence_id not in active_evidence_ids for evidence_id in referenced_evidence_ids)
    )
    if not stale:
        errors.extend(inactive_input_errors)
    return sorted(set(result)), stale


def _execute(node: NodeSpec, claims: list[Claim], judgments: list[SemanticJudgment]) -> tuple[str, list[str], str]:
    if node.operator == "present":
        value, claim_ids, error = _selector_value(node.selectors[0], claims)
        return ("INCOMPLETE", claim_ids, error) if error else ("PROVED", claim_ids, f"claim value is present: {value}")
    if node.operator in {"same", "allowed", "allowed_by_subject", "within_percent"}:
        return _execute_values(node, claims)
    if node.operator == "llm_judgment":
        return _execute_judgment(node, claims, judgments)
    raise ValueError(f"Unsupported proof operator: {node.operator}")


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


def _execute_judgment(node: NodeSpec, claims: list[Claim], judgments: list[SemanticJudgment]) -> tuple[str, list[str], str]:
    relevant: list[Claim] = []
    for selector in node.selectors:
        matches = _matching_claims(selector, claims)
        if selector.required and not matches:
            return "INCOMPLETE", [claim.id for claim in relevant], f"missing semantic claim {selector.subject}:{selector.predicate}"
        relevant.extend(matches)
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
        if item.id == node.judgment_id and "stale input Claim reference" not in item.validation_errors
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


def _execute_entity_judgments(
    relevant: list[Claim],
    proposals: list[SemanticJudgment],
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

    proposals_by_entity: dict[str, list[SemanticJudgment]] = {}
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


def _obligation(node: NodeSpec) -> ProofObligation:
    obligation_id = node.obligation_id or f"OBL_{node.id.removeprefix('CHK_').removeprefix('JDG_')}"
    return ProofObligation(
        id=obligation_id,
        check_id=node.id,
        missing_premise=node.missing_premise,
        candidate_actions=[VerificationActionHint(id=f"ACT_{obligation_id.removeprefix('OBL_')}", kind="review_or_request_evidence", target=node.missing_premise)],
        priority_shadow=27.0,
    )


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
    judgments: list[SemanticJudgment],
    *,
    policy_version: str,
    compiler_version: str,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None,
) -> str:
    claim_rows = [item.model_dump(mode="json") for item in claims]
    judgment_rows = [_canonical_judgment_row(item) for item in judgments]
    payload = {
        "graph": asdict(spec),
        "claims": sorted(claim_rows, key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True)),
        "judgments": sorted(judgment_rows, key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True)),
        "sources": _source_fingerprints(
            trusted_sources,
            {claim.evidence_id for claim in claims},
        ),
        "policy_version": policy_version,
        "compiler_version": compiler_version,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _canonical_judgment_row(item: SemanticJudgment) -> dict[str, Any]:
    row = item.model_dump(mode="json", exclude={"reason"})
    for key in (
        "input_claim_ids",
        "supporting_claim_ids",
        "opposing_claim_ids",
        "open_questions",
        "validation_errors",
    ):
        row[key] = sorted(set(str(value) for value in row.get(key) or []))
    return row


def _admissible(item: EvidenceItem, binding: ClaimBinding) -> bool:
    return (
        _base_admissible(item)
        and str(item.type) in binding.evidence_types
        and any(support.requirement == binding.support_requirement and support.support_level != "none" for support in item.supports)
    )


def _base_admissible(item: EvidenceItem) -> bool:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    review_result = item.review_result if isinstance(item.review_result, dict) else {}
    classification = str(metadata.get("classification") or "").lower()
    boundary = str(metadata.get("boundary") or review_result.get("boundary") or "").lower()
    truth_status = str(metadata.get("truth_status") or review_result.get("truth_status") or "").lower()
    source_ref = str(metadata.get("source_ref") or review_result.get("source_ref") or "").lower()
    return (
        item.source == "attachment"
        and item.credibility != "low"
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


def _strings(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value] if value else []
    return [str(item).strip() for item in items if str(item).strip()]


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


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
]
