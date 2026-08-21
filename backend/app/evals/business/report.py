from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import BusinessEvalCase, BusinessEvalOracle, EvalResult, EvalSnapshot


_STAGE_LABELS = {
    "understanding": "任务理解",
    "evidence": "证据与来源",
    "reasoning": "业务核查",
    "proof": "Proof 与投影",
    "report": "业务报告",
    "communication": "中文沟通",
}


def render_eval_report(
    case: BusinessEvalCase,
    oracle: BusinessEvalOracle,
    snapshot: EvalSnapshot,
    result: EvalResult,
) -> str:
    """Render the small, developer-facing report without hidden model reasoning."""
    status = "PASS" if result.passed else "VETO" if result.vetoes else "FAIL"
    first_failure = _STAGE_LABELS.get(result.first_failed_stage, result.first_failed_stage) or "无"
    lines = [
        f"# Business Eval：{case.title}",
        "",
        f"- 结果：**{status}**",
        f"- 得分：**{_number(result.score)}/100**",
        f"- 首个失败阶段：**{first_failure}**",
        f"- 案例：`{case.case_id}@{case.case_version}`",
        f"- 运行：`{snapshot.run_id}`；评分器：`{result.scorer_version}`",
        "",
        "## 一票否决",
        "",
    ]
    raw_score = result.raw_score if result.raw_score is not None else result.score
    if result.score_cap < Decimal("100"):
        lines[5:5] = [
            f"- 原始得分：**{_number(raw_score)}/100**",
            f"- 失败封顶：**{_number(result.score_cap)}/100**",
            f"- 封顶原因：{result.score_cap_reason or '未记录'}",
        ]
    if result.vetoes:
        lines.extend(f"- `{item.code}`：{item.detail}" for item in result.vetoes)
    else:
        lines.append("- 无")

    lines.extend(
        [
            "",
            "## 阶段得分",
            "",
            "| 阶段 | 得分 | 核心检查 | 结果 |",
            "|---|---:|---:|---|",
        ]
    )
    for stage, label in _STAGE_LABELS.items():
        stage_checks = [item for item in result.checks if item.stage == stage]
        earned = sum((item.earned for item in stage_checks), Decimal("0"))
        possible = sum((item.points for item in stage_checks), Decimal("0"))
        failed_core = sum(1 for item in stage_checks if item.core and not item.passed)
        stage_status = "PASS" if all(item.passed for item in stage_checks) else "FAIL"
        lines.append(
            f"| {label} | {_number(earned)}/{_number(possible)} | {failed_core} 项失败 | {stage_status} |"
        )

    failed = [item for item in result.checks if not item.passed]
    lines.extend(["", "## 期望与实际", ""])
    if failed:
        lines.extend(
            [
                "| 检查 | 期望 | 实际 | 核心 |",
                "|---|---|---|---|",
            ]
        )
        for item in failed:
            lines.append(
                "| {check} | {expected} | {observed} | {core} |".format(
                    check=_escape(item.id),
                    expected=_escape(_brief(item.expected)),
                    observed=_escape(_brief(item.observed)),
                    core="是" if item.core else "否",
                )
            )
    else:
        lines.append("全部原子检查通过。")

    report_checks = [item for item in result.checks if item.stage == "report"]
    lines.extend(["", "## 业务报告核对", ""])
    for artifact in snapshot.reports:
        lines.append(f"- `{artifact.kind}`：`{artifact.path}`（{artifact.bytes} bytes）")
    if not snapshot.reports:
        lines.append("- 未生成报告产物")
    for item in report_checks:
        lines.append(f"- {'通过' if item.passed else '失败'}：`{item.id}`")

    lines.extend(["", "## 工程指标", ""])
    for key in (
        "provider_calls",
        "role_calls",
        "api_prompt_tokens",
        "api_completion_tokens",
        "api_total_tokens",
        "api_cached_tokens",
        "role_total_tokens",
        "role_cached_tokens",
        "duration_ms",
        "error_events",
        "blocked_actions",
        "hook_rejections",
        "report_count",
        "report_bytes",
    ):
        lines.append(f"- `{key}`：{result.engineering.get(key, 0)}")
    first_error = result.engineering.get("first_error")
    if isinstance(first_error, dict) and first_error:
        lines.append(
            "- `first_error`：seq={seq}；{name}；{summary}".format(
                seq=first_error.get("seq", 0),
                name=first_error.get("name") or first_error.get("kind") or "unknown",
                summary=_brief(first_error.get("summary", ""), limit=500),
            )
        )

    lines.extend(["", "## 完整可见对话", ""])
    visible_messages = list(_visible_conversation(snapshot.conversation))
    if not visible_messages:
        lines.append("（没有用户或助手可见消息）")
    for index, (role, text) in enumerate(visible_messages, start=1):
        label = "用户" if role == "user" else "助手"
        lines.extend([f"### {index}. {label}", "", text or "（空）", ""])

    lines.extend(["## 原始记录", ""])
    trace_path = str(result.engineering.get("trace_path") or "")
    transcript_path = str(result.engineering.get("transcript_path") or "")
    lines.append(f"- Trace：`{trace_path or '见 snapshot.json 的 trace 字段'}`")
    lines.append(f"- 模型调用记录：`{transcript_path or '未提供'}`")
    lines.append("- 本报告未复制隐藏思维链；仅包含应用中用户可见的对话。")

    lines.extend(["", "## 下一项建议", ""])
    if failed:
        lines.append(f"优先检查 `{failed[0].id}`：{failed[0].detail or '核对该层输入与输出。'}")
    elif result.vetoes:
        lines.append(f"优先修复 `{result.vetoes[0].code}`，保留本次快照用于零 API 重评分。")
    else:
        lines.append("当前案例通过；新增案例前保持评分器与 Oracle 不变。")
    return "\n".join(lines).rstrip() + "\n"


def write_eval_report(
    path: Path,
    case: BusinessEvalCase,
    oracle: BusinessEvalOracle,
    snapshot: EvalSnapshot,
    result: EvalResult,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_eval_report(case, oracle, snapshot, result), encoding="utf-8")
    return path


def _number(value: Decimal) -> str:
    integer, fraction = format(value.quantize(Decimal("0.01")), "f").split(".")
    fraction = fraction.rstrip("0")
    return f"{integer}.{fraction}" if fraction else integer


def _brief(value: Any, *, limit: int = 180) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _visible_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_visible_content(item) for item in value)))
    if not isinstance(value, dict):
        return ""
    block_type = str(value.get("type") or "").casefold()
    if "reasoning" in block_type or "thinking" in block_type:
        return ""
    for key in ("text", "content", "message"):
        if key in value:
            return _visible_content(value[key])
    return ""


def _visible_conversation(conversation: list[dict[str, Any]]):
    for message in conversation:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").casefold()
        if role not in {"user", "assistant"}:
            continue
        yield role, _visible_content(message.get("content", message.get("text", ""))).strip()
