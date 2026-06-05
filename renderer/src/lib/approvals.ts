import type { ApprovalInterrupt, TraceEvent } from '@/types'

export function approvalInterruptsFromTrace(caseId: string, trace: Record<string, unknown>): ApprovalInterrupt[] {
  const interrupts = arrayValue(trace.interrupts) || arrayValue(trace.pending_approvals) || arrayValue(trace.pendingApprovals) || []
  const runId = stringValue(trace.run_id)
  return interrupts.filter(isRecord).map((item) => normalizeApproval(caseId, runId, item))
}

export function approvalInterruptsFromEvents(caseId: string, runId: string, events: TraceEvent[]): ApprovalInterrupt[] {
  const runEvents = events.filter((event) => event.run_id === runId)
  const latestDecisionPosition = runEvents.reduce((latest, event) => {
    if (event.raw_kind !== 'approval_decision') return latest
    return Math.max(latest, eventPosition(event))
  }, -1)
  return runEvents
    .filter((event) => event.raw_kind === 'approval_interrupt' && eventPosition(event) > latestDecisionPosition)
    .map((event) => {
      const payload = isRecord(event.payload.approval_payload) ? event.payload.approval_payload : event.payload
      return normalizeApproval(caseId, runId, payload)
    })
}

function normalizeApproval(caseId: string, runId: string, item: Record<string, unknown>): ApprovalInterrupt {
  return {
    type: stringValue(item.type) || 'tool_approval',
    case_id: stringValue(item.case_id) || caseId,
    run_id: stringValue(item.run_id) || runId,
    tool: stringValue(item.tool) || 'tool',
    risk_level: stringValue(item.risk_level) || 'read',
    input_preview: stringValue(item.input_preview),
    input_sha256: stringValue(item.input_sha256),
    reason: stringValue(item.reason) || 'This action requires approval.'
  }
}

function eventPosition(event: TraceEvent): number {
  return event.case_seq || event.seq || 0
}

function arrayValue(value: unknown): unknown[] | null {
  return Array.isArray(value) ? value : null
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}
