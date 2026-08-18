from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from pydantic import ValidationError

from .models import Claim, EvidenceIR


_LINE_LOCATOR = re.compile(r"\blines?\s+(\d+)(?:\s*[-:]\s*(\d+))?\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """A run-local source. Content is exposed only after ``read_source``."""

    source_id: str
    content: str
    title: str = ""
    kind: str = "unknown"

    def __post_init__(self) -> None:
        source_id = self.source_id.strip()
        if not source_id:
            raise ValueError("source_id must not be blank")
        if not isinstance(self.content, str):
            raise TypeError("source content must be text")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "kind", self.kind.strip() or "unknown")


@dataclass(frozen=True, slots=True)
class CheckSubmission:
    submission_id: str
    check_id: str
    claim_ids: tuple[str, ...]
    note: str = ""


class EvidenceSandbox:
    """Small, in-memory capability boundary for one evidence review run.

    The sandbox deliberately has no file, shell, Python, policy, or CaseStore
    capability. Invalid model actions return repairable results and leave the
    accepted IR and check submissions unchanged.
    """

    capability_names = ("list_sources", "read_source", "bind_claim", "submit_check")

    def __init__(
        self,
        *,
        sources: Iterable[SourceRecord],
        allowed_check_ids: Iterable[str],
        evidence_ir: EvidenceIR | None = None,
    ) -> None:
        source_rows = list(sources)
        self._sources = {row.source_id: row for row in source_rows}
        if len(self._sources) != len(source_rows):
            raise ValueError("source_id values must be unique")

        self._allowed_check_ids = frozenset(
            check_id.strip() for check_id in allowed_check_ids if check_id.strip()
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
    def read_source_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._read_source_ids))

    @property
    def source_records(self) -> tuple[SourceRecord, ...]:
        return tuple(self._sources[source_id] for source_id in sorted(self._sources))

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
        if quote not in source.content:
            return self._failure(
                "bind_claim",
                code="QUOTE_NOT_IN_SOURCE",
                message="The quote is not an exact substring of the selected source.",
                repair="Read the source again and copy the quote exactly, including case and spacing.",
                source_id=source_id,
            )

        normalized_locator, locator_error = self._normalize_locator(locator, source.content)
        if locator_error is not None:
            return self._failure("bind_claim", source_id=source_id, **locator_error)

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

    def submit_check(
        self,
        *,
        check_id: str,
        claim_ids: Sequence[str] = (),
        note: str = "",
        submission_id: str = "",
    ) -> dict[str, Any]:
        check_id = check_id.strip()
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

        note = note.strip()
        fingerprint = self._digest(
            {"check_id": check_id, "claim_ids": sorted(normalized_claim_ids), "note": note}
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
            note=note,
        )
        self._submissions.append(submission)
        self._submission_by_id[submission_id] = submission
        self._submission_by_fingerprint[fingerprint] = submission
        return self._success(
            "submit_check",
            created=True,
            duplicate=False,
            submission=self._dump_submission(submission),
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
