# Public ERP evidence source catalog

Research date: 2026-08-20.

This catalog separates **discovered material** from **qualified vendored cases**. A public URL is not enough: files are vendored only when the license, immutable revision, content origin, and oracle path are clear.

## Qualified now

| Source | Revision | License | Assets | Use |
|---|---|---:|---:|---|
| [InvoiceBenchmark](https://huggingface.co/datasets/jngb-labs/InvoiceBenchmark/tree/f0699d8c94468fb2fefaa22f669684b22e68c3d3) | `f0699d8...` | MIT | 200 PDF + JSON; 50 vendored | Controlled invoice arithmetic, number formatting, VAT, discounts, credit notes |

## Eligible next sources

| Source | Revision | License | Candidate assets | Intended pack | Qualification note |
|---|---|---:|---:|---|---|
| [HV09 bilingual invoices](https://huggingface.co/datasets/HV09/synthetic-bilingual-invoices-200/tree/1c08b595c2aa9538dc6c773b657fee631f08dc4b) | `1c08b59...` | CC-BY-4.0 | 200 PNG + row truth | OCR/RTL/Hijri | Convert to PDF, retain attribution and transformation record |
| [Mustang Project samples](https://github.com/ZUGFeRD/mustangproject/tree/7e263a7b403f26cc27b2fa64ed8141633097f2ff) | `7e263a7...` | Apache-2.0 | 6 reviewed PDF with embedded XML | Factur-X, partial invoice, foreign currency, credit note | Retain LICENSE and NOTICE; perform identity/trademark review |
| [ZUGFeRD corpus](https://github.com/ZUGFeRD/corpus/tree/d891458e9822e34271a5438497bf924e89955979) | `d891458...` | Apache-2.0 with notices | 26 PDF + CII XML | EN16931/XRechnung semantics and corrections | Preserve FeRD notices; treat as public test material, not enterprise records |
| [invoice-pdfkit](https://github.com/Matteljay/invoice-pdfkit/tree/a936b14eba38433a5d7701ad8320703a9dab812a) | `a936b14...` | MIT | quote/invoice/packing/return/receipt/refund | Multi-document lifecycle | Regenerate from a scrubbed shared seed; original timeline also contains a useful chronology contradiction |
| [IBM PO samples](https://github.com/IBM/bob-oic-tutorials/tree/92ce87f7e3d89eaa1f08373ac710927fb385dbdf/docling-saas-purchase-order/sample) | `92ce87f...` | Apache-2.0 | 5 PDF | PO and parser resilience | Renderable, but malformed `startxref`; use as negative parser cases, not clean golden files |
| [Typst business templates](https://github.com/casoon/typst-business-templates/tree/7839908ae7db18f82d801167c2ec6d30494d9521) | `7839908...` | MIT | 3 invoice/offer pairs + JSON | Non-PO contract/offer matching | Regenerate both sides from one scrubbed JSON seed |
| [invoice-payment-matcher fixtures](https://github.com/realtonkaa/invoice-payment-matcher/tree/1b1e4a4e14acb22503ca7d8dae821115aee3cb12) | `1b1e4a4...` | MIT | 3 invoice text + 2 bank CSV | Payment matching and duplicate lifecycle | Render our own PDFs from the open structured seed |

## Index only

| Source | Reason not vendored |
|---|---|
| Robocorp PDF invoice demo | Apache repository, but PDFs contain third-party branding/watermarks; use as visual reference only |
| invoice2data sample invoices | Repository is MIT, but the license chain for third-party invoice assets is not explicit |
| INV-CDIP / RealKIE | CC-BY-NC and underlying fair-use/copyright conditions are unsuitable for a broadly redistributable repository pack |
| Parsee invoice samples | Publicly accessible, but no license was found that clearly covers redistribution of the original PDFs |

## Rejected

- Repositories without an explicit license covering document assets.
- Real invoices whose privacy, trademark, or copyright chain cannot be verified.
- Signed or expiring download URLs without an immutable materialization path.
- Multiple copies or renderings of the same source SHA presented as independent documents.

## Planned architecture packs

The next packs should reuse the same `manifest / scenario / expected / oracle` boundary:

1. `public_invoice_vision_v1`: RTL, image-only, bilingual, and degraded invoices.
2. `public_einvoice_semantics_v1`: Factur-X/XRechnung with embedded XML oracle.
3. `synthetic_three_way_v1`: invoice, PO, and receipt generated from one structured seed.
4. `synthetic_payment_lifecycle_v1`: payment, reversal, refund, duplicate, and chronology cases.

No new Agent or hand-written business DAG is required for these packs.
