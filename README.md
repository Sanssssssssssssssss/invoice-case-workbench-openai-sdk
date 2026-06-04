# LLM-Native Invoice Case Workbench Agent

A clean local case workbench for invoice payment review. The default interface is a native local desktop app with multiple case chats and attachment drag/drop. It keeps business reasoning in planner/role model calls while the harness controls steps, checkpoints, traces, and local case workspace writes.

## Quick Start

```powershell
cd E:\GPTProject2\NewERPAgnent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.\.venv\Scripts\python.exe -m pytest backend\tests
.\.venv\Scripts\python.exe -m app.desktop
```

Or double-click/run:

```powershell
.\start_desktop.bat
```

The API is still available for raw HTTP debugging:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

## VSCode

Open this folder in VSCode:

```powershell
code E:\GPTProject2\NewERPAgnent
```

Recommended extensions and debug/task configs are already in `.vscode/`.

Common VSCode actions:

- `Terminal > Run Task > Python: Install dependencies`
- `Terminal > Run Task > Test: Pytest`
- `Terminal > Run Task > Run: Desktop app`
- `Run and Debug > Desktop: Local App`
- `Run and Debug > API: Uvicorn`
- `Run and Debug > Sample: File Case`

More details: `docs/LOCAL_DEV.md`.

## User Flow

As a user, interact with the desktop workbench:

1. `帮我新建一个发票付款审查 case。`
2. `我现在需要准备什么？`
3. Submit evidence text, choose files, or drag files into the attachment area.
4. Continue with invoice, PO, GRN, vendor record, duplicate payment check.
5. `生成报告。`
6. Review local outputs under `workspace/cases/{case_id}/`.

More details: `docs/USER_FLOW.md`.

## Sample Case

A complete sample is included at `samples/cases/invoice_payment_case_001/`.

Run it:

```powershell
.\.venv\Scripts\python.exe backend\scripts\run_sample_case.py
```

This creates `workspace/cases/case_sample_001/` with state, conversation, traces, and reports.

## Configuration

The app loads environment values from:

1. `INVOICE_AGENT_ENV_FILE`
2. `backend/.env`
3. `E:\GPTProject2\ERPagent\rfp-security-rag\backend\.env`

The default RAG knowledge root is this repository's `knowledge/invoice_payment/`. The old local ERP knowledge folder is opt-in through `INVOICE_AGENT_INCLUDE_LEGACY_KNOWLEDGE=true` or `INVOICE_AGENT_KNOWLEDGE_ROOTS`.

A valid OpenAI-compatible chat model key is required for agent turns. The agent runtime does not provide a deterministic no-LLM execution mode.
