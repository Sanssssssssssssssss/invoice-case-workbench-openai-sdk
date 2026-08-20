import type { ConversationAttachment, ConversationItem } from '@/types'

interface OptimisticUserMessageOptions {
  content: string
  attachments: ConversationAttachment[]
  id?: string
  now?: string
}

export function createOptimisticUserMessage({
  content,
  attachments,
  id = `client_${Date.now()}`,
  now = new Date().toISOString()
}: OptimisticUserMessageOptions): ConversationItem {
  return {
    ts: now,
    role: 'user',
    content,
    attachments,
    metadata: {
      optimistic: true,
      client_id: id,
      attachment_count: attachments.length
    }
  }
}

export function createOptimisticSystemMessage(content: string, now = new Date().toISOString()): ConversationItem {
  return {
    ts: now,
    role: 'system',
    content,
    attachments: [],
    metadata: {
      optimistic: true,
      error: true
    }
  }
}

export function createOptimisticAssistantMessage(content: string, id = `assistant_${Date.now()}`, now = new Date().toISOString()): ConversationItem {
  return {
    ts: now,
    role: 'assistant',
    content,
    attachments: [],
    metadata: {
      optimistic: true,
      client_id: id,
      streaming: true
    }
  }
}

export function mergeConversationWithOptimistic(persisted: ConversationItem[] = [], optimistic: ConversationItem[] = []) {
  if (!optimistic.length) return persisted
  const persistedContentKeys = new Set(persisted.map((item) => contentKey(item)))
  return [...persisted, ...optimistic.filter((item) => !persistedContentKeys.has(contentKey(item)))]
}

export function plainChatText(content: string) {
  return content
    .replace(/^\s*```.*$/gm, '')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '• ')
    .replace(/^\s*>\s?/gm, '')
    .replace(/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/gm, '')
    .replace(/^\s*\|(.+)\|\s*$/gm, (_line, cells: string) => cells.split('|').map((cell) => cell.trim()).filter(Boolean).join(' · '))
    .replace(/\[([^\]]+)]\([^)]+\)/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/(^|\s)\*([^*\n]+)\*(?=\s|$)/g, '$1$2')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

function contentKey(item: ConversationItem) {
  const attachments = item.attachments
    .map((attachment) => `${attachment.name}:${attachment.content_type}`)
    .sort()
    .join('|')
  return `${item.role}:${item.content}:${attachments}`
}
