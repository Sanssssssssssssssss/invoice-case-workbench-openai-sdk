# Case 01 - Clean Three-Way Match With Real JPG Invoice

Real French invoice JPG plus XML/TSV sidecars. Generated ERP PO, GRN, vendor, and duplicate check all match.

## How to test in the current app

Upload `originals/FACTU2015020048.jpg` together with files 02-05 from `upload_to_app/`. Do not also upload the text invoice extract because that would duplicate the invoice evidence.

Suggested prompt:

```text
请审核这些发票付款材料，判断证据是否完整。只做材料审核，不要生成报告或 PDF。
```
