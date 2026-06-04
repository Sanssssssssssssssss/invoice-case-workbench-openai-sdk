# Materials Advisor Task Validation

Date: 2026-05-25  
Branch: `codex/flow-trace-prompt-validation`

## Scope

This pass upgrades `materials_advisor` from a generic advice role into a supplemental material task generator. The implementation stays small: no new agent, no new route, no schema expansion. The existing `MaterialsAdvisorResult` still returns `answer`, `missing_materials`, and `next_questions`.

## Reference Inputs

Public AP control references were folded into local RAG guidance:

- Microsoft Dynamics 365 Finance invoice matching overview: https://learn.microsoft.com/en-us/dynamics365/finance/accounts-payable/accounts-payable-invoice-matching
- Microsoft Dynamics 365 Finance three-way matching policies: https://learn.microsoft.com/en-us/dynamics365/finance/accounts-payable/three-way-matching-policies
- Microsoft Dynamics 365 Finance vendor invoices overview: https://learn.microsoft.com/en-us/dynamics365/finance/accounts-payable/vendor-invoices-overview
- Microsoft Dynamics 365 Finance vendor bank account workflow: https://learn.microsoft.com/en-us/dynamics365/finance/accounts-payable/vendor-bank-account-workflow
- Oracle Fusion Cloud supplier profile approval changes: https://docs.oracle.com/en/cloud/saas/procurement/25b/oapro/how-you-configure-internal-changes-on-supplier-profile-approvals.html
- SAP duplicate invoice check overview: https://help.sap.com/docs/SUPPORT_CONTENT/fiaccounting/3361878522.html

GitHub search for mature open AP policy/agent repos did not return a high-signal source. The useful pattern was still the lightweight local skill approach: keep the role simple, give it a compact skill guide, and let route contracts enforce where the role is used.

## Changes

- `materials_advisor/prompt.md` now requires a Chinese task table with: task, affected requirement, status, why insufficient, owner, format, required fields, and impact.
- `materials_advisor/task_skill.md` adds owner/format defaults for invoice, PO, GRN, vendor master, duplicate check, and bank-change evidence.
- Planner route contract now forces explicit material-task requests to `materials_advisor` and returns the advisor answer verbatim so Planner cannot flatten the table.
- RAG now stores a knowledge fingerprint in `index.json`; added or edited knowledge files rebuild the local index instead of serving stale snippets.
- Added `knowledge/invoice_payment/enterprise_ap_controls.md` with three-way matching, duplicate payment, vendor master/bank-change, and Clear Invoice boundary guidance.

## Real LLM Validation

Model used by the local environment: `kimi-k2.5`.

Direct role test: `manual_materials_advisor_status_boundary_rerun`

- Input case had weak invoice OCR, missing PO/vendor/duplicate check, and conflict `发票数量20，GRN数量18`.
- Output kept a task table with owners, formats, fields, and requirement impact.
- It did not invent `PO-123`.
- It preserved conflict direction: invoice 20, GRN 18.
- It did not emit `accepted`, `satisfied`, `是否已付款`, `已付款风险`, `是否已清账`, or `已清账风险`.

Full graph test: `manual_materials_advisor_graph_20260525_final2`

- User asked for Clear Invoice boundary plus enterprise supplemental material tasks.
- Planner was first tempted to answer boundary directly in earlier trials; route contract now enforces `materials_advisor`.
- Final response preserved the advisor task table instead of a generic material list.
- Trace showed 2 steps: forced `call_role materials_advisor`, then forced `final_answer` from advisor output.

## RAG Recall Checks

Deterministic tests disable vector search and use BM25:

- Duplicate-payment query recalled `enterprise_ap_controls.md` duplicate check chunk.
- Vendor-bank-change query recalled bank approval/workflow chunk.
- Three-way-match query recalled invoice/PO/product receipt chunk.
- Index rebuild test confirms adding a new knowledge file under the same root is reflected without deleting storage manually.

Default local RAG smoke:

- `duplicate payment same supplier same amount historical clearing voucher` -> `enterprise_ap_controls.md chunk 3`
- `vendor bank account change approval workflow history` -> `enterprise_ap_controls.md chunk 4`
- `three-way matching invoice purchase order product receipt fields` -> `enterprise_ap_controls.md chunk 2`

## Test Result

`python -m pytest backend/tests -q`

Result: `119 passed in 116.68s`.

## Remaining Risks

- `missing_materials` schema is still only a flat list, so weak/conflict categories live in the task table instead of separate JSON fields.
- Advisor quality still depends on LLM compliance for table content, but route selection and final answer relay are now deterministic for explicit material-task requests.
