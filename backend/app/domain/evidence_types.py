from __future__ import annotations


CORE_EVIDENCE_TYPES = (
    "invoice",
    "purchase_order",
    "goods_receipt",
    "vendor_record",
    "duplicate_payment_check",
)

PROCESS_ONLY_EVIDENCE_TYPES = (
    "process_log",
    "clear_invoice_event",
    "bpi_event_log",
)
