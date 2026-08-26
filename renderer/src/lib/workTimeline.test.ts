import { describe, expect, it } from 'vitest'
import type { TraceEvent } from '@/types'
import { publicWorkTimeline } from './workTimeline'

function trace(
  id: string,
  seq: number,
  rawKind: string,
  name: string,
  payload: Record<string, unknown> = {}
): TraceEvent {
  return {
    event_id: id,
    run_id: 'run_live',
    case_id: 'case_1',
    seq,
    case_seq: seq,
    ts: `2026-08-26T13:31:${String(seq).padStart(2, '0')}.000Z`,
    kind: rawKind.includes('tool') ? 'tool' : rawKind === 'model_thinking' ? 'thinking' : 'model',
    raw_kind: rawKind,
    name,
    status: 'ok',
    summary: `${name} summary`,
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

describe('public work timeline', () => {
  it('creates an immediate optimistic Agent shell before the first public event', () => {
    expect(publicWorkTimeline([], true).items).toEqual([
      expect.objectContaining({ id: 'agent-turn-shell', status: 'running', optimistic: true })
    ])
  })

  it('updates one phase item from started to completed without exposing hidden fields', () => {
    const started = trace('phase-start', 1, 'model_started', 'fine_verifier', {
      compiler_run_id: 'compiler_1',
      compiler_revision: 1,
      stage: 'fine_verifier',
      status: 'started',
      action: 'Fine Verifier 正在核查',
      public_reason: '只读取公开核查输入。',
      focused_check_ids: ['check_total'],
      reasoning_excerpt: 'private chain',
      raw_prompt: 'private prompt'
    })
    const completed = trace('phase-complete', 2, 'model_thinking', 'fine_verifier', {
      compiler_run_id: 'compiler_1',
      compiler_revision: 1,
      stage: 'fine_verifier',
      status: 'completed',
      action: 'Fine Verifier 已完成核查',
      public_reason: '公开结果已交给 Kernel。',
      focused_check_ids: ['check_total']
    })

    const timeline = publicWorkTimeline([completed, started], true)
    expect(timeline.items).toHaveLength(1)
    expect(timeline.items[0]).toMatchObject({
      id: 'phase:compiler_1:1:check_total:fine_verifier:1',
      actor: 'Fine Verifier',
      title: 'Fine Verifier 已完成核查',
      publicReason: '公开结果已交给 Kernel。',
      status: 'completed',
      checkId: 'check_total'
    })
    expect(JSON.stringify(timeline)).not.toContain('private')
  })

  it('pairs a tool start and finish into one stable lifecycle item', () => {
    const frontier = trace('frontier', 1, 'model_thinking', 'executor', {
      compiler_run_id: 'compiler_1',
      stage: 'executor',
      status: 'frontier_started',
      focused_check_ids: ['check_tax'],
      frontier_attempt: 1,
      action: '开始 CHECK'
    })
    const started = trace('tool-start', 2, 'tool_started', 'bind_claim', {
      compiler_run_id: 'compiler_1',
      stage: 'executor',
      status: 'started',
      tool: 'bind_claim',
      action: 'Evidence Worker 调用 bind_claim'
    })
    const finished = trace('tool-finish', 3, 'tool_finished', 'bind_claim', {
      compiler_run_id: 'compiler_1',
      stage: 'executor',
      status: 'completed',
      tool: 'bind_claim',
      action: 'bind_claim 已完成',
      public_reason: '沙箱已接受这一步。'
    })

    const timeline = publicWorkTimeline([frontier, started, finished], true)
    const tools = timeline.items.filter((item) => item.tool === 'bind_claim')
    expect(tools).toHaveLength(1)
    expect(tools[0]).toMatchObject({ actor: 'Executor', status: 'completed', checkId: 'check_tax', title: 'bind_claim 已完成' })
  })

  it('projects Manager decisions using only public operational fields', () => {
    const decision = trace('decision', 1, 'supervisor_decision', 'delegate_agent', {
      action: 'delegate_agent',
      target: 'evidence_reviewer',
      prompt: 'private prompt'
    })
    const timeline = publicWorkTimeline([decision], false)
    expect(timeline.items[0]).toMatchObject({ title: 'Supervisor：delegate_agent → evidence_reviewer' })
    expect(JSON.stringify(timeline)).not.toContain('private prompt')
  })

  it('marks unfinished work as interrupted when the run is no longer active', () => {
    const started = trace('phase-start', 1, 'model_started', 'executor', {
      status: 'started',
      stage: 'executor',
      action: 'Executor 正在工作'
    })

    expect(publicWorkTimeline([started], false).items[0].status).toBe('warning')
  })

  it('completes a Manager phase that handed off to a later public decision', () => {
    const thinking = trace('manager-thinking', 1, 'model_thinking', 'planner', {
      status: 'running',
      stage: 'planner',
      action: 'Manager 正在判断下一步'
    })
    const decision = trace('manager-decision', 2, 'supervisor_decision', 'delegate_agent', {
      status: 'completed',
      action: 'delegate_agent',
      target: 'evidence_reviewer'
    })

    const timeline = publicWorkTimeline([thinking, decision], false)
    expect(timeline.items.find((item) => item.id.startsWith('phase:'))?.status).toBe('completed')
  })

  it('does not infer completion for an unfinished tool from a later public event', () => {
    const tool = trace('tool-start', 1, 'tool_started', 'inspect_compiler_run', {
      status: 'started',
      tool: 'inspect_compiler_run'
    })
    const decision = trace('decision', 2, 'supervisor_decision', 'stop', {
      status: 'completed',
      action: 'stop'
    })

    const timeline = publicWorkTimeline([tool, decision], false)
    expect(timeline.items.find((item) => item.tool === 'inspect_compiler_run')?.status).toBe('warning')
  })

  it('maps blocked supervisor and policy decisions from structured payloads', () => {
    const blocked = trace('blocked', 1, 'supervisor_decision_blocked', 'delegate_agent', {
      action: 'delegate_agent'
    })
    const policyRejected = trace('policy-rejected', 2, 'policy_check', 'policy_check', {
      policy_check: { allowed: false }
    })
    const policyAllowed = trace('policy-allowed', 3, 'policy_check', 'policy_check', {
      policy_check: { allowed: true }
    })

    expect(publicWorkTimeline([blocked], true).items[0].status).toBe('warning')
    expect(publicWorkTimeline([policyRejected], true).items[0].status).toBe('warning')
    expect(publicWorkTimeline([policyAllowed], true).items[0].status).toBe('completed')
  })

  it('closes prior Manager and Kernel phases when later public terminal events arrive', () => {
    const manager = trace('manager-running', 1, 'model_thinking', 'planner', {
      stage: 'planner',
      status: 'running',
      action: 'Manager 正在判断'
    })
    const decision = trace('manager-decision', 2, 'supervisor_decision', 'delegate_agent', {
      action: 'delegate_agent',
      target: 'evidence_reviewer'
    })
    const kernel = trace('kernel-running', 3, 'model_started', 'proof_kernel', {
      compiler_run_id: 'compiler_1',
      stage: 'proof_kernel',
      status: 'started',
      focused_check_ids: ['check_total']
    })
    const committed = trace('kernel-committed', 4, 'model_thinking', 'proof_kernel', {
      compiler_run_id: 'compiler_1',
      stage: 'proof_kernel',
      status: 'frontier_committed',
      focused_check_ids: ['check_total']
    })

    const items = publicWorkTimeline([manager, decision, kernel, committed], true).items
    expect(items.find((item) => item.actor === '规划器')?.status).toBe('completed')
    expect(items.find((item) => item.id.startsWith('phase:') && item.actor === 'proof_kernel')?.status).toBe('completed')
  })
})
