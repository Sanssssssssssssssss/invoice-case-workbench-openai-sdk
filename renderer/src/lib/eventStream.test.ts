import { describe, expect, it } from 'vitest'
import { parseAgentRunStreamMessage, parseLiveStatusMessage, parseTraceEventMessage } from './eventStream'

describe('parseTraceEventMessage', () => {
  it('parses valid trace event payloads', () => {
    expect(parseTraceEventMessage(JSON.stringify({ event_id: 'evt_1', run_id: 'run_1' })).event_id).toBe('evt_1')
  })

  it('rejects malformed payloads', () => {
    expect(() => parseTraceEventMessage(JSON.stringify({ event_id: 1 }))).toThrow('Invalid trace_event payload')
  })
})

describe('parseLiveStatusMessage', () => {
  it('parses valid live status payloads', () => {
    const status = parseLiveStatusMessage(JSON.stringify({ latestEventId: 'evt_1', isRunning: true, latestThinking: 'checking' }))
    expect(status.latestThinking).toBe('checking')
  })

  it('rejects malformed live status payloads', () => {
    expect(() => parseLiveStatusMessage(JSON.stringify({ latestEventId: 1 }))).toThrow('Invalid live_status payload')
  })
})

describe('parseAgentRunStreamMessage', () => {
  it('parses valid agent run stream payloads', () => {
    const event = parseAgentRunStreamMessage(JSON.stringify({ event_id: 'run_1:stream:1', run_id: 'run_1', kind: 'assistant_delta', payload: { delta: 'hi' } }))
    expect(event?.kind).toBe('assistant_delta')
  })

  it('ignores empty agent run stream payloads', () => {
    expect(parseAgentRunStreamMessage(undefined)).toBeNull()
    expect(parseAgentRunStreamMessage('')).toBeNull()
    expect(parseAgentRunStreamMessage('undefined')).toBeNull()
    expect(parseAgentRunStreamMessage('null')).toBeNull()
  })

  it('rejects malformed agent run stream payloads', () => {
    expect(() => parseAgentRunStreamMessage(JSON.stringify({ event_id: 'evt_1' }))).toThrow('Invalid agent run stream payload')
  })
})
