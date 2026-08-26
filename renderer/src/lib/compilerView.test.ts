import { describe, expect, it } from 'vitest'
import type { ProofPlan, TraceEvent } from '@/types'
import { compilerChildRunView, compilerStageForEvent, flattenProofTree, initialCompilerTraceSelection, proofStatusLabel } from './compilerView'

const plan: ProofPlan = {
  plan_id: 'plan_1',
  version: '1',
  objective: 'test',
  active_requirement_ids: ['vendor_active'],
  policy_refs: [],
  roots: { vendor_active: 'all_1' },
  nodes: [
    { id: 'all_1', kind: 'ALL', statement: '', depends_on: ['exists', 'active'], requirement_refs: [], policy_refs: [] },
    { id: 'exists', kind: 'CHECK', statement: 'Vendor exists', depends_on: [], requirement_refs: ['vendor_active'], policy_refs: [] },
    { id: 'active', kind: 'CHECK', statement: 'Vendor is active', depends_on: [], requirement_refs: ['vendor_active'], policy_refs: [] }
  ]
}

function event(name: string, rawKind = 'provider_call'): TraceEvent {
  return {
    event_id: name,
    run_id: 'run_1',
    case_id: 'case_1',
    seq: 1,
    case_seq: 1,
    ts: '',
    kind: 'model',
    raw_kind: rawKind,
    name,
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

function shapedEvent({
  id,
  rawKind,
  name,
  role = '',
  summary = '',
  phase = ''
}: {
  id: string
  rawKind: string
  name: string
  role?: string
  summary?: string
  phase?: string
}): TraceEvent {
  return {
    ...event(id, rawKind),
    name,
    summary,
    payload: role ? { role } : {},
    raw: phase ? { phase } : {}
  }
}

describe('compiler proof view', () => {
  it('flattens an aggregate proof root into readable depth rows', () => {
    expect(flattenProofTree(plan, 'all_1').map(({ node, depth }) => [node.id, depth])).toEqual([
      ['all_1', 0],
      ['exists', 1],
      ['active', 1]
    ])
  })

  it('uses business-facing three-value labels', () => {
    expect(proofStatusLabel('SUPPORTED')).toBe('支持')
    expect(proofStatusLabel('CONTRADICTED')).toBe('反驳')
    expect(proofStatusLabel('NOT_FOUND')).toBe('未完成')
  })
})

describe('compiler trace stages', () => {
  it('routes provider calls to their visible compiler stage', () => {
    expect(compilerStageForEvent(event('task_compiler'))).toBe('compiler')
    expect(compilerStageForEvent(event('executor'))).toBe('executor')
    expect(compilerStageForEvent(event('fine_verifier'))).toBe('verifier')
  })

  it('routes persistence and reviewer completion without relying on event kind', () => {
    expect(compilerStageForEvent(event('evidence_reviewer', 'role_call'))).toBe('kernel')
    expect(compilerStageForEvent(event('write_case_patch', 'tool_call'))).toBe('case_store')
    expect(compilerStageForEvent(event('assistant_reply', 'final_answer'))).toBe('case_store')
  })

  it('keeps generic orchestration outside the five evidence stages', () => {
    expect(compilerStageForEvent(event('allow', 'policy_check'))).toBeNull()
  })

  it('does not classify attachment reading or incidental summary text as Executor', () => {
    const attachment = shapedEvent({ id: 'attachment', rawKind: 'tool_call', name: 'read_attachment' })
    const incidental = shapedEvent({
      id: 'policy',
      rawKind: 'policy_check',
      name: 'allow',
      summary: 'allowed executor and evidence_reviewer to continue'
    })
    expect(compilerStageForEvent(attachment)).toBeNull()
    expect(compilerStageForEvent(incidental)).toBeNull()
  })

  it('does not classify incidental phase text or orchestration names', () => {
    const incidentalPhase = shapedEvent({
      id: 'checkpoint',
      rawKind: 'checkpoint',
      name: 'trace_ckpt_misc',
      phase: 'after_evidence_reviewed_cleanup'
    })
    const orchestrationName = shapedEvent({ id: 'runtime', rawKind: 'runtime_policy', name: 'task_compiler' })
    expect(compilerStageForEvent(incidentalPhase)).toBeNull()
    expect(compilerStageForEvent(orchestrationName)).toBeNull()
  })

  it('uses exact role/name fields and exact checkpoint phases', () => {
    expect(compilerStageForEvent(shapedEvent({ id: 'compiler', rawKind: 'provider_call', name: 'provider', role: 'task_compiler' }))).toBe('compiler')
    expect(compilerStageForEvent(shapedEvent({ id: 'kernel', rawKind: 'observation', name: 'role/evidence_reviewer' }))).toBe('kernel')
    expect(compilerStageForEvent(shapedEvent({ id: 'kernel-checkpoint', rawKind: 'checkpoint', name: 'trace_ckpt_2', phase: 'evidence_reviewed' }))).toBe('kernel')
    expect(compilerStageForEvent(shapedEvent({ id: 'commit-checkpoint', rawKind: 'checkpoint', name: 'trace_ckpt_4', phase: 'patch_written' }))).toBe('case_store')
  })
})

describe('initial compiler trace selection', () => {
  it('selects the first non-empty stage and prefers its provider call', () => {
    const modelEvent = shapedEvent({ id: 'model', rawKind: 'model_call', name: 'task_compiler' })
    const providerEvent = shapedEvent({ id: 'provider', rawKind: 'provider_call', name: 'task_compiler' })
    const verifierEvent = shapedEvent({ id: 'verifier', rawKind: 'provider_call', name: 'fine_verifier' })

    expect(initialCompilerTraceSelection([modelEvent, providerEvent, verifierEvent])).toEqual({
      stage: 'compiler',
      event: providerEvent
    })
  })

  it('skips orchestration and falls forward to the first stage with canonical events', () => {
    const attachment = shapedEvent({ id: 'attachment', rawKind: 'tool_call', name: 'read_attachment' })
    const executor = shapedEvent({ id: 'executor', rawKind: 'provider_call', name: 'executor' })
    expect(initialCompilerTraceSelection([attachment, executor])).toEqual({ stage: 'executor', event: executor })
  })

  it('keeps an empty run on Compiler with no selected event', () => {
    expect(initialCompilerTraceSelection([])).toEqual({ stage: 'compiler', event: null })
    expect(initialCompilerTraceSelection([event('allow', 'policy_check')])).toEqual({ stage: 'compiler', event: null })
  })
})

describe('compiler child run view', () => {
  it('derives the latest revision and exposes only bounded public operational fields', () => {
    const events = Array.from({ length: 10 }, (_, index) => ({
      ...event(`child-${index}`, 'model_thinking'),
      case_seq: index,
      payload: {
        compiler_run_id: 'compiler_child',
        compiler_revision: index === 0 ? 1 : 2,
        stage: 'executor',
        status: index === 9 ? 'frontier_committed' : 'frontier_started',
        action: `action ${index}`,
        public_reason: `reason ${index}`,
        focused_check_ids: ['check_total'],
        check_count: 1,
        reasoning_excerpt: 'private',
        raw_prompt: 'private'
      }
    }))

    const view = compilerChildRunView(events)
    expect(view).toMatchObject({
      compilerRunId: 'compiler_child',
      revision: 2,
      status: 'completed',
      completedChecks: 1,
      totalChecks: 1
    })
    expect(view?.events).toHaveLength(8)
    expect(JSON.stringify(view)).not.toContain('private')
  })

  it('does not treat a phase-local completion as completion of the whole child run', () => {
    const childEvent = (id: string, caseSeq: number, payload: Record<string, unknown>) => ({
      ...event(id, 'model_thinking'),
      case_seq: caseSeq,
      payload: { compiler_run_id: 'compiler_live', compiler_revision: 1, ...payload }
    })
    const view = compilerChildRunView([
      childEvent('plan', 1, { stage: 'task_compiler', status: 'completed', check_count: 6 }),
      childEvent('check-1', 2, { stage: 'proof_kernel', status: 'frontier_committed', focused_check_ids: ['check_1'] }),
      childEvent('check-2', 3, { stage: 'proof_kernel', status: 'frontier_committed', focused_check_ids: ['check_2'] }),
      childEvent('partial-proof', 4, { stage: 'proof_kernel', status: 'completed', supported_count: 1 }),
      childEvent('check-3-started', 5, { stage: 'fine_verifier', status: 'started', focused_check_ids: ['check_3'], check_count: 1 })
    ])

    expect(view).toMatchObject({
      status: 'running',
      completedChecks: 2,
      totalChecks: 6,
      activeCheckId: 'check_3'
    })
  })

  it('does not count a rolled-back CHECK as completed', () => {
    const childEvent = (id: string, caseSeq: number, status: string, checkId = '') => ({
      ...event(id, 'model_thinking'),
      case_seq: caseSeq,
      payload: {
        compiler_run_id: 'compiler_paused',
        compiler_revision: 1,
        status,
        check_count: 6,
        focused_check_ids: checkId ? [checkId] : []
      }
    })
    const events = [childEvent('plan', 1, 'completed')]
    for (let index = 1; index <= 5; index += 1) events.push(childEvent(`check-${index}`, index + 1, 'frontier_committed', `check_${index}`))
    events.push(childEvent('check-6-rollback', 7, 'frontier_rolled_back', 'check_6'))

    expect(compilerChildRunView(events)).toMatchObject({ status: 'running', completedChecks: 5, totalChecks: 6 })
  })

  it('returns null when the selected run has no child metadata', () => {
    expect(compilerChildRunView([event('task_compiler')])).toBeNull()
  })
})
