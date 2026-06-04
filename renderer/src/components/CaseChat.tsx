import { useEffect, useMemo, useRef, useState, type DragEvent } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Bot, CheckCircle2, ChevronDown, ChevronUp, CloudUpload, FileText, Paperclip, Send, Settings2, UserRound, X } from 'lucide-react'
import { Virtuoso } from 'react-virtuoso'
import type { CaseState, ConversationItem, LiveStatus, TraceEvent } from '@/types'
import { requirementProgress, statusLabel } from '@/lib/requirements'
import { shortTime } from '@/lib/trace'
import { shouldShowThinking, thinkingLineClass, thinkingRaw, thinkingSummary, thinkingTitle } from '@/lib/thinking'
import { dataTransferHasFiles, mergeFiles } from '@/lib/files'
import { RequirementRing } from './RequirementRing'

interface CaseChatProps {
  caseState?: CaseState
  messages: ConversationItem[]
  liveEvents: TraceEvent[]
  liveStatus: LiveStatus | null
  running: boolean
  agentRunning: boolean
  onOpenRequirements: () => void
  onSend: (message: string, files: File[]) => void
}

export function CaseChat({ caseState, messages, liveEvents, liveStatus, running, agentRunning, onOpenRequirements, onSend }: CaseChatProps) {
  const [message, setMessage] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [dragActive, setDragActive] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const dragDepth = useRef(0)
  const requirements = caseState?.requirements ?? []
  const progress = requirementProgress(requirements)
  const virtuosoComponents = useMemo(() => ({ Footer: MessageFooter }), [])
  const rows = useMemo<ChatRow[]>(() => {
    const base: ChatRow[] = messages.map((item) => ({ type: 'message', id: `${item.ts}:${item.role}:${item.content}`, item }))
    if (shouldShowThinking(liveStatus, agentRunning)) {
      base.push({ type: 'thinking' as const, id: 'thinking:live', status: liveStatus })
    }
    return base
  }, [agentRunning, liveStatus, messages])

  const submit = () => {
    if ((!message.trim() && files.length === 0) || running) return
    onSend(message.trim(), files)
    setMessage('')
    setFiles([])
    inputRef.current?.focus()
  }

  const addFiles = (incoming: FileList | File[] | null) => {
    if (!incoming) return
    setFiles((current) => mergeFiles(current, incoming))
  }

  const handleDragEnter = (event: DragEvent<HTMLElement>) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return
    event.preventDefault()
    dragDepth.current += 1
    setDragActive(true)
  }

  const handleDragOver = (event: DragEvent<HTMLElement>) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
  }

  const handleDragLeave = (event: DragEvent<HTMLElement>) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return
    event.preventDefault()
    dragDepth.current = Math.max(0, dragDepth.current - 1)
    if (dragDepth.current === 0) setDragActive(false)
  }

  const handleDrop = (event: DragEvent<HTMLElement>) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return
    event.preventDefault()
    dragDepth.current = 0
    setDragActive(false)
    addFiles(event.dataTransfer.files)
    inputRef.current?.focus()
  }

  const lastAccepted = [...liveEvents].reverse().find((event) => event.kind === 'artifact' || event.status === 'saved')

  return (
    <main className="chat-workspace">
      <header className="chat-header">
        <div className="case-title-block">
          <h2>{caseState?.case_id ?? 'Agent Case Cockpit'}</h2>
          <p>{caseState?.summary || '在一个工作台里复核发票、证据链和 Agent 决策过程。'}</p>
        </div>
        <div className="case-health">
            <RequirementRing requirements={requirements} onClick={onOpenRequirements} />
          <div className="status-copy">
            <strong>{statusLabel(caseState?.status ?? 'new')}</strong>
            <span>{progress.ready}/{progress.total} 已就绪</span>
          </div>
          <div className={`agent-card ${agentRunning ? 'running' : ''}`}>
            <span className="running-dot" />
            <strong>{agentRunning ? 'Agent 运行中' : 'Agent 就绪'}</strong>
            <span>{agentRunning ? liveStatus?.latestSummary || liveEvents.at(-1)?.summary || '规划器正在判断下一步' : '等待指令'}</span>
          </div>
        </div>
      </header>

      <section className="message-surface">
        <Virtuoso
          data={rows}
          followOutput="smooth"
          computeItemKey={(_index, row) => row.id}
          components={virtuosoComponents}
          itemContent={(_index, row) => (row.type === 'thinking' ? <ThinkingBubble status={row.status} /> : <MessageItem item={row.item} />)}
        />
        {messages.length === 0 && (
          <div className="empty-chat">
            <Bot size={24} />
            <span>发送问题或添加材料，开始案件运行。</span>
          </div>
        )}
      </section>

      {lastAccepted && (
        <motion.div className="evidence-toast" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
          <CheckCircle2 size={16} />
          <span>{lastAccepted.summary || '证据检查点已保存'}</span>
        </motion.div>
      )}

      <footer
        className={`composer ${dragActive ? 'drag-active' : ''}`}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        <div className="attachment-row">
          <span>附件</span>
          <div className="attachment-chips">
            <AnimatePresence initial={false}>
              {files.map((file) => (
                <motion.button
                  key={`${file.name}:${file.size}:${file.lastModified}`}
                  className="attachment-chip"
                  initial={{ opacity: 0, x: -8 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: -8 }}
                  onClick={() => setFiles((current) => current.filter((item) => item !== file))}
                >
                  <FileText size={14} />
                  <span>{file.name}</span>
                  <X size={13} />
                </motion.button>
              ))}
            </AnimatePresence>
          </div>
          <button className="dashed-add" onClick={() => fileInputRef.current?.click()}>
            <Paperclip size={15} />
            添加
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            onChange={(event) => {
              addFiles(event.currentTarget.files)
              event.currentTarget.value = ''
            }}
          />
        </div>
        <div className="composer-box">
          <textarea
            ref={inputRef}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') submit()
            }}
            placeholder="询问缺失材料、上传发票，或让 Agent 复核当前案件"
          />
          <div className="composer-tools">
            <button onClick={() => fileInputRef.current?.click()} aria-label="Attach files">
              <Paperclip size={19} />
            </button>
            <button onClick={() => fileInputRef.current?.click()} aria-label="Upload files">
              <CloudUpload size={20} />
            </button>
          </div>
          <button className="send-button" disabled={running || (!message.trim() && files.length === 0)} onClick={submit}>
            <Send size={19} />
          </button>
        </div>
      </footer>
    </main>
  )
}

type ChatRow =
  | { type: 'message'; id: string; item: ConversationItem }
  | { type: 'thinking'; id: string; status: LiveStatus | null }

function MessageFooter() {
  return <div className="message-bottom-space" />
}

function MessageItem({ item }: { item: ConversationItem }) {
  const isUser = item.role === 'user'
  const isSystem = item.role === 'system'
  return (
    <motion.article className={`message-item ${isUser ? 'user' : isSystem ? 'system' : 'agent'}`} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
      <div className="message-avatar">{isUser ? <UserRound size={18} /> : isSystem ? <Settings2 size={18} /> : <Bot size={18} />}</div>
      <div className="message-body">
        <div className="message-meta">
          <strong>{isUser ? '你' : isSystem ? '系统' : 'Agent'}</strong>
          <time>{shortTime(item.ts)}</time>
        </div>
        <p>{item.content}</p>
      </div>
    </motion.article>
  )
}

function ThinkingBubble({ status }: { status: LiveStatus | null }) {
  const [expanded, setExpanded] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!status?.isRunning) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [status?.isRunning, status?.runStartedAt])
  const summaryText = thinkingSummary(status)
  const rawText = thinkingRaw(status)
  const hasRaw = Boolean(rawText && rawText !== summaryText)
  const startedAt = status?.runStartedAt ? Date.parse(status.runStartedAt) : NaN
  const liveElapsedMs = Number.isFinite(startedAt) && status?.isRunning ? Math.max(0, now - startedAt) : status?.elapsedMs
  if (!summaryText && !rawText) return null
  return (
    <motion.article
      className="message-item agent thinking-message"
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 6 }}
      transition={{ duration: 0.18 }}
    >
      <div className="message-avatar thinking-avatar">
        <Bot size={18} />
      </div>
      <div className="message-body thinking-body">
        <div className="message-meta">
          <strong>{thinkingTitle(status, liveElapsedMs)}</strong>
          <time>{shortTime(status?.updatedAt || new Date().toISOString())}</time>
        </div>
        <div className="thinking-panel">
          <div className="thinking-pulse">
            <span />
            <span />
            <span />
          </div>
          <p className={thinkingLineClass(expanded)}>{summaryText || rawText}</p>
          {hasRaw && expanded ? <pre className="thinking-raw">{rawText}</pre> : null}
          <button className="thinking-toggle" onClick={() => setExpanded((value) => !value)}>
            {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            {expanded ? '收起' : '展开'}
          </button>
        </div>
      </div>
    </motion.article>
  )
}
