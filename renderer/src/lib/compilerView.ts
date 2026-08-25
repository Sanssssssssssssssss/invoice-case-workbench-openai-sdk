import type { CaseState, ProofNode, ProofPlan, ProofStatus, TraceEvent } from '@/types'

export type CompilerStageId = 'compiler' | 'executor' | 'verifier' | 'kernel' | 'case_store'

export interface CompilerStage {
  id: CompilerStageId
  step: number
  label: string
  shortLabel: string
  description: string
}

export const compilerStages: CompilerStage[] = [
  { id: 'compiler', step: 1, label: 'Task Compiler', shortLabel: 'Plan', description: '把 Requirement 与 Policy 编译成 ProofPlan' },
  { id: 'executor', step: 2, label: 'Executor / Sandbox', shortLabel: 'Work', description: '读源、绑定 Claim、提交原子检查' },
  { id: 'verifier', step: 3, label: 'Fine Verifier', shortLabel: 'Verify', description: '逐项核查 Claim 与来源是否支持命题' },
  { id: 'kernel', step: 4, label: 'Proof Kernel', shortLabel: 'Proof', description: '用三值逻辑传播并生成 DecisionProof' },
  { id: 'case_store', step: 5, label: 'CaseStore / 回复', shortLabel: 'Commit', description: '投影状态、持久化并回复用户' }
]

export interface ProofTreeRow {
  node: ProofNode
  depth: number
  repeated: boolean
}

export interface CompilerTraceSelection {
  stage: CompilerStageId
  event: TraceEvent | null
}

export interface CompilerChildRunView {
  compilerRunId: string
  revision: number
  status: string
  completedChecks: number
  totalChecks: number
  activeCheckId: string
  events: Array<{
    eventId: string
    stage: string
    status: string
    action: string
    publicReason: string
    checkId: string
    diagnosticCode: string
  }>
}

export function flattenProofTree(plan: ProofPlan, rootId: string): ProofTreeRow[] {
  const nodes = new Map(plan.nodes.map((node) => [node.id, node]))
  const rows: ProofTreeRow[] = []
  const visited = new Set<string>()

  const visit = (nodeId: string, depth: number, path: Set<string>) => {
    const node = nodes.get(nodeId)
    if (!node || path.has(nodeId)) return
    const repeated = visited.has(nodeId)
    rows.push({ node, depth, repeated })
    if (repeated) return
    visited.add(nodeId)
    const nextPath = new Set(path)
    nextPath.add(nodeId)
    for (const childId of node.depends_on) visit(childId, depth + 1, nextPath)
  }

  visit(rootId, 0, new Set())
  return rows
}

export function proofStatusLabel(status: ProofStatus | string) {
  return {
    SUPPORTED: '支持',
    CONTRADICTED: '反驳',
    NOT_FOUND: '未完成'
  }[status] ?? status
}

export function proofStatusTone(status: ProofStatus | string) {
  if (status === 'SUPPORTED') return 'supported'
  if (status === 'CONTRADICTED') return 'contradicted'
  return 'not-found'
}

export function compilerArtifactStats(caseState?: CaseState) {
  const artifact = caseState?.review_artifact
  const proof = caseState?.compiled_proof
  return {
    requirements: artifact?.plan.active_requirement_ids.length ?? 0,
    checks: artifact?.plan.nodes.filter((node) => node.kind === 'CHECK').length ?? 0,
    sources: artifact?.evidence_ir.source_ids.length ?? 0,
    claims: artifact?.evidence_ir.claims.length ?? 0,
    decisions: proof?.decisions.length ?? 0,
    obligations: proof?.obligations.length ?? 0
  }
}

export function compilerStageForEvent(event: TraceEvent): CompilerStageId | null {
  const rawKind = event.raw_kind.toLowerCase()
  const name = event.name.toLowerCase()
  const role = stringValue(event.payload.role).toLowerCase()
  const phase = stringValue(event.raw.phase).toLowerCase()

  if (rawKind === 'runtime_policy' || rawKind === 'supervisor_decision' || rawKind === 'policy_check') return null
  if (rawKind === 'observation' && name === 'feedback/step_result') return null

  const modelIdentity = new Set([role, name])
  const isModelEvent = ['provider_call', 'model_call', 'model_thinking'].includes(rawKind)
  if (isModelEvent && modelIdentity.has('task_compiler')) return 'compiler'
  if (isModelEvent && modelIdentity.has('executor')) return 'executor'
  if (isModelEvent && modelIdentity.has('fine_verifier')) return 'verifier'

  if (rawKind === 'role_call' && name === 'evidence_reviewer') return 'kernel'
  if (rawKind === 'observation' && name === 'role/evidence_reviewer') return 'kernel'
  if (rawKind === 'checkpoint' && phase === 'evidence_reviewed') return 'kernel'

  if (rawKind === 'role_call' && name === 'case_patch_writer') return 'case_store'
  if (rawKind === 'observation' && name === 'role/case_patch_writer') return 'case_store'
  if (rawKind === 'tool_call' && name === 'write_case_patch') return 'case_store'
  if (rawKind === 'observation' && name === 'tool/write_case_patch') return 'case_store'
  if (rawKind === 'final_answer') return 'case_store'
  if (rawKind === 'checkpoint' && (phase === 'patch_ready' || phase === 'patch_written')) return 'case_store'
  return null
}

export function eventsForCompilerStage(events: TraceEvent[], stage: CompilerStageId) {
  return events.filter((event) => compilerStageForEvent(event) === stage)
}

export function initialCompilerTraceSelection(events: TraceEvent[]): CompilerTraceSelection {
  for (const stage of compilerStages) {
    const candidates = eventsForCompilerStage(events, stage.id)
    if (candidates.length === 0) continue
    return {
      stage: stage.id,
      event: candidates.find((event) => event.raw_kind === 'provider_call') ?? candidates[0]
    }
  }
  return { stage: 'compiler', event: null }
}

export function compilerChildRunView(events: TraceEvent[]): CompilerChildRunView | null {
  const ordered = [...events].sort((left, right) => left.case_seq - right.case_seq || left.seq - right.seq)
  const tagged = ordered.filter((event) => stringValue(event.payload.compiler_run_id))
  const compilerRunId = stringValue(tagged.at(-1)?.payload.compiler_run_id)
  if (!compilerRunId) return null

  const runEvents = tagged.filter((event) => stringValue(event.payload.compiler_run_id) === compilerRunId)
  const revision = Math.max(1, ...runEvents.map((event) => numberValue(event.payload.compiler_revision)))
  const revisionEvents = runEvents.filter((event) => Math.max(1, numberValue(event.payload.compiler_revision)) === revision)
  const completed = new Set<string>()
  let totalChecks = 0

  for (const event of revisionEvents) {
    totalChecks = Math.max(totalChecks, numberValue(event.payload.check_count), numberValue(event.payload.target_check_count))
    if (stringValue(event.payload.status) === 'frontier_committed') {
      const checkId = checkIdForEvent(event)
      if (checkId) completed.add(checkId)
    }
    for (const checkId of stringArray(event.payload.completed_check_ids)) completed.add(checkId)
  }

  const latest = revisionEvents.at(-1)!
  const latestStatus = stringValue(latest.payload.status) || latest.status
  const hasFinalProof = revisionEvents.some((event) => numberValue(event.payload.supported_count)
    + numberValue(event.payload.contradicted_count)
    + numberValue(event.payload.not_found_count) > 0)
  const status = ['fatal', 'error'].includes(latestStatus)
    ? 'error'
    : hasFinalProof || (totalChecks > 0 && completed.size >= totalChecks)
      ? 'completed'
      : latestStatus || 'running'

  return {
    compilerRunId,
    revision,
    status,
    completedChecks: completed.size,
    totalChecks,
    activeCheckId: status === 'completed' ? '' : checkIdForEvent(latest),
    events: revisionEvents.slice(-8).reverse().map((event) => ({
      eventId: event.event_id,
      stage: stringValue(event.payload.stage) || stringValue(event.payload.role),
      status: stringValue(event.payload.status) || event.status,
      action: stringValue(event.payload.action) || event.summary,
      publicReason: stringValue(event.payload.public_reason),
      checkId: checkIdForEvent(event),
      diagnosticCode: stringValue(event.payload.diagnostic_code)
        || stringValue(event.payload.hook_code)
        || stringArray(event.payload.diagnostic_codes)[0]
        || ''
    }))
  }
}

function checkIdForEvent(event: TraceEvent) {
  return stringValue(event.payload.check_id) || stringArray(event.payload.focused_check_ids)[0] || ''
}

function stringArray(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && Boolean(item)) : []
}

function numberValue(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function stringValue(value: unknown) {
  return typeof value === 'string' ? value : ''
}
