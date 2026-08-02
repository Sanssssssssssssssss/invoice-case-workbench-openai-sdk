from __future__ import annotations

import hashlib
from copy import deepcopy
from decimal import Decimal

import pytest

from app.domain import semantic_graph
from app.domain.invoice_proof_compiler import compile_no_active_duplicate_proof, load_invoice_proof_policy, no_active_duplicate_program
from app.domain.semantic_graph import AttributeBinding, ClaimBinding, ClaimSelector, NodeSpec, SemanticGraphSpec, compile_proof_graph
from app.state.attachment_manifest import save_attachment_manifest
from app.state.case_store import CaseStore
from app.state.schemas import EvidenceItem


def _claim_id(subject: str, predicate: str, entity_key: str | None = None) -> str:
    suffix = f"_{entity_key}" if entity_key else ""
    return f"CLM_{subject}_{predicate}{suffix}".upper().replace(":", "_").replace("-", "_")


def _claim(
    subject: str,
    predicate: str,
    value: str,
    quote: str,
    *,
    entity_key: str | None = None,
) -> dict[str, str]:
    row = {
        "id": _claim_id(subject, predicate, entity_key),
        "subject": subject,
        "predicate": predicate,
        "value_type": "string",
        "typed_value": value,
        "quote": quote,
        "block_or_table_or_region": f"{subject}:{predicate}",
        "confidence": "high",
    }
    if entity_key:
        row["entity_key"] = entity_key
    return row


def _evidence(
    evidence_id: str,
    evidence_type: str,
    claims: list[dict[str, str]],
    *,
    judgments: list[dict] | None = None,
    source: str = "attachment",
) -> EvidenceItem:
    requirement = "invoice" if evidence_type == "invoice" else "duplicate_payment_screen"
    return EvidenceItem(
        id=evidence_id,
        type=evidence_type,
        source=source,
        credibility="high",
        content=" ".join(str(claim["quote"]) for claim in claims),
        review_result={"should_accept": True},
        supports=[{"requirement": requirement, "support_level": "full"}],
        metadata={
            "classification": "business_evidence",
            "claim_to_source_refs": claims,
            "semantic_judgments": judgments or [],
        },
    )


def _ref(
    evidence_id: str,
    subject: str,
    predicate: str,
    *,
    entity_key: str | None = None,
) -> dict[str, str]:
    return {
        "evidence_id": evidence_id,
        "claim_id": _claim_id(subject, predicate, entity_key),
        "subject": subject,
        "predicate": predicate,
    }


def _trusted_sources(items: list[EvidenceItem]) -> dict[str, dict[str, object]]:
    return {
        item.id: {
            "texts": [item.content],
            "sha256": f"sha256:{item.id}",
            "original_ref": f"attachment:{item.id}",
        }
        for item in items
    }


def _compile(items: list[EvidenceItem]):
    return compile_no_active_duplicate_proof(items, trusted_sources=_trusted_sources(items))


def _field_program() -> SemanticGraphSpec:
    return SemanticGraphSpec(
        id="field_boundary",
        version="1",
        requirement_id="invoice",
        claim_bindings=(ClaimBinding(
            "invoice",
            "reference",
            "extracted_field",
            ("invoice",),
            "invoice",
            "string",
            ("reference",),
            attributes=(AttributeBinding("currency", ("currency",), "currency"),),
        ),),
        nodes=(NodeSpec(
            "CHK_REFERENCE_PRESENT",
            "invoice",
            "present",
            (ClaimSelector("invoice", "reference"),),
        ),),
        root_id="CHK_REFERENCE_PRESENT",
    )


def _field_evidence(*, confidence: str | None = "high") -> EvidenceItem:
    reference = {
        "value": "INV-1",
        "source_quote": "Invoice INV-1",
        "source_locator": "line:1",
    }
    if confidence:
        reference["confidence"] = confidence
    return EvidenceItem(
        id="ev_field",
        type="invoice",
        source="attachment",
        credibility="high",
        content="Invoice INV-1 Currency GBP",
        review_result={"should_accept": True},
        supports=[{"requirement": "invoice", "support_level": "full"}],
        metadata={
            "classification": "business_evidence",
            "extracted_fields": {
                "reference": reference,
                "currency": {
                    "value": "GBP",
                    "source_quote": "Currency GBP",
                    "source_locator": "line:1",
                    "confidence": "high",
                },
            },
        },
    )


def _seed_store_sources(
    store: CaseStore,
    case_id: str,
    items: list[EvidenceItem],
) -> None:
    attachments = []
    for item in items:
        original_ref = f"attachments/originals/{item.id}.md"
        attachment_id = f"att_{item.id}"
        source_filename = f"{item.id}.md"
        path = store.resolve_case_path(case_id, original_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.content, encoding="utf-8")
        item.metadata.update({
            "attachment_id": attachment_id,
            "original_ref": original_ref,
            "source_filename": source_filename,
        })
        attachments.append({
            "attachment_id": attachment_id,
            "name": source_filename,
            "original_ref": original_ref,
            "status": "active",
            "sha256": hashlib.sha256(item.content.encode()).hexdigest(),
            "evidence_ids": [item.id],
        })
    save_attachment_manifest(store, case_id, {"attachments": attachments})


def _packet(verdict: str, *, reversal: bool = True) -> list[EvidenceItem]:
    entity_key = "candidate:PAY-771"
    candidate_ref = _ref(
        "ev_search",
        "duplicate_search",
        "candidate_identity",
        entity_key=entity_key,
    )
    identity_ref = _ref("ev_history", "payment", "identity", entity_key=entity_key)
    relation_ref = _ref(
        "ev_history",
        "payment",
        "relationship_to_payable",
        entity_key=entity_key,
    )
    effect_ref = _ref("ev_history", "payment", "economic_effect", entity_key=entity_key)
    refs = [candidate_ref, identity_ref, relation_ref, effect_ref]
    items = [
        _evidence("ev_invoice", "invoice", [_claim("invoice", "payable_identity", "payable:INV-7701", "Invoice INV-7701 remains payable")]),
        _evidence("ev_search", "duplicate_payment_check", [
            _claim("duplicate_search", "payable_identity", "payable:INV-7701", "Search target is payable INV-7701"),
            _claim("duplicate_search", "coverage", "complete", "Search covers posted and cleared payments"),
            _claim("duplicate_search", "result", "candidate_found", "Search found candidate payment PAY-771"),
            _claim(
                "duplicate_search",
                "candidate_identity",
                "payment:PAY-771",
                "Search found candidate payment PAY-771",
                entity_key=entity_key,
            ),
        ]),
        _evidence("ev_history", "duplicate_payment_check", [
            _claim("payment", "identity", "payment:PAY-771", "Payment record PAY-771", entity_key=entity_key),
            _claim(
                "payment",
                "relationship_to_payable",
                "same_obligation",
                "PAY-771 relates to INV-7701",
                entity_key=entity_key,
            ),
            _claim(
                "payment",
                "economic_effect",
                {"SUPPORTED": "neutralized_by_reversal", "REFUTED": "active_settled", "UNKNOWN": "unknown"}[verdict],
                {
                    "SUPPORTED": "The earlier payment was fully cancelled and the invoice balance was restored",
                    "REFUTED": "PAY-771 remains settled with no reversal or refund",
                    "UNKNOWN": "PAY-771 is a candidate match; lifecycle disposition is not shown",
                }[verdict],
                entity_key=entity_key,
            ),
        ]),
    ]
    if reversal:
        reversal_refs = [
            _ref("ev_reversal", "reversal", "reverses", entity_key=entity_key),
            _ref("ev_reversal", "reversal", "posting_status", entity_key=entity_key),
            _ref("ev_reversal", "reversal", "scope", entity_key=entity_key),
        ]
        refs.extend(reversal_refs)
        items.append(_evidence("ev_reversal", "duplicate_payment_check", [
            _claim("reversal", "reverses", "payment:PAY-771", "RV-771 reverses PAY-771", entity_key=entity_key),
            _claim("reversal", "posting_status", "posted", "RV-771 status: posted", entity_key=entity_key),
            _claim("reversal", "scope", "full", "RV-771 is a full reversal", entity_key=entity_key),
        ]))
    carrier = items[-1]
    carrier.metadata["semantic_judgments"] = [{
        "id": "JDG_NO_ACTIVE_DUPLICATE",
        "verdict": verdict,
        "input_refs": refs,
        "supporting_refs": refs if verdict == "SUPPORTED" else [],
        "opposing_refs": [effect_ref] if verdict == "REFUTED" else [],
        "open_questions": ["authoritative lifecycle disposition"] if verdict == "UNKNOWN" else [],
        "confidence": "high",
        "reason": {
            "SUPPORTED": "The candidate payment has no remaining economic effect after a posted full reversal.",
            "REFUTED": "The same obligation still has an economically active settled payment.",
            "UNKNOWN": "The candidate relationship is known, but its lifecycle disposition is not.",
        }[verdict],
    }]
    return items


def _no_candidate_packet(search_payable: str = "payable:INV-7701") -> list[EvidenceItem]:
    refs = [
        _ref("ev_invoice", "invoice", "payable_identity"),
        _ref("ev_search", "duplicate_search", "payable_identity"),
        _ref("ev_search", "duplicate_search", "coverage"),
        _ref("ev_search", "duplicate_search", "result"),
    ]
    return [
        _evidence("ev_invoice", "invoice", [
            _claim("invoice", "payable_identity", "payable:INV-7701", "Invoice INV-7701 remains payable"),
        ]),
        _evidence("ev_search", "duplicate_payment_check", [
            _claim("duplicate_search", "payable_identity", search_payable, f"Search target is {search_payable}"),
            _claim("duplicate_search", "coverage", "complete", "Search covers posted and cleared payments"),
            _claim("duplicate_search", "result", "no_candidate", "Search found no candidate payment"),
        ], judgments=[{
            "id": "JDG_NO_ACTIVE_DUPLICATE",
            "verdict": "SUPPORTED",
            "input_refs": refs,
            "supporting_refs": refs,
            "opposing_refs": [],
            "open_questions": [],
            "confidence": "high",
            "reason": "The complete search found no candidate payment.",
        }]),
    ]


def test_second_semantic_program_runs_on_the_same_graph_runtime() -> None:
    proof = _compile(_packet("SUPPORTED"))
    decision = proof.decision_for("no_active_duplicate")

    assert decision and decision.proof_status == "PROVED"
    assert decision.root_check_id == "FINAL_NO_ACTIVE_DUPLICATE"
    assert decision.outcome == "EVIDENCE_SUFFICIENT_FOR_REPORT"
    assert proof.obligations == []
    judgment_check = next(item for item in proof.checks if item.id == "JDG_NO_ACTIVE_DUPLICATE")
    assert judgment_check.executor == "llm"
    assert judgment_check.operator == "llm_judgment"
    assert len(judgment_check.input_claim_ids) == 11
    assert {claim.entity_key for claim in proof.claims if claim.entity_key} == {"candidate:PAY-771"}
    assert all(claim.source_quote and claim.source_locator for claim in proof.claims)


def test_complete_search_with_no_candidate_is_also_proved() -> None:
    proof = _compile(_no_candidate_packet())

    assert proof.decision_for("no_active_duplicate").proof_status == "PROVED"
    assert not any(claim.subject == "payment" for claim in proof.claims)


def test_no_candidate_branch_rejects_unkeyed_candidate_claims() -> None:
    packet = _no_candidate_packet()
    rogue = _evidence("ev_rogue", "duplicate_payment_check", [
        _claim("payment", "identity", "payment:PAY-ROGUE", "Payment record PAY-ROGUE"),
        _claim("payment", "economic_effect", "active_settled", "PAY-ROGUE remains settled"),
    ])
    refs = [
        _ref("ev_rogue", "payment", "identity"),
        _ref("ev_rogue", "payment", "economic_effect"),
    ]
    judgment = packet[-1].metadata["semantic_judgments"][0]
    judgment["input_refs"].extend(refs)
    judgment["supporting_refs"].extend(refs)
    packet.append(rogue)

    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"


def test_no_candidate_search_for_another_payable_cannot_prove_current_invoice() -> None:
    proof = _compile(_no_candidate_packet("payable:INV-OTHER"))

    decision = proof.decision_for("no_active_duplicate")
    assert decision and decision.proof_status == "INCOMPLETE"
    assert "CHK_DUPLICATE_SEARCH_SCOPE" in decision.incomplete_check_ids
    assert "OBL_DUPLICATE_SEARCH_SCOPE" in decision.obligation_ids


def test_supported_candidate_requires_an_explicit_reversal_link() -> None:
    packet = _packet("SUPPORTED")
    link_id = packet[3].metadata["claim_to_source_refs"].pop(0)["id"]
    judgment = packet[3].metadata["semantic_judgments"][0]
    judgment["input_refs"] = [ref for ref in judgment["input_refs"] if ref["claim_id"] != link_id]
    judgment["supporting_refs"] = [ref for ref in judgment["supporting_refs"] if ref["claim_id"] != link_id]

    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"
    assert "invalid entity relation" in next(
        item for item in proof.checks if item.id == "JDG_NO_ACTIVE_DUPLICATE"
    ).reason


def test_identity_values_cannot_be_forged_independently_of_quotes() -> None:
    packet = _packet("SUPPORTED")
    identity_predicates = {
        ("invoice", "payable_identity"),
        ("duplicate_search", "payable_identity"),
        ("duplicate_search", "candidate_identity"),
        ("payment", "identity"),
        ("reversal", "reverses"),
    }
    for item in packet:
        for row in item.metadata["claim_to_source_refs"]:
            if (row["subject"], row["predicate"]) in identity_predicates:
                row["typed_value"] = "identity:FORGED-999"

    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"


def test_one_neutralized_and_one_active_candidate_is_disproved() -> None:
    packet = _packet("SUPPORTED")
    entity_key = "candidate:PAY-772"
    candidate = _claim(
        "duplicate_search",
        "candidate_identity",
        "payment:PAY-772",
        "Search found candidate payment PAY-772",
        entity_key=entity_key,
    )
    history = [
        _claim("payment", "identity", "payment:PAY-772", "Payment record PAY-772", entity_key=entity_key),
        _claim(
            "payment",
            "relationship_to_payable",
            "same_obligation",
            "PAY-772 relates to INV-7701",
            entity_key=entity_key,
        ),
        _claim(
            "payment",
            "economic_effect",
            "active_settled",
            "PAY-772 remains settled with no reversal or refund",
            entity_key=entity_key,
        ),
    ]
    packet[1].metadata["claim_to_source_refs"].append(candidate)
    packet[1].content += f" {candidate['quote']}"
    packet[2].metadata["claim_to_source_refs"].extend(history)
    packet[2].content += " " + " ".join(item["quote"] for item in history)
    refs = [
        _ref("ev_search", "duplicate_search", "candidate_identity", entity_key=entity_key),
        _ref("ev_history", "payment", "identity", entity_key=entity_key),
        _ref("ev_history", "payment", "relationship_to_payable", entity_key=entity_key),
        _ref("ev_history", "payment", "economic_effect", entity_key=entity_key),
    ]
    packet[-1].metadata["semantic_judgments"].append({
        "id": "JDG_NO_ACTIVE_DUPLICATE",
        "verdict": "REFUTED",
        "input_refs": refs,
        "supporting_refs": [],
        "opposing_refs": [refs[-1]],
        "open_questions": [],
        "confidence": "high",
        "reason": "PAY-772 is an active settlement of the same obligation.",
    })

    proof = _compile(packet)

    decision = proof.decision_for("no_active_duplicate")
    assert decision and decision.proof_status == "DISPROVED"
    assert "candidate:PAY-771=SUPPORTED" in next(
        item for item in proof.checks if item.id == "JDG_NO_ACTIVE_DUPLICATE"
    ).reason
    assert "candidate:PAY-772=REFUTED" in next(
        item for item in proof.checks if item.id == "JDG_NO_ACTIVE_DUPLICATE"
    ).reason


def test_semantic_program_distinguishes_unknown_from_refuted() -> None:
    unknown = _compile(_packet("UNKNOWN", reversal=False))
    refuted = _compile(_packet("REFUTED", reversal=False))

    assert unknown.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"
    assert unknown.decision_for("no_active_duplicate").outcome == "HOLD_FOR_EVIDENCE"
    assert [item.id for item in unknown.obligations] == ["OBL_DUPLICATE_DISPOSITION"]
    assert refuted.decision_for("no_active_duplicate").proof_status == "DISPROVED"
    assert refuted.decision_for("no_active_duplicate").outcome == "EVIDENCE_SUFFICIENT_FOR_REPORT"


def test_semantic_judgment_is_bound_to_current_admissible_claims() -> None:
    packet = _packet("SUPPORTED")
    packet[-1].source = "user_message"
    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"
    assert any(not judgment.valid for judgment in proof.judgments) or not proof.judgments


def test_unclassified_attachment_cannot_enter_the_proof() -> None:
    packet = _no_candidate_packet()
    packet[0].metadata.pop("classification")

    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"


def test_duplicate_evidence_ids_fail_closed() -> None:
    packet = _no_candidate_packet()
    packet[-1].id = packet[0].id

    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"


def test_semantic_claim_quote_must_exist_in_its_evidence() -> None:
    packet = _packet("SUPPORTED")
    trusted_sources = _trusted_sources(packet)
    packet[2].metadata["claim_to_source_refs"][2]["quote"] = "Invented lifecycle statement"
    packet[2].content += " Invented lifecycle statement"

    proof = compile_no_active_duplicate_proof(packet, trusted_sources=trusted_sources)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"
    assert not any(
        claim.subject == "payment" and claim.predicate == "economic_effect"
        for claim in proof.claims
    )


def test_extracted_field_requires_explicit_confidence_and_runtime_source() -> None:
    missing_confidence = _field_evidence(confidence=None)
    trusted_sources = _trusted_sources([missing_confidence])

    without_confidence = compile_proof_graph(
        _field_program(),
        [missing_confidence],
        policy_version="test",
        compiler_version="test",
        trusted_sources=trusted_sources,
    )
    with_confidence = _field_evidence()
    without_runtime_source = compile_proof_graph(
        _field_program(),
        [with_confidence],
        policy_version="test",
        compiler_version="test",
    )
    incomplete_runtime_source = compile_proof_graph(
        _field_program(),
        [with_confidence],
        policy_version="test",
        compiler_version="test",
        trusted_sources={
            with_confidence.id: {
                "texts": [with_confidence.content],
                "sha256": "",
                "original_ref": "attachment:ev_field",
            },
        },
    )
    grounded = compile_proof_graph(
        _field_program(),
        [with_confidence],
        policy_version="test",
        compiler_version="test",
        trusted_sources=_trusted_sources([with_confidence]),
    )

    assert without_confidence.decision.proof_status == "INCOMPLETE"
    assert without_runtime_source.decision.proof_status == "INCOMPLETE"
    assert incomplete_runtime_source.decision.proof_status == "INCOMPLETE"
    assert grounded.decision.proof_status == "PROVED"


def test_extracted_attribute_quote_cannot_be_self_certified() -> None:
    item = _field_evidence()
    trusted_sources = _trusted_sources([item])
    trusted_sources[item.id]["texts"] = ["Invoice INV-1"]

    proof = compile_proof_graph(
        _field_program(),
        [item],
        policy_version="test",
        compiler_version="test",
        trusted_sources=trusted_sources,
    )

    assert proof.claims[0].currency is None
    assert "currency" not in proof.claims[0].attribute_sources


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Amount DUE", None),
        ("VAT total", None),
        ("$100", None),
        ("\u00a5100", None),
        ("GBP 100", "GBP"),
        ("US$100", "USD"),
        ("USD 100 / EUR 92", None),
    ],
)
def test_currency_parser_fails_closed_on_ambiguous_tokens(raw: str, expected: str | None) -> None:
    assert semantic_graph._typed_value(raw, "currency", ()) == expected


def test_invalid_active_judgment_cannot_be_ignored() -> None:
    packet = _packet("SUPPORTED")
    malformed = dict(packet[-1].metadata["semantic_judgments"][0])
    malformed.update({"verdict": "REFUTED", "supporting_refs": [], "opposing_refs": []})
    packet[0].metadata["semantic_judgments"] = [malformed]

    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"
    assert any(not judgment.valid for judgment in proof.judgments)


def test_stale_judgment_for_removed_evidence_is_ignored() -> None:
    packet = _packet("SUPPORTED")
    stale = deepcopy(packet[-1].metadata["semantic_judgments"][0])
    old_ref = _ref("ev_superseded", "payment", "economic_effect")
    stale.update({
        "verdict": "REFUTED",
        "input_refs": [old_ref],
        "supporting_refs": [],
        "opposing_refs": [old_ref],
    })
    packet[0].metadata["semantic_judgments"] = [stale]

    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "PROVED"
    assert any("stale input Claim reference" in item.validation_errors for item in proof.judgments)


def test_partly_stale_judgment_still_poisons_current_claim_set() -> None:
    packet = _packet("SUPPORTED")
    malformed = deepcopy(packet[-1].metadata["semantic_judgments"][0])
    malformed["input_refs"].append(_ref("ev_superseded", "payment", "economic_effect"))
    packet[0].metadata["semantic_judgments"] = [malformed]

    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"
    assert any(
        any("ev_superseded" in error for error in judgment.validation_errors)
        for judgment in proof.judgments
    )


def test_strong_judgment_must_match_declared_claim_values() -> None:
    packet = _packet("REFUTED", reversal=False)
    judgment = packet[-1].metadata["semantic_judgments"][0]
    judgment.update({
        "verdict": "SUPPORTED",
        "supporting_refs": [judgment["input_refs"][3]],
        "opposing_refs": [],
        "reason": "Incorrectly claims that the active settled payment has no effect.",
    })

    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"
    assert "allowed Claim-value option" in next(
        item for item in proof.checks if item.id == "JDG_NO_ACTIVE_DUPLICATE"
    ).reason


def test_generated_claim_ids_preserve_lossy_evidence_id_differences() -> None:
    first_claim = _claim("invoice", "payable_identity", "payable:A01", "Invoice A01")
    second_claim = _claim("invoice", "payable_identity", "payable:B02", "Invoice B02")
    first_claim.pop("id")
    second_claim.pop("id")

    packet = [
        _evidence("ev-a", "invoice", [first_claim]),
        _evidence("ev_a", "invoice", [second_claim]),
    ]
    proof = _compile(packet)

    assert len(proof.claims) == 2
    assert len({claim.id for claim in proof.claims}) == 2


def test_reviewer_claim_id_collision_fails_closed() -> None:
    packet = _packet("SUPPORTED")
    packet[2].metadata["claim_to_source_refs"][1]["id"] = packet[2].metadata["claim_to_source_refs"][0]["id"]

    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"


def test_claim_id_must_be_unique_across_the_whole_semantic_packet() -> None:
    packet = _no_candidate_packet()
    packet[0].metadata["claim_to_source_refs"][0]["id"] = "CLM_PACKET_COLLISION"
    packet[0].metadata["claim_to_source_refs"].append({
        **_claim("invoice", "outside_this_program", "other", "Other sourced value"),
        "id": "CLM_PACKET_COLLISION",
    })
    invoice_ref = packet[-1].metadata["semantic_judgments"][0]["input_refs"][0]
    invoice_ref["claim_id"] = "CLM_PACKET_COLLISION"
    packet[-1].metadata["semantic_judgments"][0]["supporting_refs"][0]["claim_id"] = "CLM_PACKET_COLLISION"

    proof = _compile(packet)

    assert proof.decision_for("no_active_duplicate").proof_status == "INCOMPLETE"


def test_semantic_program_hash_is_order_independent() -> None:
    packet = _packet("SUPPORTED")
    first = _compile(packet).decision_for("no_active_duplicate")
    reordered = deepcopy(list(reversed(packet)))
    for item in reordered:
        item.metadata["claim_to_source_refs"] = list(reversed(item.metadata["claim_to_source_refs"]))
        for judgment in item.metadata["semantic_judgments"]:
            judgment["input_refs"] = list(reversed(judgment["input_refs"]))
            judgment["supporting_refs"] = list(reversed(judgment["supporting_refs"]))
            judgment["reason"] = "Equivalent explanatory prose must not change proof identity."
    second = _compile(reordered).decision_for("no_active_duplicate")
    assert first.evidence_snapshot_hash == second.evidence_snapshot_hash


def test_snapshot_canonicalizes_judgment_collections_and_source_fingerprints() -> None:
    packet = _packet("SUPPORTED")
    malformed = deepcopy(packet[-1].metadata["semantic_judgments"][0])
    malformed.update({
        "verdict": "UNKNOWN",
        "input_refs": [
            {**_ref("ev_invoice", "invoice", "payable_identity"), "claim_id": "CLM_MISSING_A"},
            {**_ref("ev_search", "duplicate_search", "coverage"), "claim_id": "CLM_MISSING_B"},
        ],
        "supporting_refs": [],
        "opposing_refs": [],
        "open_questions": ["z question", "a question", "z question"],
    })
    packet[0].metadata["semantic_judgments"] = [malformed]
    sources = _trusted_sources(packet)
    first = compile_no_active_duplicate_proof(packet, trusted_sources=sources).decision_for("no_active_duplicate")

    reordered = deepcopy(packet)
    invalid = reordered[0].metadata["semantic_judgments"][0]
    invalid["input_refs"] = list(reversed(invalid["input_refs"]))
    invalid["open_questions"] = ["a question", "z question"]
    same_fingerprints = dict(reversed(list(deepcopy(sources).items())))
    same_fingerprints["ev_invoice"]["texts"].append("Unrelated trusted text is not copied into the hash")
    second = compile_no_active_duplicate_proof(
        reordered,
        trusted_sources=same_fingerprints,
    ).decision_for("no_active_duplicate")

    changed_fingerprint = deepcopy(sources)
    changed_fingerprint["ev_invoice"]["sha256"] = "sha256:changed"
    third = compile_no_active_duplicate_proof(
        packet,
        trusted_sources=changed_fingerprint,
    ).decision_for("no_active_duplicate")

    assert first.evidence_snapshot_hash == second.evidence_snapshot_hash
    assert first.evidence_snapshot_hash != third.evidence_snapshot_hash


def test_unrelated_trusted_source_does_not_change_program_snapshot() -> None:
    packet = _packet("SUPPORTED")
    sources = _trusted_sources(packet)
    first = compile_no_active_duplicate_proof(packet, trusted_sources=sources).decision_for("no_active_duplicate")
    sources["ev_unrelated"] = {
        "texts": ["Unrelated vendor file"],
        "sha256": "sha256:unrelated",
        "original_ref": "attachment:unrelated",
    }
    second = compile_no_active_duplicate_proof(packet, trusted_sources=sources).decision_for("no_active_duplicate")

    assert first.evidence_snapshot_hash == second.evidence_snapshot_hash


def test_unrelated_amount_policy_does_not_change_duplicate_snapshot() -> None:
    packet = _packet("SUPPORTED")
    sources = _trusted_sources(packet)
    policy = load_invoice_proof_policy()
    changed = policy.model_copy(update={"amount_tolerance_percent": Decimal("99")})

    first = compile_no_active_duplicate_proof(packet, policy=policy, trusted_sources=sources)
    second = compile_no_active_duplicate_proof(packet, policy=changed, trusted_sources=sources)

    assert first.decision.evidence_snapshot_hash == second.decision.evidence_snapshot_hash


def test_graph_rejects_missing_dependencies_before_execution() -> None:
    spec = SemanticGraphSpec(
        id="invalid",
        version="1",
        requirement_id="invalid",
        claim_bindings=(),
        nodes=(NodeSpec("REQ_INVALID", "invalid", "all", depends_on=("MISSING",)),),
        root_id="REQ_INVALID",
    )
    with pytest.raises(ValueError, match="Missing graph dependencies"):
        compile_proof_graph(spec, [], policy_version="test", compiler_version="test")


def test_graph_rejects_empty_aggregate() -> None:
    spec = SemanticGraphSpec(
        id="invalid",
        version="1",
        requirement_id="invalid",
        claim_bindings=(),
        nodes=(NodeSpec("ROOT", "invalid", "all"),),
        root_id="ROOT",
    )
    with pytest.raises(ValueError, match="Aggregate nodes require dependencies"):
        compile_proof_graph(spec, [], policy_version="test", compiler_version="test")


def test_graph_rejects_nodes_that_do_not_lead_to_root() -> None:
    spec = SemanticGraphSpec(
        id="invalid",
        version="1",
        requirement_id="invalid",
        claim_bindings=(),
        nodes=(
            NodeSpec("CONNECTED", "invalid", "present", (ClaimSelector("invoice", "reference"),)),
            NodeSpec("DISCONNECTED", "invalid", "present", (ClaimSelector("invoice", "other"),)),
            NodeSpec("ROOT", "invalid", "all", depends_on=("CONNECTED",)),
        ),
        root_id="ROOT",
    )
    with pytest.raises(ValueError, match="must lead to root"):
        compile_proof_graph(spec, [], policy_version="test", compiler_version="test")


def test_disproved_dependency_chain_cannot_become_sufficient() -> None:
    spec = SemanticGraphSpec(
        id="dependency_chain",
        version="1",
        requirement_id="invoice",
        claim_bindings=_field_program().claim_bindings,
        nodes=(
            NodeSpec("A", "invoice", "allowed", (ClaimSelector("invoice", "reference"),), params={"allowed": ("OTHER",)}),
            NodeSpec("B", "invoice", "present", (ClaimSelector("invoice", "reference"),), depends_on=("A",)),
            NodeSpec("ROOT", "invoice", "all", depends_on=("B",)),
        ),
        root_id="ROOT",
    )
    item = _field_evidence()
    result = compile_proof_graph(
        spec,
        [item],
        policy_version="test",
        compiler_version="test",
        trusted_sources=_trusted_sources([item]),
    )

    assert result.checks[-1].status == "NOT_APPLICABLE"
    assert result.decision.outcome == "HOLD_FOR_EVIDENCE"


def test_duplicate_program_is_declared_without_new_runtime_operators() -> None:
    spec = no_active_duplicate_program(load_invoice_proof_policy())
    assert {node.operator for node in spec.nodes} == {"same", "allowed", "llm_judgment", "all"}
    assert spec.activation_requirements == ("duplicate_payment_screen",)


def test_case_store_projects_a_second_program_without_amount_specific_code(tmp_path) -> None:
    store = CaseStore(tmp_path)
    case_id = "case_duplicate_reversal"
    packet = _packet("SUPPORTED")
    _seed_store_sources(store, case_id, packet)
    updated = store.apply_patch(case_id, {
        "patch_type": "add_evidence",
        "case_updates": {
            "requirements": [
                {"id": "invoice", "label": "Invoice"},
                {"id": "duplicate_payment_screen", "label": "Duplicate-payment source"},
            ],
            "add_evidence": [item.model_dump(mode="json") for item in packet],
        },
    })

    requirements = {item.id: item.status for item in updated.requirements}
    assert requirements["no_active_duplicate"] == "satisfied"
    assert updated.compiled_proof.decision_for("no_active_duplicate").proof_status == "PROVED"
    assert updated.status == "ready_for_report"


def test_selected_program_cannot_be_bypassed_when_reviewer_omits_ir(tmp_path) -> None:
    store = CaseStore(tmp_path)
    updated = store.apply_patch("case_missing_semantic_ir", {
        "patch_type": "add_evidence",
        "case_updates": {
            "requirements": [
                {"id": "invoice"},
                {"id": "duplicate_payment_screen"},
            ],
            "add_evidence": [
                _evidence("ev_invoice", "invoice", []).model_dump(mode="json"),
                _evidence("ev_search", "duplicate_payment_check", []).model_dump(mode="json"),
            ],
        },
    })

    decision = updated.compiled_proof.decision_for("no_active_duplicate")
    assert decision and decision.proof_status == "INCOMPLETE"
    assert next(item for item in updated.requirements if item.id == "no_active_duplicate").status == "missing"


def test_refuted_duplicate_proof_is_reportable_not_a_missing_source(tmp_path) -> None:
    store = CaseStore(tmp_path)
    case_id = "case_active_duplicate"
    packet = _packet("REFUTED", reversal=False)
    _seed_store_sources(store, case_id, packet)
    updated = store.apply_patch(case_id, {
        "patch_type": "add_evidence",
        "case_updates": {
            "requirements": [
                {"id": "invoice"},
                {"id": "duplicate_payment_screen"},
            ],
            "add_evidence": [item.model_dump(mode="json") for item in packet],
        },
    })

    assert updated.compiled_proof.decision_for("no_active_duplicate").proof_status == "DISPROVED"
    assert next(item for item in updated.requirements if item.id == "duplicate_payment_screen").status == "satisfied"
    assert updated.status == "ready_for_report"
