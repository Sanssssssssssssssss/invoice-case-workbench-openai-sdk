import * as Tabs from '@radix-ui/react-tabs'
import { useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Bot, Brain, Check, Code2, Database, Download, ExternalLink, FileText, FolderOpen, GitBranch, Wrench, XCircle } from 'lucide-react'
import type { ArtifactItem, CaseState, EvidenceItem, RunSummary, TraceEvent } from '@/types'
import { compilerStages, compilerStageForEvent, eventsForCompilerStage, initialCompilerTraceSelection, type CompilerStageId } from '@/lib/compilerView'
import { eventTitle, eventTone, formatDuration, shortTime, timelinePositionClass } from '@/lib/trace'
import { modelMetricItems, modelMetricsFromEvent } from '@/lib/modelMetrics'
import { statusLabel } from '@/lib/requirements'
import { runArtifactAction } from '@/lib/artifacts'
import { CompilerPanel } from './CompilerPanel'
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
  onEventSelect: (event: TraceEvent | null) => void
}

const tabs = [
  ['requirements', '需求'],
  ['evidence', '证据'],
  ['compiler', 'Compiler'],
  ['trace', '调试'],
  ['artifacts', '产物']
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
        <Tabs.Content value="compiler" className="tab-content">
          <CompilerPanel caseState={props.caseState} />
        </Tabs.Content>
        <Tabs.Content value="trace" className="tab-content trace-tab">
          <TracePanel {...props} />
        </Tabs.Content>
        <Tabs.Content value="artifacts" className="tab-content">
          <ArtifactsPanel caseId={props.caseState?.case_id ?? ''} artifacts={props.artifacts} />
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
          <p>{evidencePreview(item)}</p>
          <span>{statusLabel(item.type)} · {item.source}</span>
        </div>
      ))}
    </div>
  )
}

function evidencePreview(item: EvidenceItem): string {
  const quotes = item.quoted_text.map((value) => value.trim()).filter(Boolean).slice(0, 2)
  if (quotes.length > 0) return quotes.join(' · ')
  if (item.reviewer_notes.trim()) return item.reviewer_notes.trim()
  const compact = item.content.replace(/\s+/g, ' ').trim()
  if (!compact) return '暂无预览。'
  return compact.length > 240 ? `${compact.slice(0, 237)}...` : compact
}

function TracePanel({ runs, events, selectedRunId, selectedEvent, onRunSelect, onEventSelect }: InspectorProps) {
  const [selectedStage, setSelectedStage] = useState<CompilerStageId>('compiler')
  const initializedRunRef = useRef('')
  const waitingRunRef = useRef('')
  const stageEvents = useMemo(() => eventsForCompilerStage(events, selectedStage), [events, selectedStage])
  const orchestrationEvents = useMemo(() => events.filter((event) => compilerStageForEvent(event) === null), [events])
  const providerCalls = events.filter((item) => item.raw_kind === 'provider_call').length
  const roleCalls = runs.find((run) => run.run_id === selectedRunId)?.model_count ?? 0
  const tools = providerToolCount(events)

  useEffect(() => {
    if (!selectedRunId) {
      initializedRunRef.current = ''
      waitingRunRef.current = ''
      setSelectedStage('compiler')
      onEventSelect(null)
      return
    }
    if (events.length === 0) {
      initializedRunRef.current = ''
      if (waitingRunRef.current !== selectedRunId) {
        waitingRunRef.current = selectedRunId
        setSelectedStage('compiler')
        onEventSelect(null)
      }
      return
    }
    if (initializedRunRef.current === selectedRunId) return
    const selection = initialCompilerTraceSelection(events)
    if (!selection.event) {
      initializedRunRef.current = ''
      if (waitingRunRef.current !== selectedRunId) {
        waitingRunRef.current = selectedRunId
        setSelectedStage('compiler')
        onEventSelect(null)
      }
      return
    }
    initializedRunRef.current = selectedRunId
    waitingRunRef.current = ''
    setSelectedStage(selection.stage)
    onEventSelect(selection.event)
  }, [events, onEventSelect, selectedRunId])

  const selectStage = (stage: CompilerStageId) => {
    setSelectedStage(stage)
    const candidates = eventsForCompilerStage(events, stage)
    const preferred = candidates.find((event) => event.raw_kind === 'provider_call') ?? candidates[0]
    if (preferred) onEventSelect(preferred)
  }

  return (
    <div className="debug-workbench">
      <div className="run-toolbar">
        <label>
          <span>运行</span>
          <select value={selectedRunId} onChange={(event) => onRunSelect(event.target.value)} disabled={runs.length === 0}>
            {runs.length === 0 && <option value="">暂无运行</option>}
            {runs.map((run) => (
              <option key={run.run_id} value={run.run_id}>
                {run.run_id.replace(/^run_/, '').slice(0, 10)} · {statusLabel(run.status)} · {shortTime(run.started_at || run.updated_at)}
              </option>
            ))}
          </select>
        </label>
        <div className="run-facts">
          <span><Bot size={13} /> {providerCalls} API calls</span>
          <span><GitBranch size={13} /> {roleCalls} role calls</span>
          <span><Wrench size={13} /> {tools} tools</span>
          <span className={events.some((event) => event.status === 'error') ? 'has-error' : ''}>
            {events.some((event) => event.status === 'error') ? <XCircle size={13} /> : <Check size={13} />}
            {events.filter((event) => event.status === 'error').length} errors
          </span>
        </div>
      </div>

      <div className="compiler-stage-flow" aria-label="Evidence Compiler stages">
        {compilerStages.map((stage) => {
          const inStage = eventsForCompilerStage(events, stage.id)
          const hasError = inStage.some((event) => event.status === 'error')
          return (
            <button key={stage.id} className={`${selectedStage === stage.id ? 'active' : ''} ${hasError ? 'error' : ''}`} onClick={() => selectStage(stage.id)} title={stage.description}>
              <span>{stage.step}</span>
              <strong>{stage.shortLabel}</strong>
              <small>{inStage.length}</small>
            </button>
          )
        })}
      </div>

      <div className="stage-heading">
        <div>
          <span>阶段 {compilerStages.find((stage) => stage.id === selectedStage)?.step}</span>
          <strong>{compilerStages.find((stage) => stage.id === selectedStage)?.label}</strong>
        </div>
        <p>{compilerStages.find((stage) => stage.id === selectedStage)?.description}</p>
      </div>

      <div className="stage-timeline">
        {stageEvents.length === 0 && <p className="muted-copy padded">这个运行没有记录该阶段。</p>}
        <AnimatePresence initial={false}>
          {stageEvents.map((event, index) => (
            <TraceEventRow
              key={event.event_id}
              event={event}
              linePosition={timelinePositionClass(index, stageEvents.length)}
              active={selectedEvent?.event_id === event.event_id}
              onSelect={() => onEventSelect(event)}
            />
          ))}
        </AnimatePresence>
      </div>
      {orchestrationEvents.length > 0 && (
        <details className="orchestration-events">
          <summary>运行控制事件 <span>{orchestrationEvents.length}</span></summary>
          <div>
            {orchestrationEvents.map((event, index) => (
              <TraceEventRow
                key={event.event_id}
                event={event}
                linePosition={timelinePositionClass(index, orchestrationEvents.length)}
                active={selectedEvent?.event_id === event.event_id}
                onSelect={() => onEventSelect(event)}
              />
            ))}
          </div>
        </details>
      )}
      <EventDetail event={selectedEvent} selectedStage={selectedEvent ? compilerStageForEvent(selectedEvent) : selectedStage} />
    </div>
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
      <span className={`timeline-icon ${tone}`}>{iconFor(event)}</span>
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

function iconFor(event: TraceEvent) {
  if (event.status === 'error') return <XCircle size={16} />
  if (event.raw_kind === 'provider_call') return <Bot size={15} />
  if (event.kind === 'thinking') return <Brain size={15} />
  if (event.kind === 'tool') return <Wrench size={15} />
  if (event.kind === 'model') return <Bot size={15} />
  if (event.kind === 'checkpoint') return <Check size={16} />
  if (event.kind === 'artifact') return <FileText size={15} />
  if (event.kind === 'observation') return <Database size={15} />
  return <GitBranch size={15} />
}

const detailTabs = ['概览', 'Prompt', 'Input', 'Output', 'Tools', 'Raw'] as const

function EventDetail({ event, selectedStage }: { event: TraceEvent | null; selectedStage: CompilerStageId | null }) {
  const [active, setActive] = useState<(typeof detailTabs)[number]>('概览')

  useEffect(() => {
    setActive('概览')
  }, [event?.event_id])

  const content = detailContent(event, active)
  return (
    <div className="event-detail">
      <div className="detail-header">
        <div className="detail-title">
          <span>{compilerStages.find((stage) => stage.id === selectedStage)?.label ?? '运行控制'}</span>
          <strong>{event ? eventTitle(event) : '请选择一个事件'}</strong>
        </div>
        <div className="detail-tabs">
        {detailTabs.map((tab) => (
          <button key={tab} className={active === tab ? 'active' : ''} onClick={() => setActive(tab)}>
            {tab}
          </button>
        ))}
        </div>
        <ModelMetricsStrip event={event} />
      </div>
      <pre className={active === '概览' ? 'detail-overview' : ''}>{content}</pre>
      <div className="detail-footer">
        <Code2 size={15} />
        <span>{event ? `${event.raw_kind} · ${event.event_id}` : '没有选中事件'}</span>
        <span>{active === 'Raw' ? '完整事件' : '选择 Raw 查看原始记录'}</span>
      </div>
    </div>
  )
}

function ModelMetricsStrip({ event }: { event: TraceEvent | null }) {
  const metrics = modelMetricsFromEvent(event)
  if (!metrics) return null
  const items = modelMetricItems(metrics)
  if (items.length === 0) return null
  return (
    <div className="model-metrics-strip" aria-label="model metrics">
      {items.map((item) => (
        <span key={item.label} title={item.title}>
          <strong>{item.label}</strong>
          {item.value}
        </span>
      ))}
    </div>
  )
}

function detailContent(event: TraceEvent | null, active: (typeof detailTabs)[number]) {
  if (!event) return '从阶段时间线中选择一个事件。'
  if (active === '概览') return eventOverview(event)
  if (active === 'Prompt') return textValue(event.payload.system_prompt ?? event.payload.prompt) || '该事件没有模型 Prompt。'
  if (active === 'Input') return displayStructured(event.payload.input ?? event.payload.request ?? event.payload.arguments, '该事件没有输入记录。')
  if (active === 'Output') {
    return displayStructured(
      event.payload.output ?? event.payload.result ?? event.payload.final_answer ?? event.payload.public_reason,
      event.output_preview || '该事件没有输出记录。'
    )
  }
  if (active === 'Tools') {
    const tools = event.payload.tools ?? (event.kind === 'tool' ? { tool: event.name, input: event.payload.input, result: event.payload.result } : undefined)
    return displayStructured(tools, '该事件没有工具调用。')
  }
  return stringify(event.raw)
}

function stringify(value: unknown) {
  return JSON.stringify(value, null, 2)
}

function eventOverview(event: TraceEvent) {
  const usage = recordValue(event.payload.usage)
  const lines = [
    event.summary || event.raw_kind,
    '',
    `状态       ${statusLabel(event.status)}`,
    `时间       ${event.ts || '-'}`,
    `事件类型   ${event.raw_kind}`,
    `阶段       ${compilerStages.find((stage) => stage.id === compilerStageForEvent(event))?.label ?? '运行控制'}`
  ]
  const model = textValue(event.payload.model)
  const role = textValue(event.payload.role)
  const callNumber = textValue(event.payload.call_number)
  if (role) lines.push(`角色       ${role}`)
  if (model) lines.push(`模型       ${model}`)
  if (callNumber) lines.push(`调用       #${callNumber}`)
  if (usage) {
    lines.push(`Tokens     ${numberValue(usage.prompt_tokens)} in + ${numberValue(usage.completion_tokens)} out = ${numberValue(usage.total_tokens)}`)
  }
  if (event.parent_event_id) lines.push(`父事件     ${event.parent_event_id}`)
  return lines.join('\n')
}

function displayStructured(value: unknown, empty: string) {
  if (value === undefined || value === null || value === '') return empty
  if (typeof value === 'string') {
    const parsed = parseEmbeddedJson(value)
    return parsed === value ? value : stringify(parsed)
  }
  return stringify(normalizeEmbeddedJson(value))
}

function normalizeEmbeddedJson(value: unknown): unknown {
  if (typeof value === 'string') return parseEmbeddedJson(value)
  if (Array.isArray(value)) return value.map(normalizeEmbeddedJson)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, normalizeEmbeddedJson(child)]))
  }
  return value
}

function parseEmbeddedJson(value: string): unknown {
  const trimmed = value.trim()
  if (!(trimmed.startsWith('{') || trimmed.startsWith('['))) return value
  try {
    return normalizeEmbeddedJson(JSON.parse(trimmed))
  } catch {
    return value
  }
}

function providerToolCount(events: TraceEvent[]) {
  return events.reduce((count, event) => count + (Array.isArray(event.payload.tools) ? event.payload.tools.length : event.kind === 'tool' ? 1 : 0), 0)
}

function textValue(value: unknown) {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

function numberValue(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
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
