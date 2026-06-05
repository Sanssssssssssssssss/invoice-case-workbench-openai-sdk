import { describe, expect, it } from 'vitest'
import { approvalInterruptsFromEvents, approvalInterruptsFromTrace } from './approvals'
import type { TraceEvent } from '@/types'

function event(raw_kind: string, case_seq: number, payload: Record<string, unknown>): TraceEvent {
  return {
    event_id: `evt_${case_seq}`,
    run_id: 'run_1',
    case_id: 'case_1',
    seq: case_seq,
    case_seq,
    ts: '',
    kind: 'approval' as TraceEvent['kind'],
    raw_kind,
    name: 'write_case_file',
    status: raw_kind === 'approval_decision' ? 'approved' : 'waiting',
    summary: '',
    parent_event_id: '',
    caused_by_event_id: '',
    duration_ms: null,
    token_count: null,
    input_preview: '',
    output_preview: '',
    payload,
    raw: {}
  }
}

describe('approvalInterruptsFromTrace', () => {
  it('normalizes pending approvals from turn response trace', () => {
    const approvals = approvalInterruptsFromTrace('case_1', {
      run_id: 'run_1',
      interrupts: [{ tool: 'write_case_file', risk_level: 'local_write', input_sha256: 'abc' }]
    })

    expect(approvals).toEqual([
      expect.objectContaining({
        case_id: 'case_1',
        run_id: 'run_1',
        tool: 'write_case_file',
        risk_level: 'local_write',
        input_sha256: 'abc'
      })
    ])
  })
})

describe('approvalInterruptsFromEvents', () => {
  it('recovers the latest pending approval from persisted run events', () => {
    const approvals = approvalInterruptsFromEvents('case_1', 'run_1', [
      event('approval_interrupt', 1, {
        approval_payload: { tool: 'write_case_file', risk_level: 'local_write', input_preview: '{}' }
      })
    ])

    expect(approvals).toEqual([
      expect.objectContaining({
        case_id: 'case_1',
        run_id: 'run_1',
        tool: 'write_case_file',
        risk_level: 'local_write'
      })
    ])
  })

  it('does not recover approvals already followed by a decision', () => {
    const approvals = approvalInterruptsFromEvents('case_1', 'run_1', [
      event('approval_interrupt', 1, { approval_payload: { tool: 'write_case_file' } }),
      event('approval_decision', 2, { tool: 'write_case_file', approved: true })
    ])

    expect(approvals).toEqual([])
  })
})
