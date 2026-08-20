import type { ApprovalInterrupt, ConversationItem, LiveStatus, TraceEvent } from '@/types'

export type ApprovalPhase = 'idle' | 'waiting' | 'executing'

export interface CaseRuntimeState {
  running: boolean
  liveEvents: TraceEvent[]
  liveStatus: LiveStatus | null
  optimisticMessages: ConversationItem[]
  draft: string
  files: File[]
  pendingApprovals: ApprovalInterrupt[]
  approvalPhase: ApprovalPhase
  approvalInFlight: ApprovalInterrupt | null
  resolvedApprovalKeys: string[]
  selectedRunId: string
}

export type CaseRuntimeMap = Record<string, CaseRuntimeState>

export function createCaseRuntimeState(): CaseRuntimeState {
  return {
    running: false,
    liveEvents: [],
    liveStatus: null,
    optimisticMessages: [],
    draft: '',
    files: [],
    pendingApprovals: [],
    approvalPhase: 'idle',
    approvalInFlight: null,
    resolvedApprovalKeys: [],
    selectedRunId: ''
  }
}

export function caseRuntime(runtimeByCase: CaseRuntimeMap, caseId: string): CaseRuntimeState {
  return runtimeByCase[caseId] ?? createCaseRuntimeState()
}

export function replaceCaseRuntime(
  runtimeByCase: CaseRuntimeMap,
  caseId: string,
  update: (current: CaseRuntimeState) => CaseRuntimeState
): CaseRuntimeMap {
  return { ...runtimeByCase, [caseId]: update(caseRuntime(runtimeByCase, caseId)) }
}

export function approvalKey(approval: ApprovalInterrupt): string {
  return [approval.run_id, approval.tool, approval.input_sha256 || approval.input_preview || approval.reason].join(':')
}

export function canStartRun(current: CaseRuntimeState, serverRunning = false): boolean {
  return !current.running && !serverRunning && current.pendingApprovals.length === 0 && current.approvalPhase !== 'executing'
}

export function receiveApprovals(current: CaseRuntimeState, approvals: ApprovalInterrupt[]): CaseRuntimeState {
  const inFlightKey = current.approvalInFlight ? approvalKey(current.approvalInFlight) : ''
  const pending = approvals.filter((approval) => {
    const key = approvalKey(approval)
    return key !== inFlightKey && !current.resolvedApprovalKeys.includes(key)
  })
  if (!pending.length) return current
  return {
    ...current,
    pendingApprovals: pending,
    approvalPhase: 'waiting',
    approvalInFlight: null,
    running: false
  }
}

export function beginApproval(current: CaseRuntimeState): CaseRuntimeState {
  const approval = current.pendingApprovals[0]
  if (!approval) return current
  return {
    ...current,
    running: true,
    pendingApprovals: [],
    approvalPhase: 'executing',
    approvalInFlight: approval
  }
}

export function acceptApproval(current: CaseRuntimeState): CaseRuntimeState {
  if (!current.approvalInFlight) return current
  const key = approvalKey(current.approvalInFlight)
  return {
    ...current,
    resolvedApprovalKeys: current.resolvedApprovalKeys.includes(key)
      ? current.resolvedApprovalKeys
      : [...current.resolvedApprovalKeys, key]
  }
}

export function retryApproval(current: CaseRuntimeState): CaseRuntimeState {
  const approval = current.approvalInFlight
  return {
    ...current,
    running: false,
    pendingApprovals: approval ? [approval] : [],
    approvalPhase: approval ? 'waiting' : 'idle',
    approvalInFlight: null
  }
}

export function finishRun(current: CaseRuntimeState): CaseRuntimeState {
  return {
    ...current,
    running: false,
    pendingApprovals: [],
    approvalPhase: 'idle',
    approvalInFlight: null
  }
}
