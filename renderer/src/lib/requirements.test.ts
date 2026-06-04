import { describe, expect, it } from 'vitest'
import { requirementProgress } from './requirements'
import type { Requirement } from '@/types'

const req = (status: Requirement['status']): Requirement => ({
  id: status,
  label: status,
  status,
  evidence_ids: [],
  kind: 'field',
  required: true,
  guidance: ''
})

describe('requirementProgress', () => {
  it('scores weighted material readiness', () => {
    const progress = requirementProgress([req('satisfied'), req('submitted'), req('weak'), req('conflict'), req('missing')])

    expect(progress.percent).toBe(44)
    expect(progress.ready).toBe(1)
    expect(progress.total).toBe(5)
  })
})
