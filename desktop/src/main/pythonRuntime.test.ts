import assert from 'node:assert/strict'
import test from 'node:test'

// @ts-expect-error Node's test runner loads this TypeScript module directly.
import { resolvePythonRuntime, type PythonRuntime } from './pythonRuntime.ts'

const winProjectPython = 'C:\\repo\\.venv\\Scripts\\python.exe'

test('prefers a usable project venv over configured and PATH Python', () => {
  const probed: string[] = []
  const runtime = resolvePythonRuntime('C:\\repo', {
    platform: 'win32',
    env: { INVOICE_AGENT_PYTHON: 'C:\\configured\\python.exe' },
    exists: (path: string) => path === winProjectPython,
    probe: (candidate: PythonRuntime) => {
      probed.push(candidate.command)
      return { ok: true, detail: 'ready' }
    }
  })

  assert.equal(runtime.command, winProjectPython)
  assert.equal(runtime.source, 'project .venv')
  assert.deepEqual(probed, [winProjectPython])
})

test('falls back when the project venv launcher is stale', () => {
  const runtime = resolvePythonRuntime('C:\\repo', {
    platform: 'win32',
    env: { INVOICE_AGENT_PYTHON: 'C:\\configured\\python.exe' },
    exists: (path: string) => path === winProjectPython,
    probe: (candidate: PythonRuntime) => ({
      ok: candidate.command === 'C:\\configured\\python.exe',
      detail: candidate.command === winProjectPython ? 'launcher base executable is missing' : 'ready'
    })
  })

  assert.equal(runtime.command, 'C:\\configured\\python.exe')
  assert.equal(runtime.source, 'INVOICE_AGENT_PYTHON')
})

test('uses the active environment before PATH when no project venv exists', () => {
  const activePython = '/work/runtime/bin/python'
  const runtime = resolvePythonRuntime('/repo', {
    platform: 'linux',
    env: { VIRTUAL_ENV: '/work/runtime' },
    exists: (path: string) => path === activePython,
    probe: () => ({ ok: true, detail: 'ready' })
  })

  assert.equal(runtime.command, activePython)
  assert.equal(runtime.source, 'active virtual environment')
})

test('reports every failed candidate and a concrete recovery action', () => {
  assert.throws(
    () =>
      resolvePythonRuntime('C:\\repo', {
        platform: 'win32',
        env: { INVOICE_AGENT_PYTHON: 'C:\\configured\\python.exe' },
        exists: (path: string) => path === winProjectPython,
        probe: (candidate: PythonRuntime) => ({ ok: false, detail: `cannot run ${candidate.command}` })
      }),
    (error: unknown) => {
      assert.ok(error instanceof Error)
      assert.match(error.message, /No usable Python runtime/)
      assert.match(error.message, /project \.venv/)
      assert.match(error.message, /INVOICE_AGENT_PYTHON/)
      assert.match(error.message, /PATH/)
      assert.match(error.message, /backend\/requirements\.txt/)
      return true
    }
  )
})
