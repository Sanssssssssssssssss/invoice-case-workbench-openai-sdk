import { describe, expect, it } from 'vitest'
import { acceptApproval, approvalKey, beginApproval, canStartRun, caseRuntime, createCaseRuntimeState, finishRun, receiveApprovals, replaceCaseRuntime, retryApproval } from './caseRuntime'
import type { ApprovalInterrupt } from '@/types'

const writeApproval: ApprovalInterrupt = {
  type: 'tool_approval',
  case_id: 'case_a',
  run_id: 'run_a',
  tool: 'write_case_file',
  risk_level: 'write',
  input_preview: 'report.md',
  input_sha256: 'write-hash',
  reason: 'write report'
}

const pdfApproval: ApprovalInterrupt = {
  ...writeApproval,
  tool: 'render_report_pdf',
  input_preview: 'report.pdf',
  input_sha256: 'pdf-hash',
  reason: 'render PDF'
}

describe('case runtime isolation', () => {
  it('keeps simultaneous case runs independent', () => {
    let runtimes = {}
    runtimes = replaceCaseRuntime(runtimes, 'case_a', (current) => ({ ...current, running: true, selectedRunId: 'run_a', draft: 'draft a' }))
    runtimes = replaceCaseRuntime(runtimes, 'case_b', (current) => ({ ...current, running: true, selectedRunId: 'run_b', draft: 'draft b' }))
    runtimes = replaceCaseRuntime(runtimes, 'case_a', finishRun)

    expect(caseRuntime(runtimes, 'case_a')).toMatchObject({ running: false, selectedRunId: 'run_a', draft: 'draft a' })
    expect(caseRuntime(runtimes, 'case_b')).toMatchObject({ running: true, selectedRunId: 'run_b', draft: 'draft b' })
  })

  it('blocks a second run in the same case only', () => {
    const running = { ...createCaseRuntimeState(), running: true }

    expect(canStartRun(running)).toBe(false)
    expect(canStartRun(createCaseRuntimeState(), true)).toBe(false)
    expect(canStartRun(createCaseRuntimeState())).toBe(true)
  })

  it('does not revive an approved request from stale run data', () => {
    const waiting = receiveApprovals(createCaseRuntimeState(), [writeApproval])
    const executing = beginApproval(waiting)
    const beforeAccepted = receiveApprovals(executing, [writeApproval])
    const accepted = acceptApproval(beforeAccepted)
    const afterStalePoll = receiveApprovals(accepted, [writeApproval])

    expect(beforeAccepted.approvalPhase).toBe('executing')
    expect(afterStalePoll.pendingApprovals).toEqual([])
    expect(afterStalePoll.approvalPhase).toBe('executing')
    expect(afterStalePoll.resolvedApprovalKeys).toEqual([approvalKey(writeApproval)])
  })

  it('returns a failed approval submission to waiting so it can be retried', () => {
    const waiting = receiveApprovals(createCaseRuntimeState(), [writeApproval])
    const executing = beginApproval(waiting)
    const retryable = retryApproval(executing)

    expect(executing.resolvedApprovalKeys).toEqual([])
    expect(retryable).toMatchObject({
      running: false,
      pendingApprovals: [writeApproval],
      approvalPhase: 'waiting',
      approvalInFlight: null,
      resolvedApprovalKeys: []
    })
  })

  it('shows a distinct consecutive approval and clears it on final', () => {
    const first = acceptApproval(beginApproval(receiveApprovals(createCaseRuntimeState(), [writeApproval])))
    const second = receiveApprovals(first, [pdfApproval])

    expect(second.pendingApprovals).toEqual([pdfApproval])
    expect(second.approvalPhase).toBe('waiting')
    expect(finishRun(second)).toMatchObject({ pendingApprovals: [], approvalPhase: 'idle', approvalInFlight: null })
  })
})
