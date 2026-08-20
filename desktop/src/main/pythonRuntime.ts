import { existsSync } from 'node:fs'
import { posix, win32 } from 'node:path'
import { spawnSync } from 'node:child_process'

export interface PythonRuntime {
  command: string
  prefixArgs: string[]
  source: string
}

interface PythonProbeResult {
  ok: boolean
  detail: string
}

interface ResolvePythonOptions {
  platform?: NodeJS.Platform
  env?: NodeJS.ProcessEnv
  exists?: (path: string) => boolean
  probe?: (runtime: PythonRuntime) => PythonProbeResult
}

interface PythonAttempt extends PythonRuntime {
  detail: string
}

export function resolvePythonRuntime(root: string, options: ResolvePythonOptions = {}): PythonRuntime {
  const platform = options.platform ?? process.platform
  const env = options.env ?? process.env
  const exists = options.exists ?? existsSync
  const probe = options.probe ?? probePythonRuntime
  const path = platform === 'win32' ? win32 : posix
  const attempts: PythonAttempt[] = []

  for (const candidate of pythonCandidates(root, platform, env, exists, path.join)) {
    const result = probe(candidate)
    attempts.push({ ...candidate, detail: result.detail })
    if (result.ok) return candidate
  }

  const attempted = attempts
    .map(({ command, prefixArgs, source, detail }) => {
      const invocation = [command, ...prefixArgs].join(' ')
      return `- ${source}: ${invocation} (${detail})`
    })
    .join('\n')

  throw new Error(
    [
      'No usable Python runtime was found for the local backend.',
      'The runtime must start successfully and provide the "uvicorn" package.',
      'Tried:',
      attempted || '- no candidates',
      'Create .venv and install backend/requirements.txt, or set INVOICE_AGENT_PYTHON to a usable Python executable.'
    ].join('\n')
  )
}

function pythonCandidates(
  root: string,
  platform: NodeJS.Platform,
  env: NodeJS.ProcessEnv,
  exists: (path: string) => boolean,
  join: (...paths: string[]) => string
): PythonRuntime[] {
  const candidates: PythonRuntime[] = []
  const addPathIfPresent = (command: string, source: string): void => {
    if (exists(command)) candidates.push({ command, prefixArgs: [], source })
  }

  if (platform === 'win32') {
    addPathIfPresent(join(root, '.venv', 'Scripts', 'python.exe'), 'project .venv')
  } else {
    addPathIfPresent(join(root, '.venv', 'bin', 'python'), 'project .venv')
  }

  const configured = env.INVOICE_AGENT_PYTHON?.trim()
  if (configured) {
    candidates.push({ command: configured, prefixArgs: [], source: 'INVOICE_AGENT_PYTHON' })
  }

  const activeVenv = env.VIRTUAL_ENV?.trim()
  if (activeVenv) {
    const command =
      platform === 'win32'
        ? join(activeVenv, 'Scripts', 'python.exe')
        : join(activeVenv, 'bin', 'python')
    addPathIfPresent(command, 'active virtual environment')
  }

  if (platform === 'win32') {
    candidates.push({ command: 'python.exe', prefixArgs: [], source: 'PATH' })
  } else {
    candidates.push({ command: 'python3', prefixArgs: [], source: 'PATH' })
    candidates.push({ command: 'python', prefixArgs: [], source: 'PATH' })
  }

  const seen = new Set<string>()
  return candidates.filter((candidate) => {
    const key = `${platform === 'win32' ? candidate.command.toLowerCase() : candidate.command}\0${candidate.prefixArgs.join('\0')}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function probePythonRuntime(runtime: PythonRuntime): PythonProbeResult {
  const result = spawnSync(
    runtime.command,
    [
      ...runtime.prefixArgs,
      '-c',
      'import sys, uvicorn; print(sys.executable)'
    ],
    {
      encoding: 'utf8',
      timeout: 10_000,
      windowsHide: true
    }
  )

  if (result.error) return { ok: false, detail: result.error.message }
  if (result.status !== 0) {
    const stderr = result.stderr.trim().replace(/\s+/g, ' ')
    return { ok: false, detail: stderr || `exited with code ${result.status ?? 'unknown'}` }
  }
  return { ok: true, detail: result.stdout.trim() || 'ready' }
}
