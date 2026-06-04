from __future__ import annotations

from typing import Any

from app.agents.capabilities import ROLE_CAPABILITIES, RoleCapability, role_capability
from app.agents.patch_builder.agent import CasePatchWriter
from app.agents.patch_builder.agent import SYSTEM_PROMPT as CASE_PATCH_WRITER_PROMPT
from app.agents.evidence_reviewer.agent import EvidenceReviewer
from app.agents.evidence_reviewer.agent import SYSTEM_PROMPT as EVIDENCE_REVIEWER_PROMPT
from app.agents.materials_advisor.agent import MaterialsAdvisor
from app.agents.materials_advisor.agent import SYSTEM_PROMPT as MATERIALS_ADVISOR_PROMPT
from app.agents.report_writer.agent import ReportWriter
from app.agents.report_writer.agent import SYSTEM_PROMPT as REPORT_WRITER_PROMPT
from app.llm import LlmClient


_ROLE_PROMPTS = {
    "materials_advisor": MATERIALS_ADVISOR_PROMPT,
    "evidence_reviewer": EVIDENCE_REVIEWER_PROMPT,
    "case_patch_writer": CASE_PATCH_WRITER_PROMPT,
    "report_writer": REPORT_WRITER_PROMPT,
}


class RoleRegistry:
    def __init__(self, llm: LlmClient | None = None) -> None:
        self.llm = llm or LlmClient()
        self._roles = {
            "materials_advisor": MaterialsAdvisor(self.llm),
            "evidence_reviewer": EvidenceReviewer(self.llm),
            "case_patch_writer": CasePatchWriter(self.llm),
            "report_writer": ReportWriter(self.llm),
        }

    def call(self, role: str, payload: dict[str, Any]) -> dict[str, Any]:
        if role not in self._roles:
            raise KeyError(f"Unknown role: {role}")
        return self._roles[role].run(**payload)

    def prompt(self, role: str) -> str:
        return _ROLE_PROMPTS.get(role, "")

    def prompt_file(self, role: str) -> str:
        try:
            return self.capability(role).prompt_file
        except KeyError:
            return ""

    def prompt_version(self, role: str) -> str:
        return self.capability(role).prompt_version

    def capability(self, role: str) -> RoleCapability:
        if role not in self._roles:
            raise KeyError(f"Unknown role: {role}")
        return role_capability(role)

    def trace_metadata(self, role: str) -> dict[str, Any]:
        return self.capability(role).trace_metadata()

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(ROLE_CAPABILITIES.keys())
