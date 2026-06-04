@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Run:
  echo python -m venv .venv
  echo .\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
  exit /b 1
)
where pnpm >nul 2>nul
if errorlevel 1 (
  echo pnpm was not found. Install Node.js dependencies first:
  echo npm install -g pnpm
  echo pnpm install
  exit /b 1
)
pnpm dev
