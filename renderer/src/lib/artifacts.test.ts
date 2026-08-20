import { describe, expect, it, vi } from 'vitest'
import type { ArtifactItem } from '@/types'
import { latestReportArtifacts, runArtifactAction } from './artifacts'

const artifact: ArtifactItem = {
  name: 'final_report.md',
  type: 'report',
  run_id: '',
  path: 'reports/final_report.md',
  updated_at: '2026-06-01T10:00:00+00:00',
  bytes: 42,
  content_type: 'text/markdown',
  open_url: '/api/cases/case_1/files/content?path=reports%2Ffinal_report.md&download=0',
  download_url: '/api/cases/case_1/files/content?path=reports%2Ffinal_report.md&download=1',
  generated: true
}

describe('artifact actions', () => {
  it('keeps PDF as the primary report entry ahead of Markdown', () => {
    const markdown = { ...artifact, updated_at: '2026-06-02T10:00:00+00:00' }
    const pdf = {
      ...artifact,
      name: 'final_report.pdf',
      path: 'reports/final_report.pdf',
      content_type: 'application/pdf',
      updated_at: '2026-06-01T10:00:00+00:00'
    }

    expect(latestReportArtifacts([markdown, pdf]).map((item) => item.name)).toEqual(['final_report.pdf', 'final_report.md'])
  })

  it('opens generated files through Electron IPC when available', async () => {
    const openCaseFile = vi.fn(async () => undefined)

    const result = await runArtifactAction('case_1', artifact, 'open', {
      cockpit: {
        getBackendInfo: vi.fn(async () => ({ baseUrl: 'http://127.0.0.1:8010', port: 8010, logPath: '' })),
        windowControl: vi.fn(async () => undefined),
        openCaseFile,
        showCaseFileInFolder: vi.fn(async () => undefined)
      },
      resolveUrl: vi.fn()
    })

    expect(result).toBe('electron-open')
    expect(openCaseFile).toHaveBeenCalledWith('case_1', 'reports/final_report.md')
  })

  it('reveals generated files through Electron IPC when available', async () => {
    const showCaseFileInFolder = vi.fn(async () => undefined)

    const result = await runArtifactAction('case_1', artifact, 'show', {
      cockpit: {
        getBackendInfo: vi.fn(async () => ({ baseUrl: 'http://127.0.0.1:8010', port: 8010, logPath: '' })),
        windowControl: vi.fn(async () => undefined),
        openCaseFile: vi.fn(async () => undefined),
        showCaseFileInFolder
      },
      resolveUrl: vi.fn()
    })

    expect(result).toBe('electron-show')
    expect(showCaseFileInFolder).toHaveBeenCalledWith('case_1', 'reports/final_report.md')
  })

  it('falls back to content URLs outside Electron', async () => {
    const openExternal = vi.fn()

    const result = await runArtifactAction('case_1', artifact, 'open', {
      cockpit: undefined,
      openExternal,
      resolveUrl: async (path) => `http://127.0.0.1:8010${path}`
    })

    expect(result).toBe('browser-open')
    expect(openExternal).toHaveBeenCalledWith(expect.stringContaining('download=0'))
  })

  it('uses download URL for browser downloads', async () => {
    const downloadExternal = vi.fn()

    const result = await runArtifactAction('case_1', artifact, 'download', {
      cockpit: undefined,
      downloadExternal,
      resolveUrl: async (path) => `http://127.0.0.1:8010${path}`
    })

    expect(result).toBe('browser-download')
    expect(downloadExternal).toHaveBeenCalledWith(expect.stringContaining('download=1'))
  })
})
