# Planner Attachment Manifest Skill
name: planner_attachment_manifest_skill
version: planner_attachment_manifest_skill_v1.1
owner: invoice_payment_review_agent
last_updated: 2026-05-26
input_contract: planner context_pack.attachment_manifest with attachment summaries, statuses, refs, evidence ids, and no full raw content.
output_contract: routing guidance only; choose whether to expand an old attachment with read_attachment or continue from current observations.

## Purpose

Use this skill whenever `context_pack.attachment_manifest` is present.
The manifest is a progressive-disclosure index for files already seen in this case.

## Rules

- Treat `attachment_manifest.attachments[]` as file memory, not as submitted evidence by itself.
- The manifest is safe for routing: it has file summaries, status, refs, and evidence ids, but not full raw content.
- Do not copy manifest summaries into `input`; use them only to choose the next action.
- If the current turn has new `context_pack.attachments`, call `read_attachment` for the current turn first.
- If the current turn explicitly asks to generate/export/render a report, final report, PDF, report formatting, screenshot index, field table, or evidence matrix, do not treat the word "PDF" as an older-attachment re-check. Route to report_generation unless there are new attachments to read first.
- If the user asks to re-check an older file, choose `call_tool:read_attachment` with `attachment_id` from the manifest.
- If the user asks about a file marked `quarantined`, `excluded`, or `error`, do not route it as supporting evidence and do not re-read its raw content. Answer from the manifest status, case_state, and existing observations.
- If runtime feedback says `manifest_attachment_quarantined`, `manifest_attachment_excluded`, or `manifest_attachment_error`, do not call `read_attachment` again. Output `final_answer` from the manifest status, case_state, and existing observations.
- If a file is `weak`, it may need evidence review, but do not imply it satisfies requirements.
- Never treat a RAG/profile/policy file or manifest summary as case evidence.
- If the user asks to re-check or verify fields in an older file, the route is `read_attachment(attachment_id) -> evidence_reviewer -> final_answer`.
- Do not call `report_writer`, `write_case_file`, or `render_pdf` during an older-file re-check unless the user explicitly asks to generate a report or PDF in that same turn.
- Only call `case_patch_writer` after an older-file re-check when the reviewer produced a material new finding that must update `case_state`; otherwise answer from the reviewer observation.
- If `context_pack.runtime_feedback.error_type` is `recheck_route_retry`, output `action="final_answer"` immediately. The runtime is telling you that the old file has already been expanded and reviewed. Do not call any role or tool again.

## Re-check Example

When the user says "re-check the old PDF attachment invoice number and grand total":

1. If there is no current-run `read_attachment` observation, call `read_attachment` with the manifest `attachment_id`.
2. If `read_attachment` succeeded and there is no current-run `evidence_reviewer` observation, call `evidence_reviewer`.
3. If `evidence_reviewer` already ran, output `final_answer`.
4. Forbidden in this route unless the user explicitly asks to generate a report: `report_writer`, `write_case_file`, `render_pdf`, repeated `read_attachment`.
