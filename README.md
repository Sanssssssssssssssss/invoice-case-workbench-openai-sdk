# LLM-Native Invoice Case Workbench Agent

A local invoice payment review workbench rebuilt on the OpenAI Agents SDK. The desktop experience stays stable: multiple case chats, attachment drag/drop, local case files, traces, reports, and optional Langfuse observability.

The runtime split is intentionally strict:

- OpenAI Agents SDK runs the case manager loop and structured specialist model calls.
- The local harness owns policy gates, case state, workspace writes, checkpoints, trace persistence, and frontend/API compatibility.
- Specialist agents return structured outputs only.
- Side effects stay behind the tool catalog and case workspace boundary.
- LangGraph/LangChain checkpointing is not part of this repository.

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.\.venv\Scripts\python.exe -m pytest backend\tests -q
.\.venv\Scripts\python.exe -m app.desktop
```

Electron desktop:

```powershell
pnpm install
pnpm dev
```

API debugging:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

## User Flow

1. `帮我新建一个发票付款审查 case。`
2. `我现在需要准备什么？`
3. Submit evidence text, choose files, or drag files into the attachment area.
4. Continue with invoice, PO, GRN, vendor record, duplicate payment check.
5. `生成报告。`
6. Review outputs under `workspace/cases/{case_id}/`.

## Runtime Contract

The frontend contract is preserved:

- `POST /api/agent/turn` accepts the existing `AgentTurnRequest` shape.
- `POST /api/agent/turn` returns the existing `AgentTurnResponse` shape.
- `POST /api/cases/{case_id}/attachments` keeps the existing upload behavior.
- `POST /api/cases/{case_id}/runs/{run_id}/approval` resumes SDK tool approval interruptions.
- Case state, attachments, traces, and generated reports remain local under the case workspace.

## Agent and Tool Structure

```text
CaseManagerAgent
  function tools:
    read_case_state
    read_attachment
    list_case_files
    write_case_file
    render_pdf
  specialist tools:
    materials_advisor
    evidence_reviewer
    case_patch_writer
    report_writer
  internal harness action:
    write_case_patch
```

`write_case_patch` is internal-only. Report file writes and PDF rendering are local-write tools and require explicit approval when invoked from the manager run.

## Skills

Skills are registered as manifests with instruction files, resource roots, allowed roles, declared tools, side-effect metadata, and artifact policies. Skills cannot bypass the tool catalog or write outside the case workspace.

## Observability

Langfuse is retained and opt-in. Default capture is summary-only. OpenAI SDK tracing is disabled at the SDK layer while the local harness records redacted spans, model calls, role calls, tool calls, checkpoints, and final answers into local traces and Langfuse when enabled.

## Configuration

The app loads environment values from:

1. `INVOICE_AGENT_ENV_FILE`
2. `backend/.env`

Default model settings:

```powershell
LLM_PROVIDER=openai
LLM_MODEL=gpt-4.1-mini
LLM_BASE_URL=https://api.openai.com/v1
```

Default local paths:

```powershell
INVOICE_AGENT_WORKSPACE_ROOT=workspace/cases
INVOICE_AGENT_STORAGE_ROOT=backend/storage
INVOICE_AGENT_KNOWLEDGE_ROOTS=knowledge/invoice_payment
```

## Validation

Run these before merging runtime changes:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
pnpm typecheck
pnpm test
pnpm build
```

Clean-runtime check:

```powershell
grep -R "langgraph\|StateGraph\|SqliteSaver\|langchain" backend/app backend/tests
```

That grep should return no runtime code hits.
