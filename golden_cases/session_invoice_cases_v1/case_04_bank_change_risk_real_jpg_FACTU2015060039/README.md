# Case 04 - Supplier Bank Change Risk With Real JPG Invoice

Real invoice JPG. Vendor master bank tail differs from supplier email change request, with no approval record.

## How to test in the current app

Upload all six files inside `upload_to_app/`. The text invoice extract keeps this case focused on the conflict between the vendor master and the unapproved bank-change email.

Suggested prompt:

```text
请审核这些付款材料，重点核对供应商银行账户变更是否有充分依据。不要生成报告。
```
