# BPI P2P External Case Suite

This directory contains 50 external invoice-payment-review evaluation cases.

The cases are BPI Challenge 2019 compact-sample-aligned derived fixtures. Their
source case ids are aligned with the public GitHub compact sample file at:

https://github.com/Sanssssssssssssssss/erp-approval-agent/blob/main/backend/benchmarks/cases/erp_approval/bpi2019_sample_cases.json

They intentionally do not include a raw BPI CSV row and should not be described
as ERP-connected, production benchmark, approval-workflow evidence, or proof
that payment was approved or executed.

The suite is designed to test:

- Planner routing across case creation, RAG/materials advice, evidence review,
  case patch persistence, and manager memo generation.
- Evidence review classification and credibility.
- Case state/memory updates across turns.
- P2P risk explanation for 3-way, 2-way, consignment, Clear Invoice traps,
  amount variation, reversal/cancellation/payment block, and multi-turn
  credibility scenarios.
- Report quality and no over-claiming.
