import type { ConversationItem } from '@/types'

interface OptimisticUserMessageOptions {
  content: string
  fileCount: number
  id?: string
  now?: string
}

export function createOptimisticUserMessage({
  content,
  fileCount,
  id = `client_${Date.now()}`,
  now = new Date().toISOString()
}: OptimisticUserMessageOptions): ConversationItem {
  return {
    ts: now,
    role: 'user',
    content,
    metadata: {
      optimistic: true,
      client_id: id,
      attachment_count: fileCount
    }
  }
}

export function createOptimisticSystemMessage(content: string, now = new Date().toISOString()): ConversationItem {
  return {
    ts: now,
    role: 'system',
    content,
    metadata: {
      optimistic: true,
      error: true
    }
  }
}

export function mergeConversationWithOptimistic(persisted: ConversationItem[] = [], optimistic: ConversationItem[] = []) {
  if (!optimistic.length) return persisted
  const persistedContentKeys = new Set(persisted.map((item) => contentKey(item)))
  return [...persisted, ...optimistic.filter((item) => !persistedContentKeys.has(contentKey(item)))]
}

function contentKey(item: ConversationItem) {
  return `${item.role}:${item.content}`
}
