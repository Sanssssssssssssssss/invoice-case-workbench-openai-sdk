from __future__ import annotations

import re
from typing import Any


BLOCKED_FINAL_PATTERNS = (
    re.compile(r"(?:我|我们|本系统|本 agent|agent|workbench|系统)\s*(?:已|已经)?\s*(?:付款|支付|批准|过账|提交|提交\s*ERP|路由)", re.I),
    re.compile(r"(?:^|[。！!\n])\s*(?:已|已经)\s*(?:付款|支付|过账|提交\s*ERP)(?:[。！!，,]|$)", re.I),
    re.compile(r"(?:^|[。！!\n])\s*(?:已|已经)\s*批准(?:[。！!，,]|$)", re.I),
    re.compile(r"(?:发票|款项|付款|该票|本票|case|案件)\s*(?:已|已经)\s*(?:付款|支付|批准|过账|提交)", re.I),
    re.compile(r"(?:已|已经)\s*(?:付款|支付|批准|过账|提交)\s*(?:发票|款项|付款|erp|case|案件)", re.I),
    re.compile(r"\b(?:I|we|agent|workbench|system)\s+(?:have|has|already\s+)?(?:paid|approved|posted|routed|submitted)\b", re.I),
    re.compile(r"\b(?:invoice|payment|erp|case)\s+(?:has been|have been|is|was|were)\s+(?:paid|approved|posted|routed|submitted)\b", re.I),
    re.compile(r"\b(?:approved\s+and\s+paid|paid\s+and\s+approved)\b", re.I),
    re.compile(r"\b(?:paid|approved|posted|routed|submitted)\s+(?:the\s+)?(?:invoice|payment|erp|case)\b", re.I),
    re.compile(r"(?:是否|要不要|需不需要|确认是否|请确认是否)\s*(?:提交|推送|递交)\s*(?:至|到)?\s*ERP", re.I),
    re.compile(r"(?:可|可以|能够|能|支持|帮你|我会|我可以)\s*(?:直接)?\s*(?:提交|推送|递交)\s*(?:至|到)?\s*ERP", re.I),
    re.compile(r"(?:可|可以)\s*(?:直接)?\s*(?:付款|支付|审批|批准|提交)", re.I),
    re.compile(r"(?:推进|推动|发起)\s*付款\s*(?:审批|流程)", re.I),
    re.compile(r"(?:是否|有无|有没有|确认是否|排查是否)\s*(?:已付款|已经付款|已支付|已经支付)", re.I),
    re.compile(r"\b(?:submit|route|push)\s+(?:to\s+)?ERP\b", re.I),
    re.compile(r"\b(?:can|could|ready to)\s+(?:pay|approve|submit|post|route)\b", re.I),
    re.compile(r"\b(?:whether|if).{0,80}(?:has\s+)?(?:already\s+)?(?:been\s+)?(?:paid|approved|posted|submitted)\b", re.I),
)

DANGEROUS_INSTRUCTION_REPRODUCTION_PATTERNS = (
    re.compile(r"(?:虚假声明|诱导|指令|prompt injection|提示注入).{0,30}(?:已|已经)\s*(?:付款|支付|审批|批准|过账|提交)", re.I),
    re.compile(r"(?:Ignore all previous rules|ignore previous rules).{0,80}(?:paid|approved|submit|ERP)", re.I),
)

COMPLETE_CASE_CLAIM_PATTERNS = (
    re.compile(r"(?:五项|全部|所有|核心|必需).{0,12}(?:均|都|全部)?(?:已|已经)?(?:满足|齐全|通过)"),
    re.compile(r"(?:均|全部|全都)(?:已|已经)?满足"),
    re.compile(r"证据链完整(?!性|后)"),
    re.compile(r"(?<!待)材料(?:已|已经)?齐全(?!后)"),
    re.compile(r"全部齐全"),
    re.compile(r"(?:状态|status)\s*(?:为|=|已更新为)\s*ready_for_report", re.I),
)

NEGATED_COMPLETE_CASE_CLAIM_PATTERNS = (
    re.compile(r"(?:未|尚未|不|不能|缺失|缺少|缺口|不足).{0,16}(?:满足|齐全|通过|完整|ready_for_report)"),
    re.compile(r"(?:五项|全部|所有|核心|必需).{0,16}(?:未|尚未|不|不能|缺失|缺少|缺口|不足)"),
)


class NoExecutionWordingError(ValueError):
    pass


class CaseStateConsistencyError(ValueError):
    pass


def enforce_no_execution_wording(text: str) -> str:
    value = _strip_guardrail_echoes(str(text or ""))
    for pattern in DANGEROUS_INSTRUCTION_REPRODUCTION_PATTERNS:
        if pattern.search(value):
            raise NoExecutionWordingError(f"Final answer reproduces dangerous execution instruction: {pattern.pattern}")
    for pattern in BLOCKED_FINAL_PATTERNS:
        for match in pattern.finditer(value):
            if _is_negated_execution_mention(value, match.start()):
                continue
            raise NoExecutionWordingError(f"Final answer contains blocked execution wording: {pattern.pattern}")
    return value


def _strip_guardrail_echoes(value: str) -> str:
    """Remove examples of prohibited words when the reply is only describing guardrails."""
    return re.sub(
        r"[（(][^）)]*(?:payable|paid|approved|posted|submitted)[^）)]*[）)]",
        "",
        value,
        flags=re.I,
    )


def enforce_case_state_consistency(text: str, case_state: Any) -> str:
    value = str(text or "")
    if not _claims_complete_case(value):
        return text
    errors = _complete_case_state_errors(case_state)
    if errors:
        raise CaseStateConsistencyError("; ".join(errors))
    return text


def _claims_complete_case(text: str) -> bool:
    for pattern in COMPLETE_CASE_CLAIM_PATTERNS:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 18) : min(len(text), match.end() + 18)]
            if any(negated.search(window) for negated in NEGATED_COMPLETE_CASE_CLAIM_PATTERNS):
                continue
            return True
    return False


def _complete_case_state_errors(case_state: Any) -> list[str]:
    requirements = list(getattr(case_state, "requirements", []) or [])
    statuses = {str(getattr(item, "id", "")): str(getattr(item, "status", "")) for item in requirements}
    required_ids = [
        str(getattr(item, "id", "") or "")
        for item in requirements
        if str(getattr(item, "id", "") or "") and bool(getattr(item, "required", True))
    ]
    optional_conflicts = [
        str(getattr(item, "id", "") or "")
        for item in requirements
        if str(getattr(item, "id", "") or "") and not bool(getattr(item, "required", True)) and str(getattr(item, "status", "")) == "conflict"
    ]
    blocking_ids = sorted(set(required_ids + optional_conflicts))
    unsatisfied = sorted(
        requirement
        for requirement in required_ids
        if statuses.get(requirement) not in {"accepted", "satisfied"}
    )
    errors: list[str] = []
    if not required_ids and not optional_conflicts:
        errors.append("complete claim conflicts with empty requirements")
    if unsatisfied:
        errors.append(f"complete claim conflicts with unsatisfied requirements: {unsatisfied}")
    if optional_conflicts:
        errors.append(f"complete claim conflicts with optional requirement conflicts: {sorted(optional_conflicts)}")
    evidence_items = list(getattr(case_state, "evidence_items", []) or [])
    supported_requirements = {
        str(getattr(support, "requirement", "") or "")
        for item in evidence_items
        for support in list(getattr(item, "supports", []) or [])
    }
    missing_support = sorted(set(blocking_ids) - supported_requirements)
    if missing_support:
        errors.append(f"complete claim missing evidence support for requirements: {missing_support}")
    return errors


def _is_negated_execution_mention(value: str, start: int) -> bool:
    prefix = value[max(0, start - 18) : start]
    window = value[max(0, start - 24) : min(len(value), start + 36)]
    if re.search(r"(不代表|不能证明|不是|并非|没有|未|无法|不等于|不能说|不能写|不应写|不应视为|不可以|不可|不能|不得|不要)$", prefix.strip()):
        return True
    return any(token in window for token in ("风险", "降低", "防范", "可能", "检查", "检索", "重复"))
