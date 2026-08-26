import type { TraceEvent } from '@/types'
import { roleLabel } from './trace'

export type PublicWorkStatus = 'running' | 'completed' | 'warning' | 'error'

export interface PublicWorkItem {
  id: string
  runId: string
  seq: number
  ts: string
  actor: string
  title: string
  publicReason: string
  status: PublicWorkStatus
  checkId: string
  diagnosticCode: string
  tool: string
  optimistic?: boolean
}

export interface PublicWorkTimeline {
  runId: string
  startedAt: string
  items: PublicWorkItem[]
}

const PUBLIC_EVENT_KINDS = new Set([
  'model_started',
  'model_thinking',
  'tool_started',
  'tool_finished',
  'supervisor_decision',
  'supervisor_decision_blocked',
  'policy_check',
  'role_call',
  'tool_call'
])

const MAX_VISIBLE_WORK_ITEMS = 32

export function publicWorkTimeline(events: TraceEvent[], running: boolean): PublicWorkTimeline {
  const ordered = [...events].sort((left, right) => left.case_seq - right.case_seq || left.seq - right.seq)
  const runId = ordered.at(-1)?.run_id || ''
  const selected = runId ? ordered.filter((event) => event.run_id === runId) : []
  const items = new Map<string, PublicWorkItem>()
  const itemOrder: string[] = []
  const openTools = new Map<string, string[]>()
  const toolOccurrences = new Map<string, number>()
  const activeChecks = new Map<string, string>()

  const put = (item: PublicWorkItem) => {
    if (!items.has(item.id)) itemOrder.push(item.id)
    items.set(item.id, item)
  }

  const closeLatestPhase = (actor: string, status: PublicWorkStatus, event: TraceEvent) => {
    for (let index = itemOrder.length - 1; index >= 0; index -= 1) {
      const id = itemOrder[index]
      const item = items.get(id)
      if (!item || !id.startsWith('phase:') || item.status !== 'running' || item.actor !== actor) continue
      items.set(id, { ...item, status, seq: event.seq, ts: event.ts })
      return
    }
  }

  for (const event of selected) {
    if (!PUBLIC_EVENT_KINDS.has(event.raw_kind)) continue
    const payload = event.payload
    const compilerRunId = stringValue(payload.compiler_run_id)
    const revision = numberValue(payload.compiler_revision) || 1
    const explicitCheckId = checkId(payload)
    if (compilerRunId && explicitCheckId) activeChecks.set(compilerRunId, explicitCheckId)
    const currentCheckId = explicitCheckId || (compilerRunId ? activeChecks.get(compilerRunId) || '' : '')
    const stage = stringValue(payload.stage) || stringValue(payload.role) || event.name
    const rawStatus = stringValue(payload.status) || event.status
    const action = stringValue(payload.action)
    const publicReason = stringValue(payload.public_reason)
    const diagnosticCode = firstString(payload.diagnostic_code, payload.hook_code, firstArrayString(payload.diagnostic_codes))
    const actor = roleLabel(stage || event.name || 'Agent')
    const base = {
      runId: event.run_id,
      seq: event.seq,
      ts: event.ts,
      actor,
      publicReason,
      status: publicStatus(rawStatus, event),
      checkId: currentCheckId,
      diagnosticCode,
      tool: ''
    }

    if (event.raw_kind === 'supervisor_decision' || event.raw_kind === 'supervisor_decision_blocked') {
      closeLatestPhase(roleLabel('planner'), publicStatus(rawStatus, event), event)
    } else if (isExplicitTerminalStatus(rawStatus)) {
      closeLatestPhase(actor, publicStatus(rawStatus, event), event)
    }

    if (rawStatus.startsWith('frontier_')) {
      const attempt = numberValue(payload.frontier_attempt) || 1
      const id = `frontier:${compilerRunId || event.run_id}:${revision}:${currentCheckId || 'unknown'}:${attempt}`
      put({
        ...base,
        id,
        actor: 'Proof frontier',
        title: action || event.summary || 'CHECK proof frontier update'
      })
      continue
    }

    if (event.raw_kind === 'tool_started' || event.raw_kind === 'tool_finished') {
      const tool = stringValue(payload.tool) || event.name
      const toolBase = `tool:${compilerRunId || event.run_id}:${revision}:${currentCheckId || 'global'}:${stage}:${tool}`
      if (event.raw_kind === 'tool_started') {
        const occurrence = (toolOccurrences.get(toolBase) || 0) + 1
        toolOccurrences.set(toolBase, occurrence)
        const id = `${toolBase}:${occurrence}`
        openTools.set(toolBase, [...(openTools.get(toolBase) || []), id])
        put({
          ...base,
          id,
          actor,
          title: action || `正在执行 ${tool}`,
          status: 'running',
          tool
        })
        continue
      }
      const queue = openTools.get(toolBase) || []
      const id = queue.shift() || `${toolBase}:finished:${event.event_id}`
      openTools.set(toolBase, queue)
      const existing = items.get(id)
      put({
        ...(existing || base),
        id,
        seq: event.seq,
        ts: event.ts,
        actor,
        title: action || `${tool} 已返回`,
        publicReason,
        status: publicStatus(rawStatus, event),
        diagnosticCode,
        tool
      })
      continue
    }

    if (event.raw_kind === 'model_started' || event.raw_kind === 'model_thinking') {
      const step = numberValue(payload.step_count)
      const attempt = numberValue(payload.frontier_attempt)
      const id = `phase:${compilerRunId || event.run_id}:${revision}:${currentCheckId || `step-${step || 0}`}:${stage}:${attempt || 1}`
      const existing = items.get(id)
      put({
        ...(existing || base),
        id,
        seq: event.seq,
        ts: event.ts,
        actor,
        title: action || event.summary || `${actor}正在工作`,
        publicReason,
        status: publicStatus(rawStatus, event),
        diagnosticCode
      })
      continue
    }

    const target = stringValue(payload.target)
    const decisionAction = stringValue(payload.action)
    const title = decisionAction
      ? `Supervisor：${decisionAction}${target ? ` → ${target}` : ''}`
      : event.summary || event.name
    put({
      ...base,
      id: `event:${event.event_id}`,
      title,
      publicReason: publicReason || event.summary,
      status: publicStatus(rawStatus, event),
      tool: event.raw_kind === 'tool_call' ? event.name : ''
    })
  }

  let projected = itemOrder.map((id) => items.get(id)!).filter(Boolean)
  if (!running) {
    projected = projected.map((item) => {
      if (item.status !== 'running') return item
      const phaseHandedOff = item.id.startsWith('phase:') && projected.some((candidate) => candidate.seq > item.seq)
      return { ...item, status: phaseHandedOff ? 'completed' as const : 'warning' as const }
    })
  }
  if (running && projected.length === 0) {
    projected = [{
      id: 'agent-turn-shell',
      runId: '',
      seq: 0,
      ts: '',
      actor: 'Agent',
      title: '运行已接受，正在准备上下文',
      publicReason: '真实公开事件到达后会在此处继续更新。',
      status: 'running',
      checkId: '',
      diagnosticCode: '',
      tool: '',
      optimistic: true
    }]
  }
  if (projected.length > MAX_VISIBLE_WORK_ITEMS) {
    const runningItems = projected.filter((item) => item.status === 'running').slice(-MAX_VISIBLE_WORK_ITEMS)
    const terminalBudget = Math.max(0, MAX_VISIBLE_WORK_ITEMS - runningItems.length)
    const terminalItems = terminalBudget
      ? projected.filter((item) => item.status !== 'running').slice(-terminalBudget)
      : []
    projected = [...terminalItems, ...runningItems].sort((left, right) => left.seq - right.seq)
  }

  return {
    runId,
    startedAt: selected[0]?.ts || '',
    items: projected
  }
}

function publicStatus(status: string, event: TraceEvent): PublicWorkStatus {
  const normalized = status.toLowerCase()
  const policyCheck = recordValue(event.payload.policy_check)
  if (event.raw_kind === 'supervisor_decision_blocked' || policyCheck?.allowed === false) return 'warning'
  if (event.kind === 'error' || ['error', 'fatal', 'failed', 'frontier_rolled_back'].includes(normalized)) return 'error'
  if (['rejected', 'blocked', 'warning', 'boundary_violation', 'frontier_rejected'].includes(normalized)) return 'warning'
  if (['started', 'running', 'streaming', 'frontier_started', 'partial'].includes(normalized)) return 'running'
  return 'completed'
}

function isExplicitTerminalStatus(status: string) {
  const normalized = status.toLowerCase()
  return ['completed', 'succeeded', 'success', 'terminal', 'failed', 'error', 'fatal', 'cancelled'].includes(normalized)
    || (normalized.startsWith('frontier_') && normalized !== 'frontier_started')
}

function checkId(payload: Record<string, unknown>) {
  return stringValue(payload.check_id) || firstArrayString(payload.focused_check_ids)
}

function firstArrayString(value: unknown) {
  return Array.isArray(value) ? value.find((item): item is string => typeof item === 'string' && Boolean(item)) || '' : ''
}

function firstString(...values: unknown[]) {
  return values.find((value): value is string => typeof value === 'string' && Boolean(value)) || ''
}

function stringValue(value: unknown) {
  return typeof value === 'string' ? value : ''
}

function numberValue(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function recordValue(value: unknown) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}
