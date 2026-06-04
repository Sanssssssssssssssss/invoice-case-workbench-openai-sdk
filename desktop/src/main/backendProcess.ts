import { createWriteStream, existsSync, mkdirSync } from 'node:fs'
import { createServer } from 'node:net'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { app } from 'electron'

export interface BackendHandle {
  baseUrl: string
  port: number
  logPath: string
  process: ChildProcessWithoutNullStreams
  stop: () => void
}

const __dirname = dirname(fileURLToPath(import.meta.url))

export function repoRoot(): string {
  if (!app.isPackaged) {
    return resolve(process.cwd(), '..')
  }
  return resolve(process.resourcesPath, 'app')
}

export async function startBackend(): Promise<BackendHandle> {
  const root = repoRoot()
  const port = await findFreePort()
  const baseUrl = `http://127.0.0.1:${port}`
  const logDir = app.isPackaged ? join(app.getPath('userData'), 'logs') : join(root, 'tmp', 'desktop-logs')
  mkdirSync(logDir, { recursive: true })
  const logPath = join(logDir, `backend-${new Date().toISOString().replace(/[:.]/g, '-')}.log`)
  const log = createWriteStream(logPath, { flags: 'a' })
  const python = resolvePython(root)
  const child = spawn(
    python,
    ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
    {
      cwd: root,
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        LLM_THINKING_TYPE: 'enabled',
        INVOICE_AGENT_DESKTOP_PORT: String(port)
      }
    }
  )

  child.stdout.pipe(log)
  child.stderr.pipe(log)
  child.on('exit', (code, signal) => {
    log.write(`\n[desktop] backend exited code=${code ?? ''} signal=${signal ?? ''}\n`)
    log.end()
  })

  try {
    await waitForHealth(baseUrl, 60_000)
  } catch (error) {
    child.kill()
    throw new Error(`Python backend did not become ready. See ${logPath}. ${String(error)}`)
  }

  return {
    baseUrl,
    port,
    logPath,
    process: child,
    stop: () => {
      if (!child.killed) {
        child.kill()
      }
    }
  }
}

async function findFreePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = createServer()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      server.close(() => {
        if (typeof address === 'object' && address?.port) {
          resolvePort(address.port)
        } else {
          reject(new Error('Could not allocate a backend port'))
        }
      })
    })
  })
}

function resolvePython(root: string): string {
  const winVenv = join(root, '.venv', 'Scripts', 'python.exe')
  const posixVenv = join(root, '.venv', 'bin', 'python')
  if (existsSync(winVenv)) return winVenv
  if (existsSync(posixVenv)) return posixVenv
  return process.platform === 'win32' ? 'python.exe' : 'python3'
}

async function waitForHealth(baseUrl: string, timeoutMs: number): Promise<void> {
  const started = Date.now()
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(`${baseUrl}/health`)
      if (response.ok) return
    } catch {
      // Backend is still booting.
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 350))
  }
  throw new Error(`Timed out waiting for ${baseUrl}/health from ${__dirname}`)
}
