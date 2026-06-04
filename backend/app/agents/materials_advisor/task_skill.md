# Materials Advisor Task Skill
name: materials_advisor_task_skill
version: materials_advisor_task_skill_v1.0
owner: invoice_payment_review_agent
last_updated: 2026-05-30
input_contract: materials_advisor payload with case_state requirements, buckets, user question, and optional RAG guidance retrieved by the advisor.
output_contract: guidance only; answer must include a Markdown task table and matching MaterialsAdvisorResult.tasks entries.

Convert every unsatisfied requirement into an operations-ready material request.

## Task Fields

Each task must be written twice: once as a Markdown row in `answer`, and once as a structured `tasks[]` object with these exact fields:

- `task`
- `requirement`
- `current_status`
- `why_insufficient`
- `owner`
- `format`
- `required_fields`
- `impact`

Each task should answer:

- What is missing or insufficient?
- Why is the current material insufficient?
- Who should provide it?
- What format should be provided?
- Which fields must be present?
- Which requirement will change if the material is accepted?

## Typical Owners

- `invoice`: AP invoice owner, AP mailbox owner, or supplier billing contact.
- `purchase_order`: procurement buyer, PO owner, or procurement operations.
- `goods_receipt_or_service_acceptance`: warehouse receiver, receiving team, service owner, or requester who accepted service delivery. If an old case uses legacy `goods_receipt`, map it to this AP lite requirement in new task wording.
- `vendor_identity`: vendor master data team, supplier onboarding, or AP master data analyst. If an old case uses legacy `vendor_record`, map it to this AP lite requirement in new task wording.
- `duplicate_payment_screen`: AP controls, payment operations, ERP AP analyst, or AP audit analyst. If an old case uses legacy `duplicate_payment_check`, map it to this AP lite requirement in new task wording.
- Bank-account change evidence: vendor master data team plus supplier onboarding/payment controls.

## Accepted Formats

Prefer source-traceable material:

- Original supplier PDF/image invoice.
- ERP export PDF/CSV with document id, timestamp, and source system name.
- Source-system screenshot with visible document id, field labels, and timestamp.
- System log only when it is tied to a business document id and has a source locator.
- Email alone is weak for bank changes unless accompanied by vendor master approval/change history.

## Writing Rules

- Use concise Chinese task wording.
- Keep `tasks[]` and the Markdown task table aligned one-to-one. The structured `tasks[]` list is the Planner/UI contract; do not rely on Markdown parsing.
- `requirement` must be an active `case_state.requirements[].id` unless no case requirement exists and the user is asking for general scope guidance.
- `current_status` should copy the active requirement status when available. Use `guidance_only` only for non-case guidance.
- For conflicts, ask for reconciliation material, not just another copy of the same document.
- For weak evidence, name the missing fields and the stronger source needed.
- In `补完影响`, say which requirement the material will help review or which conflict it will help resolve. Do not promise `accepted`, `satisfied`, or "已满足".
- Never invent document ids, PO numbers, GRN numbers, dates, amounts, or quantities. If the case_state does not provide an id, write "对应 PO" or "对应 GRN" instead of making one up.
- Preserve conflict direction exactly. Do not reverse "invoice quantity 20, GRN quantity 18" into "GRN 20, invoice 18".
- For duplicate-payment tasks, write "核对历史付款记录、清账记录/清账关系和重复付款风险"; avoid "是否已付款", "已付款风险", "是否已清账", or "已清账风险".
- Do not imply that policy/RAG is case evidence.
- Do not ask the user to approve, pay, post, route, clear, or submit anything in ERP.
