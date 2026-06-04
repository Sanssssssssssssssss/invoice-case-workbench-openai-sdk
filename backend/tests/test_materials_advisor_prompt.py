from __future__ import annotations

from app.agents.materials_advisor.agent import SYSTEM_PROMPT
from app.state.schemas import MaterialsAdvisorResult


def test_materials_advisor_prompt_includes_task_skill() -> None:
    assert "name: materials_advisor" in SYSTEM_PROMPT
    assert "Materials Advisor Task Skill" in SYSTEM_PROMPT
    assert "补料任务" in SYSTEM_PROMPT
    assert "找谁拿" in SYSTEM_PROMPT
    assert "材料/格式" in SYSTEM_PROMPT
    assert "不要承诺状态会变成 `accepted`, `satisfied`" in SYSTEM_PROMPT
    assert "Do not promise `accepted`, `satisfied`" in SYSTEM_PROMPT
    assert "不要发明 case_state 中没有出现的文档号" in SYSTEM_PROMPT
    assert "Preserve conflict direction exactly" in SYSTEM_PROMPT
    assert "avoid \"是否已付款\"" in SYSTEM_PROMPT
    assert "Do not imply that policy/RAG is case evidence" in SYSTEM_PROMPT
    assert "Advisor-Owned RAG Addendum v4.2" in SYSTEM_PROMPT
    assert "Planner does not call RAG directly for rules or material guidance" in SYSTEM_PROMPT
    assert "Follow-Up Detail Addendum v4.4" in SYSTEM_PROMPT
    assert "为什么不符合" in SYSTEM_PROMPT
    assert "哪里不完整" in SYSTEM_PROMPT
    assert "template_match" in SYSTEM_PROMPT
    assert "does not block report generation" in SYSTEM_PROMPT
    assert "answer only with invoice-field and visual-quality tasks" in SYSTEM_PROMPT
    assert "looks_like_invoice" in SYSTEM_PROMPT
    assert "Structured Task Contract v4.3" in SYSTEM_PROMPT
    assert "tasks` is the authoritative machine-readable" in SYSTEM_PROMPT
    assert "`tasks[]` list is the Planner/UI contract" in SYSTEM_PROMPT
    assert "Invoice-Only Field Requirements" in SYSTEM_PROMPT
    assert "AP Lite Requirements" in SYSTEM_PROMPT
    assert "Legacy AP Aliases" in SYSTEM_PROMPT
    assert "`vendor_record` maps to `vendor_identity`" in SYSTEM_PROMPT
    assert "`duplicate_payment_check` maps to `duplicate_payment_screen`" in SYSTEM_PROMPT
    assert "Prefer AP lite ids for newly generated tasks" in SYSTEM_PROMPT
    for field in (
        "task",
        "requirement",
        "current_status",
        "why_insufficient",
        "owner",
        "format",
        "required_fields",
        "impact",
    ):
        assert field in SYSTEM_PROMPT


def test_materials_advisor_result_accepts_structured_tasks() -> None:
    result = MaterialsAdvisorResult.model_validate(
        {
            "answer": "### 补料任务",
            "tasks": [
                {
                    "task": "请提供原始发票 PDF",
                    "requirement": "source_traceability",
                    "current_status": "missing",
                    "why_insufficient": "当前只有口头描述",
                    "owner": "供应商或 AP 发票收件人",
                    "format": "原始 PDF 或清晰图片",
                    "required_fields": ["发票号", "供应商", "金额"],
                    "impact": "用于审查 source_traceability",
                }
            ],
            "missing_materials": ["source_traceability"],
            "next_questions": [],
        }
    )

    assert result.tasks[0].requirement == "source_traceability"
    assert result.tasks[0].required_fields == ["发票号", "供应商", "金额"]
