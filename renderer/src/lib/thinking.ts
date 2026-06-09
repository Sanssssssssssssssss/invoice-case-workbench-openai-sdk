import type { LiveStatus } from '@/types'

export function shouldShowThinking(status: LiveStatus | null | undefined, agentRunning: boolean) {
  return Boolean(agentRunning && status?.thinkingSource === 'reasoning_content' && status.latestThinking?.trim())
}

export function thinkingLineClass(expanded: boolean) {
  return expanded ? 'expanded' : 'collapsed'
}

export function thinkingTitle(status: LiveStatus | null | undefined, elapsedMs?: number) {
  const label = roleLabel(status?.activeRole || '') || status?.activeAgent || '模型'
  const elapsed = formatThinkingElapsed(elapsedMs ?? status?.elapsedMs)
  return `${label} · 思考中${elapsed ? ` ${elapsed}` : ''}`
}

export function thinkingText(status: LiveStatus | null | undefined) {
  return thinkingSummary(status) || thinkingRaw(status)
}

export function thinkingSummary(status: LiveStatus | null | undefined) {
  if (status?.thinkingSource === 'reasoning_content' && status.latestThinking?.trim()) {
    return status.latestThinking.trim()
  }
  return ''
}

export function thinkingRaw(status: LiveStatus | null | undefined) {
  return status?.thinkingSource === 'reasoning_content' ? status.latestThinking?.trim() || '' : ''
}

export function formatThinkingElapsed(ms: number | null | undefined) {
  if (ms == null || ms < 0) return ''
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function roleLabel(role: string) {
  return {
    planner: '规划器',
    materials_advisor: '材料顾问',
    evidence_reviewer: '证据审核员',
    case_patch_writer: '案卷更新员',
    report_writer: '报告撰写员',
    artifact_summary: '附件摘要',
    summarizer: '附件摘要',
    session_compactor: '上下文整理器'
  }[role] ?? ''
}
