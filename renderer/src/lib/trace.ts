import type { TraceEvent } from '@/types'

export function eventTone(event: Pick<TraceEvent, 'kind' | 'status'>) {
  if (event.status === 'error' || event.kind === 'error') return 'danger'
  if (event.kind === 'checkpoint' || event.status === 'saved') return 'success'
  if (event.kind === 'tool') return 'teal'
  if (event.kind === 'thinking') return 'thinking'
  if (event.kind === 'model') return 'blue'
  if (event.kind === 'artifact_summary') return 'violet'
  if (event.kind === 'artifact') return 'violet'
  return 'neutral'
}

export function eventTitle(event: TraceEvent) {
  if (event.raw_kind === 'model_thinking' || event.kind === 'thinking') return `模型思考：${roleLabel(event.name)}`
  if (event.kind === 'artifact_summary') return '附件摘要'
  if (event.kind === 'tool') return `工具：${event.name}`
  if (event.kind === 'planner') return '规划器'
  if (event.kind === 'model') return `模型调用：${roleLabel(event.name)}`
  if (event.kind === 'role') return `Agent：${roleLabel(event.name)}`
  if (event.kind === 'checkpoint') return '检查点'
  if (event.kind === 'artifact') return '产物'
  return event.name || event.raw_kind
}

export function mergeEvents(existing: TraceEvent[] = [], incoming: TraceEvent[]) {
  const byId = new Map<string, TraceEvent>()
  for (const event of existing) byId.set(event.event_id, event)
  for (const event of incoming) byId.set(event.event_id, event)
  return Array.from(byId.values()).sort((a, b) => a.case_seq - b.case_seq || a.seq - b.seq)
}

export function formatDuration(ms: number | null | undefined) {
  if (ms == null) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

export function shortTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return value.slice(11, 16)
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function timelinePositionClass(index: number, total: number) {
  if (total <= 1) return 'single'
  if (index === 0) return 'first'
  if (index === total - 1) return 'last'
  return 'middle'
}

export function roleLabel(value: string) {
  return {
    planner: '规划器',
    materials_advisor: '材料顾问',
    evidence_reviewer: '证据审核员',
    case_patch_writer: '案件更新员',
    report_writer: '报告撰写员',
    summarizer: '附件摘要',
    artifact_summary: '附件摘要',
    session_compactor: '上下文整理器'
  }[value] ?? value
}
