import * as Tabs from '@radix-ui/react-tabs'
import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Bot, Brain, Check, Circle, Code2, Database, Download, ExternalLink, FileText, FolderOpen, GitBranch, Wrench, XCircle } from 'lucide-react'
import type { ArtifactItem, CaseState, EvidenceItem, RunSummary, TraceEvent } from '@/types'
import { eventTitle, eventTone, formatDuration, shortTime, timelinePositionClass } from '@/lib/trace'
import { statusLabel } from '@/lib/requirements'
import { runArtifactAction } from '@/lib/artifacts'
import { StatusChip } from './StatusChip'

interface InspectorProps {
  caseState?: CaseState
  evidence: EvidenceItem[]
  artifacts: ArtifactItem[]
  runs: RunSummary[]
  events: TraceEvent[]
  selectedRunId: string
  selectedEvent: TraceEvent | null
  tab: string
  onTabChange: (tab: string) => void
  onRunSelect: (runId: string) => void
  onEventSelect: (event: TraceEvent) => void
}

const tabs = [
  ['requirements', '需求'],
  ['evidence', '证据'],
  ['trace', 'Trace'],
  ['artifacts', '产物'],
  ['prompts', '提示词']
] as const

export function Inspector(props: InspectorProps) {
  return (
    <aside className="inspector">
      <Tabs.Root value={props.tab} onValueChange={props.onTabChange} className="inspector-tabs">
        <Tabs.List className="tab-list">
          {tabs.map(([tab, label]) => (
            <Tabs.Trigger key={tab} value={tab} className="tab-trigger">
              {label}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        <Tabs.Content value="requirements" className="tab-content">
          <RequirementsPanel caseState={props.caseState} />
        </Tabs.Content>
        <Tabs.Content value="evidence" className="tab-content">
          <EvidencePanel evidence={props.evidence} />
        </Tabs.Content>
        <Tabs.Content value="trace" className="tab-content trace-tab">
          <TracePanel {...props} />
        </Tabs.Content>
        <Tabs.Content value="artifacts" className="tab-content">
          <ArtifactsPanel caseId={props.caseState?.case_id ?? ''} artifacts={props.artifacts} />
        </Tabs.Content>
        <Tabs.Content value="prompts" className="tab-content">
          <PromptsPanel caseState={props.caseState} />
        </Tabs.Content>
      </Tabs.Root>
    </aside>
  )
}

function RequirementsPanel({ caseState }: { caseState?: CaseState }) {
  const requirements = caseState?.requirements ?? []
  return (
    <div className="plain-list">
      {requirements.length === 0 && <p className="muted-copy">还没有跟踪需求。</p>}
      {requirements.map((item) => (
        <div className="requirement-row" key={item.id}>
          <span className={`small-dot ${item.status}`} />
          <div>
            <strong>{item.label || item.id}</strong>
            <span>{item.guidance || statusLabel(item.kind)}</span>
          </div>
          <StatusChip status={item.status} compact />
        </div>
      ))}
    </div>
  )
}

function EvidencePanel({ evidence }: { evidence: EvidenceItem[] }) {
  return (
    <div className="plain-list">
      {evidence.length === 0 && <p className="muted-copy">还没有审核证据。</p>}
      {evidence.map((item) => (
        <div className="evidence-row" key={item.id}>
          <div className="row-title">
            <strong>{item.summary || item.id}</strong>
            <StatusChip status={item.credibility} compact />
          </div>
          <p>{item.content || item.reviewer_notes || '暂无预览。'}</p>
          <span>{statusLabel(item.type)} · {item.source}</span>
        </div>
      ))}
    </div>
  )
}

function TracePanel({ runs, events, selectedRunId, selectedEvent, onRunSelect, onEventSelect }: InspectorProps) {
  const counts = {
    tool: events.filter((item) => item.kind === 'tool').length,
    model: events.filter((item) => item.kind === 'model').length,
    thinking: events.filter((item) => item.kind === 'thinking').length,
    checkpoint: events.filter((item) => item.kind === 'checkpoint').length,
    error: events.filter((item) => item.status === 'error').length
  }
  return (
    <>
      <div className="trace-main">
        <div className="run-list">
          <div className="trace-heading">运行列表</div>
          {runs.length === 0 && <p className="muted-copy padded">还没有运行记录</p>}
          {runs.map((run) => (
            <motion.button
              key={run.run_id}
              className={`run-card ${run.run_id === selectedRunId ? 'active' : ''}`}
              onClick={() => onRunSelect(run.run_id)}
              whileHover={{ y: -1 }}
            >
              <span className="run-title">Run&nbsp; {run.run_id.replace(/^run_/, '').slice(0, 8)}</span>
              <span className={`run-status ${run.status}`}>
                {run.status === 'completed' ? <Check size={13} /> : <Circle size={12} />}
                {statusLabel(run.status)}
              </span>
              <time>{shortTime(run.started_at || run.updated_at)}</time>
            </motion.button>
          ))}
        </div>
        <div className="timeline-panel">
          <div className="trace-heading">时间线</div>
          <div className="trace-counts">
            {events.length} 个事件 · {counts.thinking} 段思考 · {counts.model} 次模型 · {counts.tool} 次工具 · {counts.checkpoint} 个检查点 · {counts.error} 个错误
          </div>
          <div className="timeline">
            {events.length === 0 && <p className="muted-copy padded">Trace 事件会显示在这里。</p>}
            <AnimatePresence initial={false}>
              {events.map((event, index) => (
                <TraceEventRow
                  key={event.event_id}
                  event={event}
                  linePosition={timelinePositionClass(index, events.length)}
                  active={selectedEvent?.event_id === event.event_id}
                  onSelect={() => onEventSelect(event)}
                />
              ))}
            </AnimatePresence>
          </div>
        </div>
      </div>
      <JsonDetail event={selectedEvent} />
    </>
  )
}

function TraceEventRow({ event, linePosition, active, onSelect }: { event: TraceEvent; linePosition: string; active: boolean; onSelect: () => void }) {
  const tone = eventTone(event)
  return (
    <motion.button
      className={`timeline-event line-${linePosition} ${active ? 'active' : ''}`}
      onClick={onSelect}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
    >
      <span className={`timeline-icon ${tone}`}>{iconFor(event.kind, event.status)}</span>
      <span className="timeline-copy">
        <time>{shortTime(event.ts)}</time>
        <strong>{eventTitle(event)}</strong>
        <span>{event.summary || event.raw_kind}</span>
      </span>
      <span className="timeline-meta">
        {formatDuration(event.duration_ms)}
        {event.token_count ? `${event.token_count} tok` : ''}
      </span>
    </motion.button>
  )
}

function iconFor(kind: TraceEvent['kind'], status: string) {
  if (status === 'error') return <XCircle size={16} />
  if (kind === 'thinking') return <Brain size={15} />
  if (kind === 'tool') return <Wrench size={15} />
  if (kind === 'model') return <Bot size={15} />
  if (kind === 'checkpoint') return <Check size={16} />
  if (kind === 'artifact') return <FileText size={15} />
  if (kind === 'observation') return <Database size={15} />
  return <GitBranch size={15} />
}

function JsonDetail({ event }: { event: TraceEvent | null }) {
  const [active, setActive] = useState('思考')
  const content = detailContent(event, active)
  return (
    <div className="json-detail">
      <div className="detail-tabs">
        {['思考', '输入', '输出', '关联', 'Raw JSON'].map((tab) => (
          <button key={tab} className={active === tab ? 'active' : ''} onClick={() => setActive(tab)}>
            {tab}
          </button>
        ))}
      </div>
      <pre>{content}</pre>
      <div className="detail-footer">
        <Code2 size={15} />
        <span>Ln 1, Col 1</span>
        <button aria-label="下载 JSON"><Download size={15} /></button>
      </div>
    </div>
  )
}

function detailContent(event: TraceEvent | null, active: string) {
  if (!event) return ''
  if (active === '思考') return event.output_preview || event.summary || event.raw_kind
  if (active === '输入') return event.input_preview || stringify(event.payload.input ?? event.payload.request ?? {})
  if (active === '输出') {
    return event.output_preview || stringify(event.payload.output ?? event.payload.result ?? event.payload.final_answer ?? event.payload.reasoning_excerpt ?? {})
  }
  if (active === '关联') {
    return [`event_id: ${event.event_id}`, `run_id: ${event.run_id}`, `case_seq: ${event.case_seq}`, `kind: ${event.kind}`, `parent: ${event.parent_event_id || '-'}`].join('\n')
  }
  return stringify(event.raw)
}

function stringify(value: unknown) {
  return JSON.stringify(value, null, 2)
}

function ArtifactsPanel({ caseId, artifacts }: { caseId: string; artifacts: ArtifactItem[] }) {
  const [error, setError] = useState('')
  const openArtifact = async (item: ArtifactItem) => {
    setError('')
    try {
      await runArtifactAction(caseId, item, 'open')
    } catch (reason) {
      setError(`文件打开失败：${reason instanceof Error ? reason.message : String(reason)}`)
    }
  }
  const showArtifact = async (item: ArtifactItem) => {
    setError('')
    try {
      await runArtifactAction(caseId, item, 'show')
    } catch (reason) {
      setError(`定位文件失败：${reason instanceof Error ? reason.message : String(reason)}`)
    }
  }
  const downloadArtifact = async (item: ArtifactItem) => {
    await runArtifactAction(caseId, item, 'download')
  }

  return (
    <div className="plain-list">
      {artifacts.length === 0 && <p className="muted-copy">还没有写入产物。</p>}
      {error && <p className="artifact-error">{error}</p>}
      {artifacts.map((item) => (
        <div
          role="button"
          tabIndex={0}
          className="artifact-row clickable"
          key={`${item.path}:${item.updated_at}`}
          onClick={() => void openArtifact(item)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault()
              void openArtifact(item)
            }
          }}
        >
          <span className="artifact-copy">
            <strong>{item.name}</strong>
            <span>{item.type} · {formatBytes(item.bytes)} · {item.path}</span>
          </span>
          <span className="artifact-actions" aria-label="文件操作">
            <button
              type="button"
              className="artifact-action"
              aria-label="打开文件"
              onClick={(event) => {
                event.stopPropagation()
                void openArtifact(item)
              }}
            >
              <ExternalLink size={15} />
            </button>
            <button
              type="button"
              className="artifact-action"
              aria-label="在文件夹中显示"
              onClick={(event) => {
                event.stopPropagation()
                void showArtifact(item)
              }}
            >
              <FolderOpen size={15} />
            </button>
            <button
              type="button"
              className="artifact-action"
              aria-label="下载文件"
              onClick={(event) => {
                event.stopPropagation()
                void downloadArtifact(item)
              }}
            >
              <Download size={15} />
            </button>
          </span>
        </div>
      ))}
    </div>
  )
}

function formatBytes(bytes: number) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function PromptsPanel({ caseState }: { caseState?: CaseState }) {
  const sections = [
    ['对话摘要', caseState?.conversation_summary],
    ['回复简报', caseState?.reply_brief],
    ['风险标记', caseState?.risk_flags?.join('\n')]
  ] as const
  return (
    <div className="plain-list">
      {sections.map(([title, body]) => (
        <div className="prompt-block" key={title}>
          <strong>{title}</strong>
          <p>{body || '暂无摘要。'}</p>
        </div>
      ))}
    </div>
  )
}
