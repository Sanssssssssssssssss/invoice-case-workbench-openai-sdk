from __future__ import annotations


AP_THREE_WAY_REQUIREMENTS = (
    "invoice",
    "purchase_order",
    "goods_receipt",
    "vendor_record",
    "duplicate_payment_check",
)

AP_LITE_REQUIREMENTS = (
    "invoice",
    "purchase_order",
    "goods_receipt_or_service_acceptance",
    "vendor_identity",
    "duplicate_payment_screen",
)

# Backward-compatible alias for existing AP review tests and evals.
CORE_REQUIREMENTS = AP_THREE_WAY_REQUIREMENTS

INVOICE_REQUIRED_FIELD_REQUIREMENTS = (
    "invoice_number",
    "supplier",
    "buyer",
    "invoice_date",
    "amount_total",
    "currency_tax",
    "source_traceability",
)

INVOICE_OPTIONAL_FIELD_REQUIREMENTS = (
    "line_items_product_title",
    "signature_or_authorized_signatory",
    "template_match",
)

INVOICE_FIELD_REQUIREMENTS = INVOICE_REQUIRED_FIELD_REQUIREMENTS + INVOICE_OPTIONAL_FIELD_REQUIREMENTS

DEFAULT_REQUIREMENT_LABELS = {
    "invoice": "发票原件",
    "purchase_order": "采购订单",
    "goods_receipt": "收货/验收记录",
    "vendor_record": "供应商主数据",
    "duplicate_payment_check": "重复付款检查",
    "goods_receipt_or_service_acceptance": "收货/服务验收记录",
    "vendor_identity": "供应商身份/主数据",
    "duplicate_payment_screen": "重复付款筛查",
    "invoice_number": "发票号码",
    "supplier": "供应商/开票方",
    "buyer": "购买方/收票方",
    "invoice_date": "发票日期",
    "amount_total": "金额/总计",
    "currency_tax": "币种/税额",
    "line_items_product_title": "商品行/服务标题",
    "signature_or_authorized_signatory": "签名/授权签署",
    "source_traceability": "原件可追溯性",
    "template_match": "模板/版式匹配",
}


def requirement_label(requirement_id: str) -> str:
    value = str(requirement_id or "").strip()
    return DEFAULT_REQUIREMENT_LABELS.get(value, value.replace("_", " ").strip() or "requirement")


def default_requirement_required(requirement_id: str) -> bool:
    return str(requirement_id or "").strip() not in INVOICE_OPTIONAL_FIELD_REQUIREMENTS
