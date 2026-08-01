import type { AgentRunStreamEvent, LiveStatus, TraceEvent } from '@/types'

export function parseTraceEventMessage(data: string): TraceEvent {
  const parsed = JSON.parse(data) as Partial<TraceEvent>
  if (!parsed || typeof parsed.event_id !== 'string' || typeof parsed.run_id !== 'string') {
    throw new Error('Invalid trace_event payload')
  }
  return parsed as TraceEvent
}

export function parseLiveStatusMessage(data: string): LiveStatus {
  const parsed = JSON.parse(data) as Partial<LiveStatus>
  if (!parsed || typeof parsed.latestEventId !== 'string' || typeof parsed.isRunning !== 'boolean') {
    throw new Error('Invalid live_status payload')
  }
  return parsed as LiveStatus
}

export function parseAgentRunStreamMessage(data: string | null | undefined): AgentRunStreamEvent | null {
  if (typeof data !== 'string' || !data.trim()) {
    return null
  }
  const text = data.trim()
  if (text === 'undefined' || text === 'null') {
    return null
  }
  const parsed = JSON.parse(text) as Partial<AgentRunStreamEvent>
  if (
    !parsed ||
    typeof parsed.event_id !== 'string' ||
    typeof parsed.run_id !== 'string' ||
    typeof parsed.seq !== 'number' ||
    !Number.isFinite(parsed.seq) ||
    typeof parsed.kind !== 'string'
  ) {
    throw new Error('Invalid agent run stream payload')
  }
  return parsed as AgentRunStreamEvent
}

export function isNewAgentRunStreamEvent(event: AgentRunStreamEvent, lastSeq: number): boolean {
  return event.seq > lastSeq
}
