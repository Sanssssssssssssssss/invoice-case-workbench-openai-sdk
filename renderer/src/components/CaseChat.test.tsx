// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it } from 'vitest'
import type { PublicWorkTimeline } from '@/lib/workTimeline'
import { WorkTimelineBubble } from './CaseChat'

const timeline: PublicWorkTimeline = {
  runId: 'run_1',
  startedAt: '2026-08-27T00:00:00.000Z',
  items: [{
    id: 'phase:run_1:1:step-0:planner:1',
    runId: 'run_1',
    seq: 1,
    ts: '2026-08-27T00:00:01.000Z',
    actor: '规划器',
    title: '正在规划',
    publicReason: '',
    status: 'running',
    checkId: '',
    diagnosticCode: '',
    tool: ''
  }]
}

let container: HTMLDivElement | null = null
let root: ReturnType<typeof createRoot> | null = null

afterEach(() => {
  if (root) act(() => root?.unmount())
  container?.remove()
  root = null
  container = null
})

describe('WorkTimelineBubble', () => {
  it('opens while running, folds on terminal state, and remains manually expandable', () => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)

    act(() => root?.render(<WorkTimelineBubble timeline={timeline} running startedAt={timeline.startedAt} />))
    const details = container.querySelector<HTMLDetailsElement>('.work-timeline-group')!
    expect(details.open).toBe(true)

    act(() => root?.render(<WorkTimelineBubble timeline={timeline} running={false} startedAt={timeline.startedAt} />))
    expect(details.open).toBe(false)

    act(() => {
      details.open = true
      details.dispatchEvent(new Event('toggle', { bubbles: true }))
    })
    expect(details.open).toBe(true)
    expect(details.textContent).toContain('Agent · 本轮过程')
  })
})
