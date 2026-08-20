import { createWriteStream, mkdirSync } from 'node:fs'
import { createServer } from 'node:net'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { app } from 'electron'
import { resolvePythonRuntime } from './pythonRuntime.js'

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
  const python = resolvePythonRuntime(root)
  log.write(`[desktop] python source=${python.source} command=${python.command}\n`)
  const child = spawn(
    python.command,
    [...python.prefixArgs, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
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
  child.on('error', (error) => {
    log.write(`\n[desktop] backend process error: ${error.message}\n`)
  })
  child.on('exit', (code, signal) => {
    log.write(`\n[desktop] backend exited code=${code ?? ''} signal=${signal ?? ''}\n`)
    log.end()
  })

  try {
    await waitForBackend(baseUrl, child, 60_000)
  } catch (error) {
    if (!child.killed) child.kill()
    throw new Error(
      `Python backend did not become ready using ${python.source} (${python.command}). ` +
        `See ${logPath}. ${String(error)}`
    )
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

async function waitForBackend(
  baseUrl: string,
  child: ChildProcessWithoutNullStreams,
  timeoutMs: number
): Promise<void> {
  let onError: ((error: Error) => void) | undefined
  let onExit: ((code: number | null, signal: NodeJS.Signals | null) => void) | undefined
  const processFailure = new Promise<never>((_resolve, reject) => {
    onError = (error) => reject(new Error(`Could not start backend process: ${error.message}`))
    onExit = (code, signal) => {
      reject(new Error(`Backend exited before health check (code=${code ?? ''}, signal=${signal ?? ''})`))
    }
    child.once('error', onError)
    child.once('exit', onExit)
  })

  try {
    await Promise.race([waitForHealth(baseUrl, timeoutMs), processFailure])
  } finally {
    if (onError) child.off('error', onError)
    if (onExit) child.off('exit', onExit)
  }
}
