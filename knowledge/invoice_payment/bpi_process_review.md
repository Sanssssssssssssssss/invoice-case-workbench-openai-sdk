# BPI and Process Log Evidence

BPI event logs and process-mining exports are process evidence. They may show a sequence such as Create Purchase Order, Record Goods Receipt, Post Invoice, Clear Invoice, or Change Payment Terms.

Process evidence can support:

- whether an invoice appears in a historical procure-to-pay flow;
- whether events happened in an unusual order;
- whether a case has a delay, rework loop, manual change, duplicate-looking activity, or missing event;
- whether a Clear Invoice event exists in a historical event log.

Process evidence cannot by itself prove that the local agent has approved, paid, routed, posted, or submitted anything. It also cannot replace source business materials such as invoice, PO, GRN, vendor record, or duplicate payment check.

When reviewing BPI or process logs, distinguish:

- event name and timestamp;
- case id or invoice id;
- activity owner or resource;
- lifecycle transition;
- amount, vendor, payment term, or company code if present;
- gaps between process evidence and actual source documents.
