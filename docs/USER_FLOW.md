# User Flow

This tool is a local LLM-native case workbench. It does not approve invoices, pay invoices, post to ERP, or submit anything to ERP.

## Normal Workflow

Open the local desktop app:

```powershell
.\.venv\Scripts\python.exe -m app.desktop
```

Use the left case list to open multiple case chats. Drag evidence files into the attachments area, or click `Choose files`, then send a message asking the agent to review them.

1. Create a case:

   ```text
   帮我新建一个发票付款审查 case。
   ```

2. Ask for required materials:

   ```text
   我现在需要准备什么？
   ```

3. Submit materials:

   ```text
   这是发票 INV-001，金额 10000 CNY，供应商 ABC。
   ```

   Or pass a file attachment through the app drag-and-drop area. The app uploads the file into the local case workspace first, then sends the returned attachment metadata to the agent turn.

   Raw API shape:

   ```json
   {
     "case_id": "case_001",
     "message": "请审查我提交的材料文件：01_invoice_INV-2026-001.md",
     "attachments": [
       {
         "name": "01_invoice_INV-2026-001.md",
         "path": "E:\\GPTProject2\\NewERPAgnent\\samples\\cases\\invoice_payment_case_001\\evidence\\01_invoice_INV-2026-001.md",
         "content_type": "text/markdown"
       }
     ]
   }
   ```

4. Continue submitting PO, GRN, vendor record, and duplicate payment check.

5. Generate the manager report:

   ```text
   生成报告。
   ```

6. Review local outputs:

   ```text
   workspace/cases/{case_id}/case_state.json
   workspace/cases/{case_id}/conversation.jsonl
   workspace/cases/{case_id}/traces/run_*.json
   workspace/cases/{case_id}/reports/manager_report.md
   workspace/cases/{case_id}/reports/manager_report.pdf
   ```

## Important Behavior

- Planner is the only scheduler.
- The runtime loop is a real LangGraph `StateGraph` with five nodes: `load_context`, `planner`, `execute_action`, `persist_checkpoint`, `respond_or_continue`.
- Roles produce structured JSON; they do not write files.
- Tools perform hard capabilities only.
- Errors from roles/tools are written into observations and shown to Planner.
- If Planner itself fails, the run stops instead of taking deterministic backend recovery actions.
- Attachments are readable only when explicitly declared on the current request.
