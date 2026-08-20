@echo off
setlocal
cd /d "%~dp0"
where pnpm >nul 2>nul
if errorlevel 1 (
  echo pnpm was not found. Install Node.js dependencies first:
  echo npm install -g pnpm
  echo pnpm install
  exit /b 1
)
pnpm dev
