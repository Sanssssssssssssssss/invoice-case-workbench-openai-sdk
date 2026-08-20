import { describe, expect, it } from 'vitest'
import { formatThinkingElapsed, shouldShowThinking, thinkingLineClass, thinkingRaw, thinkingSummary, thinkingText, thinkingTitle } from './thinking'
import type { LiveStatus } from '@/types'

const status: LiveStatus = {
  runId: 'run_1',
  phase: 'review',
  activeAgent: '证据审核员正在思考',
  activeRole: 'evidence_reviewer',
  currentStep: 1,
  latestSummary: 'streaming',
  latestThinking: '正在核对金额和税率。',
  latestThoughtSummary: '证据审核员正在按 Supervisor 任务核对附件。',
  elapsedMs: 84000,
  latestEventId: 'evt_1',
  isRunning: true,
  thinkingSource: 'public_work_log',
  reasoningChars: 24,
  reasoningChunks: 3,
  updatedAt: '2026-06-01T10:00:00+00:00'
}

describe('thinking helpers', () => {
  it('shows thinking only while the agent is running with text', () => {
    expect(shouldShowThinking(status, true)).toBe(true)
    expect(shouldShowThinking(status, false)).toBe(false)
    expect(shouldShowThinking({ ...status, latestThinking: ' ', latestThoughtSummary: ' ' }, true)).toBe(false)
    expect(
      shouldShowThinking(
        {
          ...status,
          activeRole: 'allow',
          latestThinking: '',
          latestThoughtSummary: 'allowed',
          activeStep: 'PolicyGate 正在检查',
          thinkingSource: ''
        },
        true
      )
    ).toBe(false)
  })

  it('maps line clamp and title text', () => {
    expect(thinkingLineClass(false)).toBe('collapsed')
    expect(thinkingLineClass(true)).toBe('expanded')
    expect(thinkingTitle(status)).toBe('证据审核员 · 工作中 01:24')
    expect(thinkingTitle(status, 125000)).toBe('证据审核员 · 工作中 02:05')
    expect(thinkingText(status)).toBe('正在核对金额和税率。')
    expect(thinkingSummary(status)).toBe(thinkingText(status))
    expect(thinkingRaw(status)).toBe('正在核对金额和税率。')
    expect(formatThinkingElapsed(84000)).toBe('01:24')
  })

  it('does not synthesize thinking from progress summaries', () => {
    const fake = {
      ...status,
      latestThinking: '',
      latestThoughtSummary: 'allowed',
      activeStep: 'tool finished',
      thinkingSource: ''
    }

    expect(thinkingText(fake)).toBe('')
    expect(thinkingSummary(fake)).toBe('')
    expect(thinkingRaw(fake)).toBe('')
  })
})
