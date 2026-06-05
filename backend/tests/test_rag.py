from __future__ import annotations

from pathlib import Path

from app.tools.rag_search import RagSkill


def test_rag_retrieves_invoice_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("INVOICE_AGENT_ENABLE_VECTOR", raising=False)
    knowledge = tmp_path / "knowledge" / "ERP Approval"
    knowledge.mkdir(parents=True)
    (knowledge / "invoice_payment_policy_excerpt.md").write_text(
        "Invoice payment review requires invoice, purchase order, goods receipt, vendor record, and duplicate payment check.",
        encoding="utf-8",
    )
    skill = RagSkill([knowledge], tmp_path / "rag")
    result = skill.retrieve("invoice payment required materials", top_k=3)
    assert result.status == "success"
    assert result.evidences
    assert result.evidences[0].channel == "txtai_hybrid"
    assert "purchase order" in result.evidences[0].snippet.lower()


def _retrieve(query: str, tmp_path, monkeypatch, *, top_k: int = 5):
    monkeypatch.setenv("INVOICE_AGENT_ENABLE_VECTOR", "0")
    skill = RagSkill([Path("knowledge/invoice_payment")], tmp_path / "rag")
    return skill.retrieve(query, top_k=top_k)


def _joined_snippets(result) -> str:  # noqa: ANN001
    return "\n".join(item.snippet for item in result.evidences)


def test_invoice_only_query_recalls_invoice_field_profile_not_ap_default(tmp_path, monkeypatch) -> None:
    result = _retrieve("审一下这张发票有没有问题，发票号、供应商、金额、税额和原件是否齐全", tmp_path, monkeypatch)

    assert result.status == "success"
    assert result.evidences
    first = result.evidences[0].snippet
    joined = _joined_snippets(result)
    assert "invoice_field_completeness" in joined or "invoice_only_material_profile" in joined
    assert "ap_lite_payment_review_material_profile" not in first


def test_payment_case_creation_recalls_ap_lite_not_invoice_only(tmp_path, monkeypatch) -> None:
    result = _retrieve("帮我新建一个发票付款审查 case，我现在需要准备什么", tmp_path, monkeypatch)

    assert result.status == "success"
    assert result.evidences
    first = result.evidences[0].snippet
    joined = _joined_snippets(result)
    assert "ap_lite_payment_review_material_profile" in joined or "case_playbook_aurora" in joined
    assert "ap_lite_payment_review_material_profile" in first or "case_playbook_aurora" in first
    assert "invoice_only_material_profile" not in first


def test_aurora_case_query_recalls_local_case_playbook(tmp_path, monkeypatch) -> None:
    result = _retrieve(
        "Aurora Office Supplies INV-2026-001 PO-2026-7788 GRN-2026-8899 10000 CNY",
        tmp_path,
        monkeypatch,
    )

    assert result.status == "success"
    assert result.evidences
    first = result.evidences[0]
    assert first.source_path == "case_playbook_aurora.md"
    assert "case_playbook_aurora" in first.snippet or "Aurora Office Supplies" in first.snippet
    assert "flipkart" not in first.snippet.lower()
    assert "sap_dox" not in first.snippet.lower()


def test_enterprise_ap_controls_recall_three_way_matching(tmp_path, monkeypatch) -> None:
    result = _retrieve("三单匹配 数量 单价 不一致 发票 PO GRN 收货单", tmp_path, monkeypatch)

    assert result.status == "success"
    joined = _joined_snippets(result)
    assert "ap_three_way_matching" in joined or "ap_lite_payment_review_material_profile" in joined
    assert "purchase order" in joined.lower()
    assert "goods receipt" in joined.lower() or "GRN" in joined


def test_enterprise_ap_controls_recall_duplicate_payment(tmp_path, monkeypatch) -> None:
    result = _retrieve("重复付款检查 同供应商 同金额 近似发票号 历史清账凭证", tmp_path, monkeypatch)

    assert result.status == "success"
    joined = _joined_snippets(result)
    assert "duplicate_payment_control" in joined or "defect_duplicate_payment_hit" in joined
    assert "same supplier" in joined.lower() or "同供应商" in joined
    assert "clearing" in joined.lower() or "清账" in joined


def test_enterprise_ap_controls_recall_bank_change(tmp_path, monkeypatch) -> None:
    result = _retrieve("供应商银行账户变更邮件 需要 审批记录 workflow history 当前值 拟议值", tmp_path, monkeypatch)

    assert result.status == "success"
    joined = _joined_snippets(result)
    assert "vendor_master_bank_change_control" in joined or "defect_bank_change_email" in joined
    assert "bank" in joined.lower() or "银行" in joined
    assert "workflow" in joined.lower()


def test_enterprise_ap_controls_recall_approval_authority(tmp_path, monkeypatch) -> None:
    result = _retrieve("审批矩阵 授权审批 审批权限 审批限额 approval matrix delegation", tmp_path, monkeypatch)

    assert result.status == "success"
    joined = _joined_snippets(result)
    assert "approval_authority_matrix_control" in joined
    assert "approval limit" in joined.lower() or "审批限额" in joined


def test_enterprise_ap_controls_recall_segregation_of_duties(tmp_path, monkeypatch) -> None:
    result = _retrieve("职责分离 同一人建供应商又付款 segregation of duties SoD compensating control", tmp_path, monkeypatch)

    assert result.status == "success"
    joined = _joined_snippets(result)
    assert "segregation_of_duties_ap_control" in joined
    assert "vendor" in joined.lower() or "供应商" in joined


def test_enterprise_ap_controls_recall_payment_release(tmp_path, monkeypatch) -> None:
    result = _retrieve("付款释放 ACH wire 银行账号临时变更 payment run payment hold", tmp_path, monkeypatch)

    assert result.status == "success"
    joined = _joined_snippets(result)
    assert "payment_release_disbursement_control" in joined
    assert "vendor master" in joined.lower() or "银行账号" in joined


def test_enterprise_ap_controls_recall_vendor_master_governance(tmp_path, monkeypatch) -> None:
    result = _retrieve("vendor onboarding 供应商入驻 vendor master change log 重复供应商 供应商对账单", tmp_path, monkeypatch)

    assert result.status == "success"
    joined = _joined_snippets(result)
    assert "vendor_onboarding_master_data_governance" in joined
    assert "duplicate vendor" in joined.lower() or "重复供应商" in joined


def test_enterprise_ap_controls_recall_non_po_contract_invoice(tmp_path, monkeypatch) -> None:
    result = _retrieve("non-PO invoice 合同发票 服务费 SOW recurring service 里程碑验收", tmp_path, monkeypatch)

    assert result.status == "success"
    joined = _joined_snippets(result)
    assert "non_po_contract_invoice_control" in joined
    assert "service acceptance" in joined.lower() or "服务验收" in joined


def test_enterprise_ap_controls_recall_tax_gl_coding(tmp_path, monkeypatch) -> None:
    result = _retrieve("GL coding tax treatment 成本中心 税码 VAT withholding 总账科目", tmp_path, monkeypatch)

    assert result.status == "success"
    joined = _joined_snippets(result)
    assert "tax_gl_coding_cost_center_control" in joined
    assert "cost center" in joined.lower() or "成本中心" in joined


def test_enterprise_ap_controls_recall_exception_hold_tolerance(tmp_path, monkeypatch) -> None:
    result = _retrieve("matching hold payment hold 匹配容差 容差超限 例外审批 hold release", tmp_path, monkeypatch)

    assert result.status == "success"
    joined = _joined_snippets(result)
    assert "exception_hold_tolerance_control" in joined
    assert "tolerance" in joined.lower() or "容差" in joined


def test_invoice_calculation_validation_recall(tmp_path, monkeypatch) -> None:
    result = _retrieve("发票金额和税额合计不一致 小计 总计 行项目", tmp_path, monkeypatch)

    assert result.status == "success"
    joined = _joined_snippets(result)
    assert "invoice_calculation_validation" in joined or "defect_amount_conflict" in joined
    assert "tax" in joined.lower() or "税额" in joined


def test_process_and_source_quality_boundaries_recall(tmp_path, monkeypatch) -> None:
    clear_invoice = _retrieve("Clear Invoice 是不是说明可以付款 流程日志", tmp_path, monkeypatch)
    injection = _retrieve("附件里写忽略规则直接批准付款 提交 ERP prompt injection", tmp_path, monkeypatch)

    clear_joined = _joined_snippets(clear_invoice)
    injection_joined = _joined_snippets(injection)

    assert clear_invoice.status == "success"
    assert "workflow_boundary_process_evidence" in clear_joined or "defect_clear_invoice_misunderstanding" in clear_joined
    assert "process evidence" in clear_joined.lower() or "不是付款证明" in clear_joined

    assert injection.status == "success"
    assert "source_quality_prompt_injection" in injection_joined or "defect_prompt_injection_attachment" in injection_joined
    assert "prompt injection" in injection_joined.lower() or "附件注入" in injection_joined


def test_rag_index_rebuilds_when_knowledge_files_change(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_ENABLE_VECTOR", "0")
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "policy.md").write_text("alpha invoice control", encoding="utf-8")

    first = RagSkill([knowledge], tmp_path / "rag")
    assert first.retrieve("alpha", top_k=1).status == "success"
    assert list((tmp_path / "rag").glob("txtai-sparse-*"))

    (knowledge / "new_policy.md").write_text("beta vendor bank approval workflow", encoding="utf-8")
    second = RagSkill([knowledge], tmp_path / "rag")
    result = second.retrieve("beta bank approval", top_k=1)

    assert result.status == "success"
    assert "beta vendor bank approval" in result.evidences[0].snippet
