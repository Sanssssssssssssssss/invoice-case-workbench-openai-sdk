# Case 02 - Amount Conflict With Real JPG Invoice

Real invoice JPG. PO/GRN intentionally use a lower amount so the report should show an amount conflict.

## How to test in the current app

Upload the five files inside `upload_to_app/` for one session. The text invoice extract keeps this case focused on the deterministic amount mismatch; `originals/` remains available for visual inspection.

Suggested prompt:

```text
请核对这批付款材料的金额和单据关系，告诉我是否可以进入报告阶段。不要生成报告。
```
