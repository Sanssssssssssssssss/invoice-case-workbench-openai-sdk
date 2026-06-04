import { describe, expect, it } from 'vitest'
import { eventTitle, mergeEvents, timelinePositionClass } from './trace'
import type { TraceEvent } from '@/types'

function event(event_id: string, case_seq: number): TraceEvent {
  return {
    event_id,
    run_id: 'run_1',
    case_id: 'case_1',
    seq: case_seq,
    case_seq,
    ts: '',
    kind: 'tool',
    raw_kind: 'tool_call',
    name: event_id,
    status: 'ok',
    summary: '',
    parent_event_id: '',
    caused_by_event_id: '',
    duration_ms: null,
    token_count: null,
    input_preview: '',
    output_preview: '',
    payload: {},
    raw: {}
  }
}

describe('mergeEvents', () => {
  it('deduplicates and sorts by case sequence', () => {
    expect(mergeEvents([event('b', 2), event('a', 1)], [event('b', 3), event('c', 4)]).map((item) => item.event_id)).toEqual([
      'a',
      'b',
      'c'
    ])
  })

  it('updates a stable thinking row instead of appending streaming snapshots', () => {
    const first = { ...event('run_1:thinking:planner:root_step_0', 1), kind: 'thinking' as const, summary: '20 chars' }
    const latest = { ...event('run_1:thinking:planner:root_step_0', 6), kind: 'thinking' as const, summary: '120 chars' }

    const merged = mergeEvents([first], [latest])

    expect(merged).toHaveLength(1)
    expect(merged[0].summary).toBe('120 chars')
    expect(merged[0].case_seq).toBe(6)
  })
})

describe('timelinePositionClass', () => {
  it('marks first, middle, last, and single rows', () => {
    expect(timelinePositionClass(0, 1)).toBe('single')
    expect(timelinePositionClass(0, 3)).toBe('first')
    expect(timelinePositionClass(1, 3)).toBe('middle')
    expect(timelinePositionClass(2, 3)).toBe('last')
  })
})

describe('eventTitle', () => {
  it('labels thinking events in Chinese', () => {
    expect(eventTitle({ ...event('thinking', 1), kind: 'thinking', raw_kind: 'model_thinking', name: 'evidence_reviewer' })).toContain('模型思考')
  })

  it('labels summarizer model calls as attachment summaries', () => {
    expect(eventTitle({ ...event('summary', 1), kind: 'artifact_summary', raw_kind: 'model_call', name: 'summarizer' })).toBe('附件摘要')
  })
})
