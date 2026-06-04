import type { Requirement, RequirementStatus } from '@/types'

export const requirementWeights: Record<RequirementStatus, number> = {
  satisfied: 1,
  accepted: 1,
  submitted: 0.6,
  weak: 0.35,
  conflict: 0.25,
  rejected: 0,
  missing: 0
}

export const readyStatuses = new Set<RequirementStatus>(['accepted', 'satisfied'])

export function requirementProgress(requirements: Requirement[]) {
  const required = requirements.filter((item) => item.required !== false)
  if (required.length === 0) return { percent: 0, ready: 0, total: 0 }
  const weighted = required.reduce((sum, item) => sum + (requirementWeights[item.status] ?? 0), 0)
  const ready = required.filter((item) => readyStatuses.has(item.status)).length
  return {
    percent: Math.round((weighted / required.length) * 100),
    ready,
    total: required.length
  }
}

export function requirementSegments(requirements: Requirement[]) {
  const required = requirements.filter((item) => item.required !== false)
  const total = Math.max(required.length, 1)
  const counts = required.reduce<Record<RequirementStatus, number>>(
    (acc, item) => {
      acc[item.status] = (acc[item.status] ?? 0) + 1
      return acc
    },
    { satisfied: 0, accepted: 0, submitted: 0, weak: 0, conflict: 0, rejected: 0, missing: 0 }
  )
  return [
    { key: 'satisfied', value: (counts.satisfied + counts.accepted) / total, color: '#169b61' },
    { key: 'submitted', value: counts.submitted / total, color: '#0f948f' },
    { key: 'weak', value: counts.weak / total, color: '#d18b00' },
    { key: 'conflict', value: counts.conflict / total, color: '#d96b00' },
    { key: 'missing', value: (counts.missing + counts.rejected) / total, color: '#d92d20' }
  ].filter((item) => item.value > 0)
}

export function statusLabel(value: string) {
  const labels: Record<string, string> = {
    satisfied: '已满足',
    accepted: '已接受',
    submitted: '已提交',
    weak: '证据较弱',
    conflict: '有冲突',
    rejected: '已拒绝',
    missing: '缺失',
    ready_for_report: '可生成报告',
    collecting_materials: '收集中',
    completed: '已完成',
    running: '运行中',
    failed: '失败',
    error: '错误',
    new: '新案件',
    ok: '正常',
    saved: '已保存'
  }
  if (labels[value]) return labels[value]
  return value
    .replace(/[-_]/g, ' ')
    .split(' ')
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(' ')
}
