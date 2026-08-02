# Clear Invoice Boundary

profile_id: `workflow_boundary_process_evidence`

In ERP and process-mining contexts, a Clear Invoice event is historical evidence that an invoice was cleared in a source system or event log. It is not a local permission for this workbench agent to claim payment or approval.

Allowed local wording:

- "The submitted process log contains a Clear Invoice event."
- "This may indicate a historical clearing event in the source process data."
- "This supports a process review note, not a new ERP payment action."

Disallowed local wording:

- "The agent paid the invoice."
- "The invoice is approved."
- "The invoice has been submitted to ERP by this agent."
- "Payment has been posted by this agent."

If Clear Invoice appears without invoice, PO, GRN, vendor, and duplicate-check evidence, the review should still record missing materials and explain the boundary.
