# Dynamic Invoice Requirements Validation

Date: 2026-05-25

## What Changed

- New cases now start with an empty requirement list.
- Single-invoice review uses dynamic invoice field requirements instead of the AP five-material default.
- AP three-way/payment review remains available as an explicit profile.
- `CaseStore` is the truth source for requirement status and now validates evidence supports against active requirements.
- Empty invoice cases backfill the default invoice field profile so missing fields remain visible.
- RAG now includes invoice reference profiles for Flipkart, SAP sample invoices, and a scanned invoice dataset sample.

## Real LLM Sessions

### Flipkart PDF

- case_id: `dynamic_flipkart_invoice_session_v2`
- run_id: `run_218a09e119cc`
- input: Chinese request to review only the PDF invoice fields and not default to PO/GRN/vendor/duplicate checks.
- observed chain: `read_attachment -> rag_search -> evidence_reviewer -> case_patch_writer -> write_case_patch -> final_answer`
- result: 10 invoice field requirements satisfied; no AP requirements created.
- notable check: case summary stayed invoice-only: `收到Flipkart零售发票，字段完整，来源可追溯`.

### SAP PDF

- case_id: `dynamic_sap_invoice_session_v8`
- run_id: `run_80d00742273d`
- input: Chinese request to review only invoice fields/template similarity and omit generic ERP boundary wording.
- observed chain: `read_attachment -> rag_search -> evidence_reviewer -> case_patch_writer -> write_case_patch -> final_answer -> guard retry -> final_answer`
- result: 9 invoice field requirements satisfied; `signature_or_authorized_signatory` remains missing.
- notable check: the generic ERP boundary template was caught by `final_answer_generic_boundary_template` guard and Planner rewrote the final reply from case_state.

## Bugs Found And Fixed

- Planner sometimes sent extra `context` to `rag_search`; the tool now ignores unknown keys.
- `case_patch_writer` sometimes output requirements as strings or `requirement_id`; schema now normalizes common shapes.
- Empty invoice cases could hide missing default invoice fields; CaseStore now backfills invoice-field requirements.
- AP five-material requirements could be injected into an empty invoice-only case while supports used invoice field ids; CaseStore now treats that as wrong AP default pollution and keeps the invoice profile.
- `derived_conflicts` was too broad and treated notes like tax not separately listed as conflicts; conflict derivation now requires strong conflict language.
- Routine final replies could include generic ERP boundary templates; runtime now records guard feedback and lets Planner rewrite instead of substituting a deterministic answer.

## Automated Verification

- Full backend test suite: `133 passed`.
- Added focused tests for dynamic requirement creation, common requirement shape normalization, wrong AP default pollution, RAG profile retrieval, unknown RAG keys, and generic boundary template guard.
