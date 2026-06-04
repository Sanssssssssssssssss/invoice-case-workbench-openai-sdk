from __future__ import annotations

from app.prompt_loader import load_system_prompt


def test_patch_writer_requires_case_summaries_for_new_evidence() -> None:
    prompt = load_system_prompt("agents/patch_builder/prompt.md")

    assert "case_updates.summary" in prompt
    assert "不要写 null" in prompt
    assert "conversation_summary" in prompt
    assert "用户本轮提交了什么和想做什么" in prompt
    assert "不要在 invoice-only 摘要里默认写 PO、GRN、供应商主数据或重复付款检查" in prompt
    assert "仍需补充PO" not in prompt


def test_patch_writer_carries_dynamic_requirements_without_setting_status() -> None:
    prompt = load_system_prompt("agents/patch_builder/prompt.md")

    assert "Default invoice-only blocker requirements" in prompt
    assert "`required=true`" in prompt
    assert "Default invoice-only optional quality requirements" in prompt
    assert "`required=false`" in prompt
    assert "AP lite payment review profile requirements" in prompt
    assert "goods_receipt_or_service_acceptance" in prompt
    assert "Do not set requirement status, missing_materials" in prompt
    assert "Missing/weak optional requirements are quality notes, not blockers" in prompt
    assert "Do not add `risk_flags` or `next_questions` merely because optional invoice-only fields are missing or weak" in prompt
    assert "Phrase optional signature/template/line item checks as \"可选补强\"" in prompt
    assert "do not add future-scope prompts such as \"if you need AP review...\"" in prompt
    assert "treat that as a prohibition, not as a request for AP options" in prompt
    assert "do not carry `signature_or_authorized_signatory_unclear`" in prompt
    assert "Do not calculate new totals from amount and tax fields" in prompt
