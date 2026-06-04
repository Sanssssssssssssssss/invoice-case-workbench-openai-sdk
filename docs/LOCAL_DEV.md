# Local VSCode Development

## One-time setup

```powershell
cd E:\GPTProject2\NewERPAgnent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

The app requires a real LLM key. It does not provide a no-key deterministic fallback.

The app can automatically load your legacy env file for model credentials at:

```text
E:\GPTProject2\ERPagent\rfp-security-rag\backend\.env
```

If you want a project-local env, copy `backend/.env.example` to `backend/.env` and fill the model values.
RAG defaults to this repository's own `knowledge/invoice_payment` directory. The old knowledge folder is opt-in only through `INVOICE_AGENT_INCLUDE_LEGACY_KNOWLEDGE=true` or `INVOICE_AGENT_KNOWLEDGE_ROOTS`.
Harness traces are local by default under `workspace/cases/{case_id}/traces`; LangSmith tracing is opt-in with `INVOICE_AGENT_ENABLE_LANGSMITH=true`.

## VSCode

Open the folder:

```powershell
code E:\GPTProject2\NewERPAgnent
```

Install recommended extensions when VSCode prompts you.

Useful commands:

- `Terminal > Run Task > Python: Install dependencies`
- `Terminal > Run Task > Test: Pytest`
- `Terminal > Run Task > Run: API`
- `Run and Debug > API: Uvicorn`
- `Run and Debug > Sample: File Case`

## Local Desktop App

Start the local window app:

```powershell
.\.venv\Scripts\python.exe -m app.desktop
```

Or run:

```powershell
.\start_desktop.bat
```

In VSCode, use:

- `Terminal > Run Task > Run: Desktop app`
- `Run and Debug > Desktop: Local App`

This is the recommended way to test the agent locally. It opens a native desktop window, supports multiple case chats, and lets you drag or choose attachment files for the next turn.

## API

The API is still available for raw debugging, but it is not required for the desktop app.

Start the API only if you need HTTP testing:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload
```

Open `docs/api.http` with the REST Client extension if you want to run raw requests from the editor.

For file review, pass an attachment path in the API request. The agent must call `read_attachment` before it can review the file content.
