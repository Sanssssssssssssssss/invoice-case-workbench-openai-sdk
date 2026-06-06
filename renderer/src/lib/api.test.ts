import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'

describe('api client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('includes response text in thrown errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('nope', { status: 500, statusText: 'Server Error' }))
    )

    await expect(api.listCases()).rejects.toThrow('500 Server Error: nope')
  })

  it('requests live status for the selected case', async () => {
    const fetchMock = vi.fn(async () => Response.json({ latestEventId: 'evt_1', isRunning: true }))
    vi.stubGlobal('fetch', fetchMock)

    await api.getLiveStatus('case_live')

    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/cases/case_live/live-status'), expect.any(Object))
  })

  it('starts a streaming run with the existing turn request shape', async () => {
    const fetchMock = vi.fn(async () => Response.json({ case_id: 'case_stream', run_id: 'run_1', status: 'accepted', stream_url: '/api/agent/runs/run_1/stream' }))
    vi.stubGlobal('fetch', fetchMock)

    await api.startRun('case_stream', 'hello', [{ case_id: 'case_stream', name: 'invoice.md', path: 'C:/tmp/invoice.md', relative_path: 'attachments/invoice.md', content_type: 'text/markdown', bytes: 10 }])

    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/agent/runs'), expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        case_id: 'case_stream',
        message: 'hello',
        attachments: [{ name: 'invoice.md', path: 'C:/tmp/invoice.md', content_type: 'text/markdown' }]
      })
    }))
  })

  it('posts streaming approval decisions', async () => {
    const fetchMock = vi.fn(async () => Response.json({ case_id: 'case_stream', run_id: 'run_1', status: 'accepted', stream_url: '/api/agent/runs/run_1/stream' }))
    vi.stubGlobal('fetch', fetchMock)

    await api.resumeRunApproval('case_stream', 'run_1', true, 'ok')

    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/agent/runs/run_1/approval'), expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ case_id: 'case_stream', approved: true, reason: 'ok' })
    }))
  })
})
