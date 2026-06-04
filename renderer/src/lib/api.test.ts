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
})
