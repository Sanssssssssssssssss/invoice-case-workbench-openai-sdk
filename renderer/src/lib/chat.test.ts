import { describe, expect, it } from 'vitest'
import { createOptimisticSystemMessage, createOptimisticUserMessage, mergeConversationWithOptimistic } from './chat'
import type { ConversationItem } from '@/types'

describe('chat optimistic messages', () => {
  it('creates an optimistic user echo with attachment metadata', () => {
    const item = createOptimisticUserMessage({
      content: 'check invoice',
      fileCount: 2,
      id: 'client_test',
      now: '2026-06-02T12:00:00.000Z'
    })

    expect(item).toMatchObject({
      ts: '2026-06-02T12:00:00.000Z',
      role: 'user',
      content: 'check invoice',
      metadata: {
        optimistic: true,
        client_id: 'client_test',
        attachment_count: 2
      }
    })
  })

  it('shows optimistic messages while the persisted conversation has not caught up', () => {
    const persisted: ConversationItem[] = [
      { ts: '2026-06-02T11:58:00.000Z', role: 'assistant', content: 'ready', metadata: {} }
    ]
    const optimistic = [
      createOptimisticUserMessage({
        content: 'new question',
        fileCount: 0,
        id: 'client_test',
        now: '2026-06-02T12:00:00.000Z'
      })
    ]

    expect(mergeConversationWithOptimistic(persisted, optimistic).map((item) => item.content)).toEqual(['ready', 'new question'])
  })

  it('does not duplicate an optimistic message after the backend persists the same user content', () => {
    const optimistic = createOptimisticUserMessage({
      content: 'new question',
      fileCount: 0,
      id: 'client_test',
      now: '2026-06-02T12:00:00.000Z'
    })
    const persisted: ConversationItem[] = [
      { ts: '2026-06-02T12:00:02.000Z', role: 'user', content: 'new question', metadata: {} },
      { ts: '2026-06-02T12:00:10.000Z', role: 'assistant', content: 'answer', metadata: {} }
    ]

    expect(mergeConversationWithOptimistic(persisted, [optimistic]).map((item) => item.content)).toEqual(['new question', 'answer'])
  })

  it('creates a local system error without throwing away the user echo', () => {
    const item = createOptimisticSystemMessage('send failed', '2026-06-02T12:01:00.000Z')

    expect(item.role).toBe('system')
    expect(item.metadata).toMatchObject({ optimistic: true, error: true })
  })
})
