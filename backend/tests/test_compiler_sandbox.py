from __future__ import annotations

import pytest

from app.compiler_runtime.models import Claim, EvidenceIR
from app.compiler_runtime.sandbox import EvidenceSandbox, SourceRecord


def _sandbox(*, ir: EvidenceIR | None = None) -> EvidenceSandbox:
    return EvidenceSandbox(
        sources=[
            SourceRecord(
                source_id="invoice-1",
                title="Invoice 1",
                kind="invoice",
                content="Invoice INV-42\nSupplier: Acme Ltd\nTotal GBP 10,500\nStatus: open",
            )
        ],
        allowed_check_ids=["check-total", "check-supplier"],
        evidence_ir=ir,
    )


def _bind_total(sandbox: EvidenceSandbox, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "subject": "invoice:INV-42",
        "predicate": "invoice.total",
        "value": {"amount": "10500", "currency": "GBP"},
        "source_id": "invoice-1",
        "quote": "Total GBP 10,500",
        "locator": "line 3",
        "confidence": "high",
        "attributes": {"basis": "gross", "currency": "GBP"},
    }
    payload.update(overrides)
    return sandbox.bind_claim(**payload)


def test_only_listed_sources_can_be_read_and_listing_hides_content() -> None:
    sandbox = _sandbox()

    listed = sandbox.list_sources()

    assert listed == {
        "ok": True,
        "action": "list_sources",
        "sources": [
            {
                "source_id": "invoice-1",
                "title": "Invoice 1",
                "kind": "invoice",
                "characters": 63,
            }
        ],
    }
    assert "content" not in listed["sources"][0]

    missing = sandbox.read_source("not-in-run")
    assert missing["ok"] is False
    assert missing["error"]["code"] == "SOURCE_NOT_FOUND"
    assert missing["error"]["repair"]
    assert sandbox.read_source_ids == ()

    read = sandbox.read_source("invoice-1")
    assert read["ok"] is True
    assert read["source"]["content"].startswith("Invoice INV-42")
    assert sandbox.read_source_ids == ("invoice-1",)


def test_claim_requires_a_prior_read_and_exact_source_quote() -> None:
    sandbox = _sandbox()

    unread = _bind_total(sandbox)
    assert unread["error"]["code"] == "SOURCE_NOT_READ"
    assert sandbox.evidence_ir.claims == []

    sandbox.read_source("invoice-1")
    wrong_quote = _bind_total(sandbox, quote="Total GBP 10,501")
    assert wrong_quote["error"]["code"] == "QUOTE_NOT_IN_SOURCE"
    assert wrong_quote["error"]["repair"]
    assert sandbox.evidence_ir.claims == []


def test_system_provenance_is_read_gated_and_quoteable_without_claiming_authenticity() -> None:
    sandbox = EvidenceSandbox(
        sources=[
            SourceRecord(
                source_id="invoice-upload",
                content="Invoice INV-42",
                provenance={
                    "runtime_admission": "admitted",
                    "attachment_id": "att-42",
                    "original_ref": "attachments/originals/invoice.pdf",
                    "content_sha256": "abc123",
                    "scope": "system_chain_of_custody_only_not_real_world_authenticity",
                },
            )
        ],
        allowed_check_ids=["check-traceability"],
    )

    assert "system_provenance" not in sandbox.list_sources()["sources"][0]
    read = sandbox.read_source("invoice-upload")
    assert read["source"]["system_provenance"]["attachment_id"] == "att-42"

    bound = sandbox.bind_claim(
        subject="invoice-upload",
        predicate="system_provenance_traceable",
        value=True,
        source_id="invoice-upload",
        quote='"original_ref": "attachments/originals/invoice.pdf"',
        locator="system_provenance.original_ref",
        confidence="high",
    )

    assert bound["ok"] is True
    assert bound["claim"]["quote"] == '"original_ref": "attachments/originals/invoice.pdf"'
    assert "authentic" not in bound["claim"]["predicate"]


@pytest.mark.parametrize(
    ("locator", "error_code"),
    [
        ("", "LOCATOR_INVALID"),
        (0, "LOCATOR_INVALID"),
        ("line 99", "LOCATOR_OUT_OF_RANGE"),
        ("lines 3-2", "LOCATOR_OUT_OF_RANGE"),
    ],
)
def test_invalid_locator_is_repairable_and_never_enters_ir(
    locator: str | int,
    error_code: str,
) -> None:
    sandbox = _sandbox()
    sandbox.read_source("invoice-1")

    result = _bind_total(sandbox, locator=locator)

    assert result["ok"] is False
    assert result["error"]["code"] == error_code
    assert result["error"]["repair"]
    assert sandbox.evidence_ir.claims == []


def test_claim_admission_is_append_only_and_semantically_idempotent() -> None:
    sandbox = _sandbox()
    sandbox.read_source("invoice-1")

    created = _bind_total(sandbox, locator=3)
    duplicate = _bind_total(sandbox, locator="3", claim_id="model-invented-id")

    assert created["ok"] is True
    assert created["created"] is True
    assert created["claim"]["locator"] == "line 3"
    assert created["claim"]["attributes"] == {"basis": "gross", "currency": "GBP"}
    assert duplicate["ok"] is True
    assert duplicate["created"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["claim"]["id"] == created["claim"]["id"]
    assert len(sandbox.evidence_ir.claims) == 1

    detached = sandbox.evidence_ir
    detached.claims.clear()
    assert len(sandbox.evidence_ir.claims) == 1


def test_conflicting_claim_id_and_schema_error_do_not_mutate_ir() -> None:
    sandbox = _sandbox()
    sandbox.read_source("invoice-1")
    first = _bind_total(sandbox, claim_id="claim-fixed")
    assert first["ok"] is True

    conflict = sandbox.bind_claim(
        claim_id="claim-fixed",
        subject="invoice:INV-42",
        predicate="invoice.status",
        value="open",
        source_id="invoice-1",
        quote="Status: open",
        locator="line 4",
    )
    invalid_confidence = sandbox.bind_claim(
        subject="invoice:INV-42",
        predicate="invoice.status",
        value="open",
        source_id="invoice-1",
        quote="Status: open",
        locator="line 4",
        confidence="certain",
    )

    assert conflict["error"]["code"] == "CLAIM_ID_CONFLICT"
    assert invalid_confidence["error"]["code"] == "CLAIM_SHAPE_INVALID"
    assert len(sandbox.evidence_ir.claims) == 1


def test_submit_check_accepts_only_plan_checks_and_admitted_claims() -> None:
    sandbox = _sandbox()

    wrong_check = sandbox.submit_check(check_id="invented", claim_ids=[])
    missing_claim = sandbox.submit_check(check_id="check-total", claim_ids=["claim-missing"])
    assert wrong_check["error"]["code"] == "CHECK_NOT_IN_PLAN"
    assert missing_claim["error"]["code"] == "CLAIM_REFERENCE_NOT_FOUND"
    assert sandbox.submissions == ()

    sandbox.read_source("invoice-1")
    claim_id = _bind_total(sandbox)["claim"]["id"]
    created = sandbox.submit_check(
        check_id="check-total",
        claim_ids=[claim_id, claim_id],
        note="Ready for independent verification",
    )
    duplicate = sandbox.submit_check(
        check_id="check-total",
        claim_ids=[claim_id],
        note="Ready for independent verification",
        submission_id="ignored-for-semantic-duplicate",
    )

    assert created["created"] is True
    assert created["submission"]["claim_ids"] == [claim_id]
    assert duplicate["created"] is False
    assert duplicate["submission"]["submission_id"] == created["submission"]["submission_id"]
    assert len(sandbox.submissions) == 1


def test_seeded_ir_is_preserved_and_public_capabilities_stay_narrow() -> None:
    seeded = Claim(
        id="claim-seeded",
        subject="vendor:ACME",
        predicate="vendor.status",
        value="active",
        source_id="historical-source",
        quote="ACME is active",
        locator="line 1",
        confidence="high",
    )
    sandbox = _sandbox(
        ir=EvidenceIR(schema_version="seed-v1", source_ids=["historical-source"], claims=[seeded])
    )

    assert sandbox.evidence_ir.schema_version == "seed-v1"
    assert sandbox.evidence_ir.source_ids == ["historical-source", "invoice-1"]
    assert [claim.id for claim in sandbox.evidence_ir.claims] == ["claim-seeded"]
    assert sandbox.capability_names == (
        "list_sources",
        "read_source",
        "bind_claim",
        "submit_check",
    )
    assert not hasattr(sandbox, "run_shell")
    assert not hasattr(sandbox, "run_python")
    assert not hasattr(sandbox, "write_case")
