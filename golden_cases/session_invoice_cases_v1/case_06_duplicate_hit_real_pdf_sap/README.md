# Case 06 - Duplicate Risk With Real SAP Sample PDF Invoice

Real multi-page SAP sample PDF. Duplicate check intentionally reports a historical payment record.

## How to test in the current app

Upload `originals/sap_5435569865439.pdf` together with files 02-05 from `upload_to_app/`. Do not also upload the text invoice extract because that would duplicate the invoice evidence.

Suggested prompt:

```text
请审核这组 SAP 发票付款材料，判断是否存在重复付款风险。不要生成报告。
```
