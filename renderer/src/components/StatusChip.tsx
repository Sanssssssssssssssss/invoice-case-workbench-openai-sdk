import { statusLabel } from '@/lib/requirements'

const tones: Record<string, string> = {
  satisfied: 'chip-success',
  accepted: 'chip-success',
  ready_for_report: 'chip-success',
  completed: 'chip-success',
  collecting_materials: 'chip-teal',
  submitted: 'chip-teal',
  running: 'chip-teal',
  weak: 'chip-warning',
  missing: 'chip-warning',
  new: 'chip-warning',
  conflict: 'chip-danger',
  rejected: 'chip-danger',
  failed: 'chip-danger',
  error: 'chip-danger'
}

export function StatusChip({ status, compact = false }: { status: string; compact?: boolean }) {
  return <span className={`status-chip ${tones[status] ?? 'chip-neutral'} ${compact ? 'chip-compact' : ''}`}>{statusLabel(status)}</span>
}
