import { describe, expect, it } from 'vitest'
import { modelMetricItems, modelMetricsFromEvent } from './modelMetrics'
import type { TraceEvent } from '@/types'

function event(payload: Record<string, unknown>, kind: TraceEvent['kind'] = 'model'): TraceEvent {
  return {
    event_id: 'evt_1',
    run_id: 'run_1',
    case_id: 'case_1',
    seq: 1,
    case_seq: 1,
    ts: '',
    kind,
    raw_kind: 'model_call',
    name: 'planner',
    status: 'ok',
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

describe('modelMetricsFromEvent', () => {
  it('extracts model metric payloads', () => {
    const metrics = modelMetricsFromEvent(
      event({
        latency_ms: 120.4,
        ttft_ms: 42,
        prompt_tokens: 1000,
        completion_tokens: 120,
        total_tokens: 1120,
        cached_tokens: 500,
        cache_hit_ratio: 0.5,
        prompt_cache_key: 'invoice_workbench:tenant:planner:v1:tools'
      })
    )

    expect(metrics?.latencyMs).toBe(120.4)
    expect(metrics?.ttftMs).toBe(42)
    expect(metrics?.totalTokens).toBe(1120)
    expect(metrics?.promptCacheKey).toContain('invoice_workbench')
  })

  it('ignores non-model and old empty traces', () => {
    expect(modelMetricsFromEvent(event({}, 'tool'))).toBeNull()
    expect(modelMetricsFromEvent(event({}))).toBeNull()
  })

  it('formats only present values', () => {
    const metrics = modelMetricsFromEvent(event({ latency_ms: 120, prompt_cache_key: 'cache-key' }))

    expect(modelMetricItems(metrics!)).toEqual([
      { label: 'latency', value: '120ms' },
      { label: 'cache key', value: 'cache-key', title: 'cache-key' }
    ])
  })
})
