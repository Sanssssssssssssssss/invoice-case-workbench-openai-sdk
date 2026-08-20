import { describe, expect, it } from 'vitest'
import { createOptimisticSystemMessage, createOptimisticUserMessage, mergeConversationWithOptimistic, plainChatText } from './chat'
import type { ConversationItem } from '@/types'

describe('chat optimistic messages', () => {
  it('creates an optimistic user echo with attachment metadata', () => {
    const item = createOptimisticUserMessage({
      content: 'check invoice',
      attachments: [
        { name: 'invoice.pdf', path: '', content_type: 'application/pdf' },
        { name: 'po.pdf', path: '', content_type: 'application/pdf' }
      ],
      id: 'client_test',
      now: '2026-06-02T12:00:00.000Z'
    })

    expect(item).toMatchObject({
      ts: '2026-06-02T12:00:00.000Z',
      role: 'user',
      content: 'check invoice',
      attachments: [
        { name: 'invoice.pdf', path: '', content_type: 'application/pdf' },
        { name: 'po.pdf', path: '', content_type: 'application/pdf' }
      ],
      metadata: {
        optimistic: true,
        client_id: 'client_test',
        attachment_count: 2
      }
    })
  })

  it('shows optimistic messages while the persisted conversation has not caught up', () => {
    const persisted: ConversationItem[] = [
      { ts: '2026-06-02T11:58:00.000Z', role: 'assistant', content: 'ready', attachments: [], metadata: {} }
    ]
    const optimistic = [
      createOptimisticUserMessage({
        content: 'new question',
        attachments: [],
        id: 'client_test',
        now: '2026-06-02T12:00:00.000Z'
      })
    ]

    expect(mergeConversationWithOptimistic(persisted, optimistic).map((item) => item.content)).toEqual(['ready', 'new question'])
  })

  it('does not duplicate an optimistic message after the backend persists the same user content', () => {
    const optimistic = createOptimisticUserMessage({
      content: 'new question',
      attachments: [],
      id: 'client_test',
      now: '2026-06-02T12:00:00.000Z'
    })
    const persisted: ConversationItem[] = [
      { ts: '2026-06-02T12:00:02.000Z', role: 'user', content: 'new question', attachments: [], metadata: {} },
      { ts: '2026-06-02T12:00:10.000Z', role: 'assistant', content: 'answer', attachments: [], metadata: {} }
    ]

    expect(mergeConversationWithOptimistic(persisted, [optimistic]).map((item) => item.content)).toEqual(['new question', 'answer'])
  })

  it('keeps the same message when it carries a different attachment', () => {
    const persisted: ConversationItem[] = [
      {
        ts: '2026-06-02T12:00:00.000Z',
        role: 'user',
        content: 'review attachments',
        attachments: [{ name: 'invoice-a.pdf', path: 'attachments/invoice-a.pdf', content_type: 'application/pdf' }],
        metadata: {}
      }
    ]
    const optimistic = createOptimisticUserMessage({
      content: 'review attachments',
      attachments: [{ name: 'invoice-b.pdf', path: '', content_type: 'application/pdf' }]
    })

    expect(mergeConversationWithOptimistic(persisted, [optimistic])).toHaveLength(2)
  })

  it('deduplicates a persisted attachment even after the backend adds its path', () => {
    const persisted: ConversationItem[] = [
      {
        ts: '2026-06-02T12:00:02.000Z',
        role: 'user',
        content: 'review attachments',
        attachments: [{ name: 'invoice.pdf', path: 'attachments/invoice.pdf', content_type: 'application/pdf' }],
        metadata: {}
      }
    ]
    const optimistic = createOptimisticUserMessage({
      content: 'review attachments',
      attachments: [{ name: 'invoice.pdf', path: '', content_type: 'application/pdf' }]
    })

    expect(mergeConversationWithOptimistic(persisted, [optimistic])).toHaveLength(1)
  })

  it('creates a local system error without throwing away the user echo', () => {
    const item = createOptimisticSystemMessage('send failed', '2026-06-02T12:01:00.000Z')

    expect(item.role).toBe('system')
    expect(item.metadata).toMatchObject({ optimistic: true, error: true })
  })

  it('keeps line breaks while removing raw Markdown decoration', () => {
    expect(plainChatText('## 结论\n\n**通过**\n- 第一项\n- 第二项')).toBe('结论\n\n通过\n• 第一项\n• 第二项')
  })
})
