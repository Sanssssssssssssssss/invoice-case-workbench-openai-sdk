from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ProofTerm = Literal["CLAIM", "BINDING", "WITNESS"]
RootComposition = Literal["ALL_REQUIRED", "ANY_SUFFICIENT"]
SemanticRole = Literal[
    "COMPONENT_OBSERVATION",
    "COMPONENT_APPLICABILITY",
    "COMPONENT_RECONCILIATION",
]


def _non_empty(value: str, field_name: str) -> str:
    result = value.strip()
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


class _SignatureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProofFacet(_SignatureModel):
    id: str
    minimum_proof_terms: list[ProofTerm]
    semantic_contract: str = ""
    required_semantic_roles: list[SemanticRole] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "facet id")

    @field_validator("minimum_proof_terms")
    @classmethod
    def validate_terms(cls, value: list[ProofTerm]) -> list[ProofTerm]:
        if not value:
            raise ValueError("minimum_proof_terms must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("minimum_proof_terms must not contain duplicates")
        return value

    @field_validator("semantic_contract")
    @classmethod
    def validate_semantic_contract(cls, value: str) -> str:
        return value.strip()

    @field_validator("required_semantic_roles")
    @classmethod
    def validate_semantic_roles(cls, value: list[SemanticRole]) -> list[SemanticRole]:
        if len(set(value)) != len(value):
            raise ValueError("required_semantic_roles must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_semantic_shape(self) -> ProofFacet:
        if self.required_semantic_roles and not self.semantic_contract:
            raise ValueError("required_semantic_roles require a semantic_contract")
        return self


class ProofSignature(_SignatureModel):
    signature_id: str
    version: str
    requirement_id: str
    root_composition: RootComposition
    required_policy_refs: list[str] = Field(default_factory=list)
    facets: list[ProofFacet]

    @field_validator("signature_id", "version", "requirement_id")
    @classmethod
    def validate_text(cls, value: str, info) -> str:
        return _non_empty(value, info.field_name)

    @field_validator("required_policy_refs")
    @classmethod
    def validate_policy_refs(cls, value: list[str]) -> list[str]:
        result = [_non_empty(item, "required_policy_refs") for item in value]
        if len(set(result)) != len(result):
            raise ValueError("required_policy_refs must not contain duplicates")
        return result

    @model_validator(mode="after")
    def validate_facets(self) -> ProofSignature:
        if not self.facets:
            raise ValueError("facets must not be empty")
        facet_ids = [facet.id for facet in self.facets]
        if len(set(facet_ids)) != len(facet_ids):
            raise ValueError("facet ids must be unique")
        return self


__all__ = [
    "ProofFacet",
    "ProofSignature",
    "ProofTerm",
    "RootComposition",
    "SemanticRole",
]
