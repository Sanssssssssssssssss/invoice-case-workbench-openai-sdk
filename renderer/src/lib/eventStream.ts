import type { LiveStatus, TraceEvent } from '@/types'

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
