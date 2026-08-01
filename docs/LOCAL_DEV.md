# Local VSCode Development

## One-time setup

```powershell
cd E:\GPTProject2\erp-openai
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
pnpm install
```

The app requires a real LLM key. It does not provide a no-key deterministic fallback.

The app loads credentials from `INVOICE_AGENT_ENV_FILE` when set, then from `backend/.env`. For a project-local environment, copy `backend/.env.example` to `backend/.env` and fill the model values.
RAG defaults to this repository's `knowledge/invoice_payment` directory; override it with `INVOICE_AGENT_KNOWLEDGE_ROOTS` when needed.
Harness traces are local by default under `workspace/cases/{case_id}/traces`; Langfuse tracing is opt-in with `INVOICE_AGENT_ENABLE_LANGFUSE=true`.

## VSCode

Open the folder:

```powershell
code E:\GPTProject2\erp-openai
```

Install recommended extensions when VSCode prompts you.

Useful commands:

- `Terminal > Run Task > Python: Install dependencies`
- `Terminal > Run Task > Test: Pytest`
- `Terminal > Run Task > Run: API`
- `Run and Debug > API: Uvicorn`
- `Run and Debug > Sample: File Case`

## Local Desktop App

Start the Electron desktop app:

```powershell
pnpm dev
```

Or run:

```powershell
.\start_desktop.bat
```

In VSCode, use:

- `Terminal > Run Task > Run: Desktop app`
- `Run and Debug > Desktop: Local App`

This is the recommended way to test the agent locally. It opens the Electron window, starts the local API automatically, supports multiple case chats, and lets you drag or choose attachment files for the next turn.

The older Tkinter desktop remains available for compatibility testing only:

```powershell
.\.venv\Scripts\python.exe -m app.desktop
```

## API

The API is still available for raw debugging, but it is not required for the desktop app.

Start the API only if you need HTTP testing:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload
```

Open `docs/api.http` with the REST Client extension if you want to run raw requests from the editor.

For file review, upload the file through `POST /api/cases/{case_id}/attachments`, then pass the returned case-workspace path in the turn request. Arbitrary local filesystem paths are rejected. The agent must call `read_attachment` before it can review the file content. PDF and image attachments produce local previews and use OCR when needed.
