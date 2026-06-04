from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.tools.catalog import RiskLevel, ToolSpec


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requires_approval: bool = False
    risk_level: RiskLevel = "read"
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolApprovalPolicy:
    def evaluate(self, *, spec: ToolSpec, tool_input: dict[str, Any], internal: bool = False) -> ApprovalDecision:
        if internal or spec.approval_mode == "never":
            return ApprovalDecision(requires_approval=False, risk_level=spec.risk_level)

        if spec.approval_mode == "always" or spec.risk_level in {"external_write", "destructive", "privileged"}:
            reason = f"{spec.name} requires approval before execution."
            return ApprovalDecision(
                requires_approval=True,
                risk_level=spec.risk_level,
                reason=reason,
                payload={
                    "tool": spec.name,
                    "risk_level": spec.risk_level,
                    **_input_summary(tool_input),
                    "reason": reason,
                },
            )

        return ApprovalDecision(requires_approval=False, risk_level=spec.risk_level)

    def evaluate_no_tool(self) -> ApprovalDecision:
        return ApprovalDecision(requires_approval=False, risk_level="read")


def _input_summary(tool_input: dict[str, Any]) -> dict[str, str]:
    text = json.dumps(tool_input or {}, ensure_ascii=False, default=str, sort_keys=True)
    return {
        "input_preview": _redact_local_paths(text)[:900],
        "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\"'\n\r]+")


def _redact_local_paths(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        basename = raw.replace("/", "\\").rstrip("\\").split("\\")[-1]
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        return f"<local_path basename={basename!r} sha256={digest}>"

    return _WINDOWS_PATH_RE.sub(repl, text)
