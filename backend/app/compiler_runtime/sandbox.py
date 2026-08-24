from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Iterator, Mapping, Sequence

from pydantic import ValidationError

from .models import Claim, EvidenceIR
from .proof_terms import (
    CalculationOperation,
    CalculationRequest,
    CalculationWitness,
    ProofTermError,
    ProofTermRef,
    SemanticBindingProposal,
    compute_witness,
)


_LINE_LOCATOR = re.compile(r"\blines?\s+(\d+)(?:\s*[-:]\s*(\d+))?\b", re.IGNORECASE)
_PAGE_TEXT_LOCATOR = re.compile(
    r"\bpage\s+(\d+)(?:\s+(?:text|body(?:\s+text)?))?\s*$",
    re.IGNORECASE,
)
_PAGE_NUMBER_LOCATOR = re.compile(r"\bpage\s+(\d+)\b", re.IGNORECASE)
_NUMERIC_VALUE = re.compile(r"^[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)\s*%?$")
_QUOTE_NUMBER = re.compile(
    r"(?<![\w])(?:[-+]\s*(?:(?:[A-Z]{3}|[$€£¥])\s*)?)?(?:\(\s*)?"
    r"(?:\d{1,3}(?:[ '’]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)*)"
    r"(?:\s*\))?\s*%?(?![\w])",
    re.UNICODE,
)
_NUMERIC_OBSERVATION_REPAIR = (
    "Re-bind the Claim with a canonical Decimal string for the localized number "
    "actually printed in its quote; do not use a JSON float."
)
_PERCENT_NUMERIC_OBSERVATION_REPAIR = (
    "Re-bind the Claim with a canonical Decimal factor string matching the printed "
    "percentage (for example, printed 20% -> Claim value string '0.20'; JSON float "
    "0.2, value 20, and string '20%' are invalid for that example)."
)


def locator_supports_quote(content: str, *, locator: str, quote: str) -> bool:
    """Return whether a persisted locator resolves to the quoted source text."""
    if not content or not locator or not quote:
        return False
    page_match = _PAGE_TEXT_LOCATOR.search(locator)
    if page_match:
        marker = re.search(
            rf"\[page\s+{re.escape(page_match.group(1))}\s+text\]",
            content,
            re.IGNORECASE,
        )
        if marker:
            next_page = re.search(r"\[page\s+\d+\s+text\]", content[marker.end() :], re.IGNORECASE)
            end = marker.end() + next_page.start() if next_page else len(content)
            return quote in content[marker.end() : end]
    locator_positions = [match.start() for match in re.finditer(re.escape(locator), content)]
    quote_positions = [match.start() for match in re.finditer(re.escape(quote), content)]
    return any(
        len(content[min(locator_pos, quote_pos) : max(locator_pos + len(locator), quote_pos + len(quote))]) <= 1200
        and content[min(locator_pos, quote_pos) : max(locator_pos + len(locator), quote_pos + len(quote))].count("\n") <= 6
        for locator_pos in locator_positions
        for quote_pos in quote_positions
    )


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """A run-local source. Content is exposed only after ``read_source``."""

    source_id: str
    content: str
    title: str = ""
    kind: str = "unknown"
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source_id = self.source_id.strip()
        if not source_id:
            raise ValueError("source_id must not be blank")
        if not isinstance(self.content, str):
            raise TypeError("source content must be text")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "kind", self.kind.strip() or "unknown")
        object.__setattr__(self, "provenance", dict(self.provenance))

    @property
    def provenance_text(self) -> str:
        # Keep the same order that read_source exposes, so a copied multi-field
        # quote is checked against the exact text the Worker saw.
        return json.dumps(self.provenance, ensure_ascii=False, default=str)


@dataclass(frozen=True, slots=True)
class CheckSubmission:
    submission_id: str
    check_id: str
    claim_ids: tuple[str, ...]
    binding_ids: tuple[str, ...] = ()
    witness_ids: tuple[str, ...] = ()
    note: str = ""


class EvidenceSandbox:
    """Small, in-memory capability boundary for one evidence review run.

    The sandbox deliberately has no file, shell, Python, policy, or CaseStore
    capability. Invalid model actions return repairable results and leave the
    accepted IR and check submissions unchanged.
    """

    capability_names = (
        "list_sources",
        "read_source",
        "bind_claim",
        "compute_witness",
        "submit_check",
    )

    def __init__(
        self,
        *,
        sources: Iterable[SourceRecord],
        allowed_check_ids: Iterable[str],
        allowed_check_facets: Mapping[str, Iterable[str]] | None = None,
        allowed_check_policy_refs: Mapping[str, Iterable[str]] | None = None,
        policy_values: Mapping[str, Any] | None = None,
        policy_snapshot_hash: str = "",
        evidence_ir: EvidenceIR | None = None,
    ) -> None:
        source_rows = list(sources)
        self._sources = {row.source_id: row for row in source_rows}
        if len(self._sources) != len(source_rows):
            raise ValueError("source_id values must be unique")

        self._allowed_check_ids = frozenset(
            check_id.strip() for check_id in allowed_check_ids if check_id.strip()
        )
        self._allowed_check_facets = {
            check_id: frozenset(str(facet).strip() for facet in facets if str(facet).strip())
            for check_id, facets in (allowed_check_facets or {}).items()
            if check_id in self._allowed_check_ids
        }
        self._allowed_check_policy_refs = {
            check_id: frozenset(str(ref).strip() for ref in refs if str(ref).strip())
            for check_id, refs in (allowed_check_policy_refs or {}).items()
            if check_id in self._allowed_check_ids
        }
        self._policy_values = dict(policy_values or {})
        self._resolved_document_currencies: dict[str, str] = {}
        self._policy_snapshot_hash = policy_snapshot_hash.strip() or self._digest(
            {"policy_values": self._policy_values}
        )
        self._read_source_ids: set[str] = set()
        self._base_ir = evidence_ir or EvidenceIR()
        self._claims = [claim.model_copy(deep=True) for claim in self._base_ir.claims]
        self._claim_by_id: dict[str, Claim] = {}
        self._claim_by_fingerprint: dict[str, Claim] = {}
        for claim in self._claims:
            fingerprint = self._fingerprint_claim(
                subject=claim.subject,
                predicate=claim.predicate,
                value=claim.value,
                source_id=claim.source_id,
                quote=claim.quote,
                locator=claim.locator,
                attributes=claim.attributes,
            )
            if claim.id in self._claim_by_id and self._claim_by_id[claim.id] != claim:
                raise ValueError(f"conflicting seeded claim id: {claim.id}")
            self._claim_by_id[claim.id] = claim
            self._claim_by_fingerprint.setdefault(fingerprint, claim)

        self._submissions: list[CheckSubmission] = []
        self._submission_by_id: dict[str, CheckSubmission] = {}
        self._submission_by_fingerprint: dict[str, CheckSubmission] = {}
        self._binding_proposals: list[SemanticBindingProposal] = []
        self._binding_by_id: dict[str, SemanticBindingProposal] = {}
        self._witnesses: list[CalculationWitness] = []
        self._witness_by_id: dict[str, CalculationWitness] = {}
        self._focused_write_check_ids: frozenset[str] | None = None

    @property
    def evidence_ir(self) -> EvidenceIR:
        source_ids = sorted(set(self._base_ir.source_ids) | set(self._sources))
        return self._base_ir.model_copy(
            update={
                "source_ids": source_ids,
                "claims": [claim.model_copy(deep=True) for claim in self._claims],
            },
            deep=True,
        )

    @property
    def submissions(self) -> tuple[CheckSubmission, ...]:
        return tuple(self._submissions)

    @property
    def binding_proposals(self) -> tuple[SemanticBindingProposal, ...]:
        return tuple(item.model_copy(deep=True) for item in self._binding_proposals)

    @property
    def calculation_witnesses(self) -> tuple[CalculationWitness, ...]:
        return tuple(item.model_copy(deep=True) for item in self._witnesses)

    @property
    def resolved_policy_terms(self) -> dict[str, Any]:
        result = dict(self._policy_values)
        for ref_id, currency in self._resolved_document_currencies.items():
            raw = result.get(ref_id)
            if isinstance(raw, Mapping) and raw.get("unit") == "document_currency":
                result[ref_id] = {
                    "value": raw.get("value"),
                    "currency": currency,
                    "unit": "",
                }
        return result

    @property
    def read_source_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._read_source_ids))

    @property
    def source_records(self) -> tuple[SourceRecord, ...]:
        return tuple(self._sources[source_id] for source_id in sorted(self._sources))

    @contextmanager
    def focused_writes(self, check_ids: Iterable[str]) -> Iterator[None]:
        """Temporarily restrict CHECK-owned mutations to an explicit focus set."""

        focused = frozenset(
            str(check_id).strip() for check_id in check_ids if str(check_id).strip()
        )
        unknown = sorted(focused - self._allowed_check_ids)
        if unknown:
            raise ValueError(f"focused writes reference checks outside the ProofPlan: {unknown}")
        previous = self._focused_write_check_ids
        self._focused_write_check_ids = (
            focused if previous is None else frozenset(focused & previous)
        )
        try:
            yield
        finally:
            self._focused_write_check_ids = previous

    def discard_proof_material(
        self,
        *,
        claim_ids: Iterable[str] = (),
        binding_ids: Iterable[str] = (),
        witness_ids: Iterable[str] = (),
    ) -> None:
        """Discard proof terms already proven unreachable from every submission."""

        discarded_claim_ids = set(claim_ids)
        discarded_binding_ids = set(binding_ids)
        discarded_witness_ids = set(witness_ids)
        retained_bindings = [
            item for item in self._binding_proposals if item.id not in discarded_binding_ids
        ]
        retained_witnesses = [
            item for item in self._witnesses if item.id not in discarded_witness_ids
        ]

        referenced_claim_ids = {
            claim_id for submission in self._submissions for claim_id in submission.claim_ids
        }
        referenced_binding_ids = {
            binding_id for submission in self._submissions for binding_id in submission.binding_ids
        }
        referenced_witness_ids = {
            witness_id for submission in self._submissions for witness_id in submission.witness_ids
        }
        for binding in retained_bindings:
            referenced_claim_ids.update(
                ref.ref_id for ref in binding.term_refs if ref.kind == "CLAIM"
            )
            referenced_witness_ids.update(
                ref.ref_id for ref in binding.term_refs if ref.kind == "WITNESS"
            )
        for witness in retained_witnesses:
            referenced_claim_ids.update(
                operand.ref.ref_id
                for operand in witness.operands
                if operand.ref.kind == "CLAIM"
            )
            referenced_witness_ids.update(
                operand.ref.ref_id
                for operand in witness.operands
                if operand.ref.kind == "WITNESS"
            )
        if (
            discarded_claim_ids.intersection(referenced_claim_ids)
            or discarded_binding_ids.intersection(referenced_binding_ids)
            or discarded_witness_ids.intersection(referenced_witness_ids)
        ):
            raise ValueError("cannot discard proof material referenced by a retained term or submission")

        self._claims = [item for item in self._claims if item.id not in discarded_claim_ids]
        self._claim_by_id = {item.id: item for item in self._claims}
        self._claim_by_fingerprint = {}
        for claim in self._claims:
            fingerprint = self._fingerprint_claim(
                subject=claim.subject,
                predicate=claim.predicate,
                value=claim.value,
                source_id=claim.source_id,
                quote=claim.quote,
                locator=claim.locator,
                attributes=claim.attributes,
            )
            self._claim_by_fingerprint.setdefault(fingerprint, claim)

        self._binding_proposals = retained_bindings
        self._binding_by_id = {item.id: item for item in retained_bindings}
        self._witnesses = retained_witnesses
        self._witness_by_id = {item.id: item for item in retained_witnesses}
        self._resolved_document_currencies = {}
        for witness in retained_witnesses:
            for operand in witness.operands:
                raw_policy = self._policy_values.get(operand.ref.ref_id)
                if (
                    operand.ref.kind == "POLICY"
                    and isinstance(raw_policy, Mapping)
                    and raw_policy.get("unit") == "document_currency"
                    and operand.currency
                ):
                    self._resolved_document_currencies[operand.ref.ref_id] = operand.currency

    def list_sources(self) -> dict[str, Any]:
        sources = [
            {
                "source_id": source.source_id,
                "title": source.title,
                "kind": source.kind,
                "characters": len(source.content),
            }
            for source in sorted(self._sources.values(), key=lambda row: row.source_id)
        ]
        return self._success("list_sources", sources=sources)

    def read_source(self, source_id: str) -> dict[str, Any]:
        source_id = source_id.strip()
        source = self._sources.get(source_id)
        if source is None:
            return self._failure(
                "read_source",
                code="SOURCE_NOT_FOUND",
                message=f"Source {source_id!r} is not available in this run.",
                repair="Call list_sources and retry with one of its source_id values.",
                source_id=source_id,
            )

        self._read_source_ids.add(source_id)
        return self._success(
            "read_source",
            source={
                "source_id": source.source_id,
                "title": source.title,
                "kind": source.kind,
                "content": source.content,
                "system_provenance": dict(source.provenance),
            },
        )

    def bind_claim(
        self,
        *,
        subject: str,
        predicate: str,
        value: Any,
        source_id: str,
        quote: str,
        locator: str | int,
        confidence: str = "medium",
        claim_id: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_id = source_id.strip()
        source = self._sources.get(source_id)
        if source is None:
            return self._failure(
                "bind_claim",
                code="SOURCE_NOT_FOUND",
                message=f"Source {source_id!r} is not available in this run.",
                repair="Call list_sources and use a listed source_id.",
                source_id=source_id,
            )
        if source_id not in self._read_source_ids:
            return self._failure(
                "bind_claim",
                code="SOURCE_NOT_READ",
                message=f"Source {source_id!r} has not been read in this run.",
                repair=f"Call read_source with source_id={source_id!r}, then bind the claim.",
                source_id=source_id,
            )

        quote = quote.strip()
        if not quote:
            return self._failure(
                "bind_claim",
                code="QUOTE_REQUIRED",
                message="A claim must include a non-empty verbatim quote.",
                repair="Copy the shortest exact supporting text from read_source into quote.",
                source_id=source_id,
            )
        if quote not in source.content and quote not in source.provenance_text:
            return self._failure(
                "bind_claim",
                code="QUOTE_NOT_IN_SOURCE",
                message="The quote is not an exact substring of the selected source.",
                repair=(
                    "Read the source again and copy the quote exactly from content or "
                    "system_provenance, including case and spacing."
                ),
                source_id=source_id,
            )

        normalized_locator, locator_error = self._normalize_locator(locator, source.content)
        if locator_error is not None:
            return self._failure("bind_claim", source_id=source_id, **locator_error)
        if (
            quote in source.content
            and not _LINE_LOCATOR.search(normalized_locator)
            and not locator_supports_quote(
                source.content,
                locator=normalized_locator,
                quote=quote,
            )
        ):
            page_match = _PAGE_NUMBER_LOCATOR.search(normalized_locator)
            page_locator = f"page {page_match.group(1)} text" if page_match else ""
            if page_locator and locator_supports_quote(
                source.content,
                locator=page_locator,
                quote=quote,
            ):
                normalized_locator = page_locator
            else:
                return self._failure(
                    "bind_claim",
                    code="LOCATOR_QUOTE_MISMATCH",
                    message="The locator does not resolve to the quoted source text.",
                    repair="Use the page-text or block locator that contains the exact quote.",
                    source_id=source_id,
                )

        subject = subject.strip()
        predicate = predicate.strip()
        if not subject or not predicate:
            return self._failure(
                "bind_claim",
                code="CLAIM_SHAPE_INVALID",
                message="Claim subject and predicate must both be non-empty.",
                repair="Provide one concrete subject and one stable predicate.",
                source_id=source_id,
            )

        try:
            claim_attributes = dict(attributes or {})
        except (TypeError, ValueError):
            return self._failure(
                "bind_claim",
                code="CLAIM_SHAPE_INVALID",
                message="Claim attributes must be a JSON object.",
                repair="Provide attributes as key-value pairs, or omit attributes.",
                source_id=source_id,
            )

        # Fail at the observation boundary for values that are plainly intended
        # as numeric proof terms.  Text observations remain verifier-owned; this
        # gate deliberately does not infer numeric meaning from business labels.
        if self._has_numeric_intent(value) and not self._numeric_value_matches_quote(
            value,
            quote,
        ):
            return self._failure(
                "bind_claim",
                code="CLAIM_VALUE_NOT_OBSERVED",
                message=(
                    "Numeric Claim value does not match any localized number "
                    "in its exact source quote."
                ),
                repair=self._numeric_observation_repair(quote),
                source_id=source_id,
            )

        fingerprint = self._fingerprint_claim(
            subject=subject,
            predicate=predicate,
            value=value,
            source_id=source_id,
            quote=quote,
            locator=normalized_locator,
            attributes=claim_attributes,
        )
        duplicate = self._claim_by_fingerprint.get(fingerprint)
        if duplicate is not None:
            return self._success(
                "bind_claim",
                created=False,
                duplicate=True,
                claim=self._dump_model(duplicate),
            )

        claim_id = claim_id.strip() or f"claim_{fingerprint[:16]}"
        if claim_id in self._claim_by_id:
            return self._failure(
                "bind_claim",
                code="CLAIM_ID_CONFLICT",
                message=f"Claim id {claim_id!r} already identifies different content.",
                repair="Omit claim_id to receive a deterministic id, or provide a new unique id.",
                claim_id=claim_id,
            )

        try:
            claim = Claim(
                id=claim_id,
                subject=subject,
                predicate=predicate,
                value=value,
                source_id=source_id,
                quote=quote,
                locator=normalized_locator,
                confidence=confidence,
                attributes=claim_attributes,
            )
        except ValidationError as exc:
            return self._failure(
                "bind_claim",
                code="CLAIM_SHAPE_INVALID",
                message="The claim does not satisfy the Claim schema.",
                repair="Correct the fields identified in details and submit the claim again.",
                validation_errors=exc.errors(include_url=False, include_input=False),
            )

        self._claims.append(claim)
        self._claim_by_id[claim.id] = claim
        self._claim_by_fingerprint[fingerprint] = claim
        return self._success(
            "bind_claim",
            created=True,
            duplicate=False,
            claim=self._dump_model(claim),
        )

    def compute_witness(
        self,
        *,
        check_id: str,
        facet_ref: str,
        operation: CalculationOperation,
        refs: Sequence[ProofTermRef | Mapping[str, Any]],
    ) -> dict[str, Any]:
        check_id = check_id.strip()
        facet_ref = facet_ref.strip()
        write_scope_error = self._validate_focused_write(
            action="compute_witness",
            check_id=check_id,
        )
        if write_scope_error is not None:
            return write_scope_error
        scope_error = self._validate_check_facet(
            action="compute_witness",
            check_id=check_id,
            facet_ref=facet_ref,
        )
        if scope_error is not None:
            return scope_error

        try:
            operands = [
                item if isinstance(item, ProofTermRef) else ProofTermRef.model_validate(item)
                for item in refs
            ]
        except ValidationError as exc:
            return self._failure(
                "compute_witness",
                code="WITNESS_REFERENCE_INVALID",
                message="Witness refs must identify admitted CLAIM, prior WITNESS, or configured POLICY terms.",
                repair="Use only typed ids returned by bind_claim or compute_witness, or a configured CHECK policy ref.",
                validation_errors=exc.errors(include_url=False, include_input=False),
            )

        for ref in operands:
            if ref.kind == "CLAIM":
                claim = self._claim_by_id.get(ref.ref_id)
                if claim is None:
                    return self._unknown_proof_ref("compute_witness", ref)
                if not self._numeric_claim_matches_quote(claim):
                    return self._failure(
                        "compute_witness",
                        code="CLAIM_VALUE_NOT_OBSERVED",
                        message=(
                            f"Numeric Claim {claim.id!r} does not match any localized number "
                            "in its exact source quote."
                        ),
                        repair=self._numeric_observation_repair(claim.quote),
                        claim_id=claim.id,
                    )
            elif ref.kind == "WITNESS":
                if ref.ref_id not in self._witness_by_id:
                    return self._unknown_proof_ref("compute_witness", ref)
            else:
                allowed_policy_refs = self._allowed_check_policy_refs.get(check_id, frozenset())
                if ref.ref_id not in allowed_policy_refs or ref.ref_id not in self._policy_values:
                    return self._failure(
                        "compute_witness",
                        code="POLICY_REFERENCE_NOT_AVAILABLE",
                        message=f"Policy ref {ref.ref_id!r} is not configured for CHECK {check_id!r}.",
                        repair="Use a configured policy_ref declared on this CHECK, or leave the CHECK unresolved.",
                        check_id=check_id,
                        policy_ref=ref.ref_id,
                    )

        witness_seed = {
            "check_id": check_id,
            "facet_ref": facet_ref,
            "operation": operation,
            "refs": [item.model_dump(mode="json") for item in operands],
            "evidence_snapshot_hash": self.evidence_ir.source_snapshot_hash(),
            "policy_snapshot_hash": self._policy_snapshot_hash,
        }
        witness_id = f"witness_{self._digest(witness_seed)[:16]}"
        duplicate = self._witness_by_id.get(witness_id)
        if duplicate is not None:
            return self._success(
                "compute_witness",
                created=False,
                duplicate=True,
                witness=self._dump_model(duplicate),
            )

        try:
            policy_values, resolved_document_currencies = self._policy_values_for_request(operands)
            witness = compute_witness(
                CalculationRequest(
                    id=witness_id,
                    check_id=check_id,
                    facet_ref=facet_ref,
                    operation=operation,
                    operands=operands,
                ),
                claims=self._claim_by_id,
                witnesses=self._witness_by_id,
                policy_values=policy_values,
                evidence_snapshot_hash=self.evidence_ir.source_snapshot_hash(),
                policy_snapshot_hash=self._policy_snapshot_hash,
            )
        except (ProofTermError, ValidationError, InvalidOperation, ValueError, TypeError) as exc:
            return self._failure(
                "compute_witness",
                code="WITNESS_COMPUTATION_REJECTED",
                message=f"The deterministic Decimal engine rejected this witness: {exc}",
                repair="Check operand types, currency/unit consistency, operation arity, and configured policy refs.",
                check_id=check_id,
                facet_ref=facet_ref,
            )

        self._witnesses.append(witness)
        self._witness_by_id[witness.id] = witness
        self._resolved_document_currencies.update(resolved_document_currencies)
        return self._success(
            "compute_witness",
            created=True,
            duplicate=False,
            witness=self._dump_model(witness),
        )

    def submit_check(
        self,
        *,
        check_id: str,
        claim_ids: Sequence[str] = (),
        binding_proposals: Sequence[SemanticBindingProposal | Mapping[str, Any]] = (),
        witness_ids: Sequence[str] = (),
        note: str = "",
        submission_id: str = "",
    ) -> dict[str, Any]:
        check_id = check_id.strip()
        write_scope_error = self._validate_focused_write(
            action="submit_check",
            check_id=check_id,
        )
        if write_scope_error is not None:
            return write_scope_error
        if check_id not in self._allowed_check_ids:
            return self._failure(
                "submit_check",
                code="CHECK_NOT_IN_PLAN",
                message=f"Check {check_id!r} is not an executable CHECK in the current ProofPlan.",
                repair="Submit one of the CHECK ids from the current ProofPlan.",
                check_id=check_id,
                allowed_check_ids=sorted(self._allowed_check_ids),
            )

        normalized_claim_ids = tuple(dict.fromkeys(item.strip() for item in claim_ids if item.strip()))
        unknown_claim_ids = [item for item in normalized_claim_ids if item not in self._claim_by_id]
        if unknown_claim_ids:
            return self._failure(
                "submit_check",
                code="CLAIM_REFERENCE_NOT_FOUND",
                message="The check submission references claims that were not admitted to EvidenceIR.",
                repair="Bind the missing claims successfully, or remove their ids before retrying.",
                check_id=check_id,
                unknown_claim_ids=unknown_claim_ids,
            )

        normalized_witness_ids = tuple(
            dict.fromkeys(item.strip() for item in witness_ids if item.strip())
        )
        witness_error = self._validate_owned_refs(
            action="submit_check",
            check_id=check_id,
            ref_ids=normalized_witness_ids,
            objects=self._witness_by_id,
            kind="witness",
        )
        if witness_error is not None:
            return witness_error

        parsed_proposals: list[SemanticBindingProposal] = []
        try:
            parsed_proposals = [
                item
                if isinstance(item, SemanticBindingProposal)
                else SemanticBindingProposal.model_validate(item)
                for item in binding_proposals
            ]
        except ValidationError as exc:
            return self._failure(
                "submit_check",
                code="BINDING_PROPOSAL_INVALID",
                message="A semantic binding proposal does not satisfy the typed proposal schema.",
                repair="Provide id, current check_id/facet_ref, relation, typed term_refs, and reason.",
                check_id=check_id,
                validation_errors=exc.errors(include_url=False, include_input=False),
            )

        proposal_ids = [item.id for item in parsed_proposals]
        if len(set(proposal_ids)) != len(proposal_ids):
            return self._failure(
                "submit_check",
                code="BINDING_ID_CONFLICT",
                message="One submission cannot contain duplicate binding proposal ids.",
                repair="Give each distinct proposal one unique id.",
                check_id=check_id,
            )
        for proposal in parsed_proposals:
            scope_error = self._validate_check_facet(
                action="submit_check",
                check_id=proposal.check_id,
                facet_ref=proposal.facet_ref,
            )
            if proposal.check_id != check_id or scope_error is not None:
                return self._failure(
                    "submit_check",
                    code="BINDING_SCOPE_MISMATCH",
                    message=(
                        f"Binding proposal {proposal.id!r} must belong to CHECK {check_id!r} "
                        "and one of that CHECK's facet_refs."
                    ),
                    repair="Use the current check_id and a facet_ref declared on that CHECK.",
                    check_id=check_id,
                    binding_id=proposal.id,
                )
            existing = self._binding_by_id.get(proposal.id)
            if existing is not None and existing != proposal:
                return self._failure(
                    "submit_check",
                    code="BINDING_ID_CONFLICT",
                    message=f"Binding id {proposal.id!r} already identifies different content.",
                    repair="Use a new unique binding id or resubmit the identical proposal.",
                    check_id=check_id,
                    binding_id=proposal.id,
                )
            for ref in proposal.term_refs:
                if ref.kind == "CLAIM" and (
                    ref.ref_id not in self._claim_by_id or ref.ref_id not in normalized_claim_ids
                ):
                    return self._binding_ref_failure(check_id, proposal.id, ref)
                if ref.kind == "WITNESS" and (
                    ref.ref_id not in self._witness_by_id
                    or ref.ref_id not in normalized_witness_ids
                ):
                    return self._binding_ref_failure(check_id, proposal.id, ref)
                if ref.kind == "POLICY" and (
                    ref.ref_id not in self._policy_values
                    or ref.ref_id
                    not in self._allowed_check_policy_refs.get(check_id, frozenset())
                ):
                    return self._binding_ref_failure(check_id, proposal.id, ref)

        note = note.strip()
        fingerprint = self._digest(
            {
                "check_id": check_id,
                "claim_ids": sorted(normalized_claim_ids),
                "binding_proposals": sorted(
                    (item.model_dump(mode="json") for item in parsed_proposals),
                    key=lambda item: item["id"],
                ),
                "witness_ids": sorted(normalized_witness_ids),
                "note": note,
            }
        )
        duplicate = self._submission_by_fingerprint.get(fingerprint)
        if duplicate is not None:
            return self._success(
                "submit_check",
                created=False,
                duplicate=True,
                submission=self._dump_submission(duplicate),
            )

        submission_id = submission_id.strip() or f"submission_{fingerprint[:16]}"
        if submission_id in self._submission_by_id:
            return self._failure(
                "submit_check",
                code="SUBMISSION_ID_CONFLICT",
                message=f"Submission id {submission_id!r} already identifies different content.",
                repair="Omit submission_id to receive a deterministic id, or provide a new unique id.",
                submission_id=submission_id,
            )

        submission = CheckSubmission(
            submission_id=submission_id,
            check_id=check_id,
            claim_ids=normalized_claim_ids,
            binding_ids=tuple(proposal_ids),
            witness_ids=normalized_witness_ids,
            note=note,
        )
        for proposal in parsed_proposals:
            if proposal.id not in self._binding_by_id:
                self._binding_proposals.append(proposal)
                self._binding_by_id[proposal.id] = proposal
        self._submissions.append(submission)
        self._submission_by_id[submission_id] = submission
        self._submission_by_fingerprint[fingerprint] = submission
        return self._success(
            "submit_check",
            created=True,
            duplicate=False,
            submission=self._dump_submission(submission),
        )

    def _validate_focused_write(
        self,
        *,
        action: str,
        check_id: str,
    ) -> dict[str, Any] | None:
        focused = self._focused_write_check_ids
        if focused is None or check_id in focused:
            return None
        return self._failure(
            action,
            code="CHECK_OUTSIDE_FOCUS",
            message=(
                f"CHECK {check_id!r} is frozen during this focused Executor run."
            ),
            repair="Write proof material only for one of the focused CHECK ids.",
            check_id=check_id,
            focused_check_ids=sorted(focused),
        )

    def _validate_check_facet(
        self,
        *,
        action: str,
        check_id: str,
        facet_ref: str,
    ) -> dict[str, Any] | None:
        if check_id not in self._allowed_check_ids:
            return self._failure(
                action,
                code="CHECK_NOT_IN_PLAN",
                message=f"Check {check_id!r} is not an executable CHECK in the current ProofPlan.",
                repair="Use one of the CHECK ids from the current ProofPlan.",
                check_id=check_id,
                allowed_check_ids=sorted(self._allowed_check_ids),
            )
        allowed_facets = self._allowed_check_facets.get(check_id, frozenset())
        if not facet_ref or facet_ref not in allowed_facets:
            return self._failure(
                action,
                code="FACET_NOT_IN_CHECK",
                message=f"Facet {facet_ref!r} is not declared on CHECK {check_id!r}.",
                repair="Use one of the facet_refs declared on the current CHECK.",
                check_id=check_id,
                facet_ref=facet_ref,
                allowed_facet_refs=sorted(allowed_facets),
            )
        return None

    def _validate_owned_refs(
        self,
        *,
        action: str,
        check_id: str,
        ref_ids: Sequence[str],
        objects: Mapping[str, Any],
        kind: str,
    ) -> dict[str, Any] | None:
        unknown = [ref_id for ref_id in ref_ids if ref_id not in objects]
        if unknown:
            return self._failure(
                action,
                code=f"{kind.upper()}_REFERENCE_NOT_FOUND",
                message=f"The submission references unknown {kind} ids.",
                repair=f"Use only {kind} ids created successfully in this sandbox.",
                check_id=check_id,
                unknown_ids=unknown,
            )
        cross_check = [ref_id for ref_id in ref_ids if objects[ref_id].check_id != check_id]
        if cross_check:
            return self._failure(
                action,
                code=f"{kind.upper()}_CHECK_MISMATCH",
                message=f"The submission references {kind}s owned by another CHECK.",
                repair=f"Submit each {kind} only under the CHECK that created it.",
                check_id=check_id,
                cross_check_ids=cross_check,
            )
        bad_facets = [
            ref_id
            for ref_id in ref_ids
            if objects[ref_id].facet_ref
            not in self._allowed_check_facets.get(check_id, frozenset())
        ]
        if bad_facets:
            return self._failure(
                action,
                code=f"{kind.upper()}_FACET_MISMATCH",
                message=f"The submission references {kind}s outside the CHECK's facet_refs.",
                repair="Recompute or resubmit proof terms under a declared facet_ref.",
                check_id=check_id,
                invalid_ids=bad_facets,
            )
        return None

    def _policy_values_for_request(
        self,
        refs: Sequence[ProofTermRef],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        result = dict(self._policy_values)
        resolved_document_currencies: dict[str, str] = {}
        currencies: set[str] = set()
        for ref in refs:
            if ref.kind == "CLAIM" and ref.ref_id in self._claim_by_id:
                currency = str(self._claim_by_id[ref.ref_id].attributes.get("currency") or "").strip()
            elif ref.kind == "WITNESS" and ref.ref_id in self._witness_by_id:
                currency = self._witness_by_id[ref.ref_id].currency
            else:
                currency = ""
            if currency:
                currencies.add(currency)
        for ref in refs:
            if ref.kind != "POLICY":
                continue
            raw = result.get(ref.ref_id)
            if not isinstance(raw, Mapping) or raw.get("unit") != "document_currency":
                continue
            if len(currencies) != 1:
                raise ProofTermError(
                    f"policy {ref.ref_id!r} uses document_currency but operands do not resolve one currency"
                )
            currency = next(iter(currencies))
            previous = self._resolved_document_currencies.get(ref.ref_id)
            if previous and previous != currency:
                raise ProofTermError(
                    f"policy {ref.ref_id!r} document_currency resolved inconsistently: "
                    f"{previous!r} != {currency!r}"
                )
            resolved_document_currencies[ref.ref_id] = currency
            result[ref.ref_id] = {
                "value": raw.get("value"),
                "currency": currency,
                "unit": "",
            }
        return result, resolved_document_currencies

    @classmethod
    def _numeric_claim_matches_quote(cls, claim: Claim) -> bool:
        return cls._numeric_value_matches_quote(claim.value, claim.quote)

    @staticmethod
    def _numeric_observation_repair(quote: str) -> str:
        if "%" in quote:
            return _PERCENT_NUMERIC_OBSERVATION_REPAIR
        return _NUMERIC_OBSERVATION_REPAIR

    @staticmethod
    def _has_numeric_intent(raw_value: Any) -> bool:
        if isinstance(raw_value, bool) or raw_value is None:
            return False
        if isinstance(raw_value, (Decimal, int, float)):
            return True
        return isinstance(raw_value, str) and bool(
            _NUMERIC_VALUE.fullmatch(raw_value.strip())
        )

    @classmethod
    def _numeric_value_matches_quote(cls, raw_value: Any, quote: str) -> bool:
        if isinstance(raw_value, bool) or raw_value is None or isinstance(raw_value, float):
            return False
        text = str(raw_value).strip()
        if not _NUMERIC_VALUE.fullmatch(text):
            return False
        value_percent = text.endswith("%")
        if value_percent:
            # Claim values are canonical numeric factors. A printed `20%` is
            # represented as `0.20`, never as the presentation string `20%`.
            return False
        try:
            canonical = Decimal(text.rstrip("%").replace(",", "."))
        except InvalidOperation:
            return False
        for match in _QUOTE_NUMBER.finditer(quote):
            token = match.group(0).strip()
            token_percent = token.endswith("%")
            for candidate in cls._localized_decimal_candidates(token):
                if token_percent and candidate / Decimal("100") == canonical:
                    return True
                if not token_percent and candidate == canonical:
                    return True
        return False

    @staticmethod
    def _localized_decimal_candidates(token: str) -> set[Decimal]:
        text = token.strip().rstrip("%").strip()
        negative = text.startswith("(") and text.endswith(")")
        text = text.strip("()").strip()
        sign = Decimal("-1") if negative else Decimal("1")
        if text[:1] in {"+", "-"}:
            if text[0] == "-":
                sign *= Decimal("-1")
            text = text[1:].strip()
            text = re.sub(r"^(?:[A-Z]{3}|[$€£¥])\s*", "", text)

        normalized = ""
        if any(mark in text for mark in (" ", "'", "’")):
            grouped = re.fullmatch(
                r"(\d{1,3}(?:[ '’]\d{3})+)(?:([.,])(\d+))?",
                text,
            )
            if grouped is None:
                return set()
            integer = re.sub(r"[ '’]", "", grouped.group(1))
            fraction = grouped.group(3)
            normalized = integer if fraction is None else f"{integer}.{fraction}"
        elif "." in text and "," in text:
            decimal_mark = "." if text.rfind(".") > text.rfind(",") else ","
            grouping_mark = "," if decimal_mark == "." else "."
            if text.count(decimal_mark) != 1:
                return set()
            integer, fraction = text.rsplit(decimal_mark, 1)
            if not fraction.isdigit():
                return set()
            grouped_integer = re.fullmatch(
                rf"\d{{1,3}}(?:{re.escape(grouping_mark)}\d{{3}})+",
                integer,
            )
            if grouped_integer is None:
                return set()
            normalized = integer.replace(grouping_mark, "") + "." + fraction
        elif "." in text or "," in text:
            mark = "." if "." in text else ","
            count = text.count(mark)
            if count > 1:
                if re.fullmatch(rf"\d{{1,3}}(?:{re.escape(mark)}\d{{3}})+", text) is None:
                    return set()
                normalized = text.replace(mark, "")
            else:
                integer, fraction = text.split(mark, 1)
                if not integer.isdigit() or not fraction.isdigit():
                    return set()
                # With no locale, `1,234` and `1.234` are equally valid as a
                # grouped integer or a three-decimal value. Do not guess.
                if len(fraction) == 3 and 1 <= len(integer) <= 3:
                    return set()
                normalized = f"{integer}.{fraction}"
        elif text.isdigit():
            normalized = text
        else:
            return set()

        try:
            return {Decimal(normalized) * sign}
        except InvalidOperation:
            return set()

    def _unknown_proof_ref(self, action: str, ref: ProofTermRef) -> dict[str, Any]:
        return self._failure(
            action,
            code=f"{ref.kind}_REFERENCE_NOT_FOUND",
            message=f"Proof term {ref.kind}:{ref.ref_id} is not available in this sandbox.",
            repair="Use ids returned by successful sandbox tools.",
            ref=ref.model_dump(mode="json"),
        )

    def _binding_ref_failure(
        self,
        check_id: str,
        binding_id: str,
        ref: ProofTermRef,
    ) -> dict[str, Any]:
        return self._failure(
            "submit_check",
            code="BINDING_REFERENCE_NOT_SUBMITTED",
            message=(
                f"Binding proposal {binding_id!r} references {ref.kind}:{ref.ref_id} "
                "outside this CHECK submission."
            ),
            repair="Submit every Claim/Witness used by the binding with the same CHECK; use only configured CHECK policies.",
            check_id=check_id,
            binding_id=binding_id,
            ref=ref.model_dump(mode="json"),
        )

    @staticmethod
    def _normalize_locator(
        locator: str | int,
        source_content: str,
    ) -> tuple[str, Mapping[str, Any] | None]:
        if isinstance(locator, bool):
            text = ""
        elif isinstance(locator, int):
            text = f"line {locator}" if locator > 0 else ""
        elif isinstance(locator, str):
            text = locator.strip()
            if text.isdigit():
                text = f"line {int(text)}" if int(text) > 0 else ""
        else:
            text = ""

        if not text or len(text) > 500 or not any(character.isalnum() for character in text):
            return "", {
                "code": "LOCATOR_INVALID",
                "message": "Locator must be a non-empty textual locator or positive line number.",
                "repair": "Use a stable locator such as 'invoice.pdf page 1' or line 3.",
            }

        line_match = _LINE_LOCATOR.search(text)
        if line_match:
            first = int(line_match.group(1))
            last = int(line_match.group(2) or first)
            line_count = max(1, len(source_content.splitlines()))
            if first < 1 or last < first or last > line_count:
                return "", {
                    "code": "LOCATOR_OUT_OF_RANGE",
                    "message": f"Locator {text!r} is outside the source's {line_count} lines.",
                    "repair": f"Use a line range between 1 and {line_count}, or an opaque page/field locator.",
                }
        return text, None

    @classmethod
    def _fingerprint_claim(
        cls,
        *,
        subject: str,
        predicate: str,
        value: Any,
        source_id: str,
        quote: str,
        locator: str,
        attributes: Mapping[str, Any],
    ) -> str:
        return cls._digest(
            {
                "subject": subject,
                "predicate": predicate,
                "value": value,
                "source_id": source_id,
                "quote": quote,
                "locator": locator,
                "attributes": attributes,
            }
        )

    @staticmethod
    def _digest(value: Mapping[str, Any]) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _dump_model(model: Any) -> dict[str, Any]:
        return model.model_dump(mode="json")

    @staticmethod
    def _dump_submission(submission: CheckSubmission) -> dict[str, Any]:
        return {
            "submission_id": submission.submission_id,
            "check_id": submission.check_id,
            "claim_ids": list(submission.claim_ids),
            "binding_ids": list(submission.binding_ids),
            "witness_ids": list(submission.witness_ids),
            "note": submission.note,
        }

    @staticmethod
    def _success(action: str, **data: Any) -> dict[str, Any]:
        return {"ok": True, "action": action, **data}

    @staticmethod
    def _failure(
        action: str,
        *,
        code: str,
        message: str,
        repair: str,
        **details: Any,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "action": action,
            "error": {
                "code": code,
                "message": message,
                "repair": repair,
                "details": details,
            },
        }
