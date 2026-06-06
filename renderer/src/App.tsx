import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { api, createEventSource } from '@/lib/api'
import { approvalInterruptsFromEvents, approvalInterruptsFromTrace } from '@/lib/approvals'
import { createOptimisticAssistantMessage, createOptimisticSystemMessage, createOptimisticUserMessage, mergeConversationWithOptimistic } from '@/lib/chat'
import { parseAgentRunStreamMessage, parseLiveStatusMessage, parseTraceEventMessage } from '@/lib/eventStream'
import { mergeEvents } from '@/lib/trace'
import type { AgentRunStreamEvent, AgentTurnResponse, ApprovalInterrupt, AttachmentUpload, ConversationItem, LiveStatus, TraceEvent } from '@/types'
import { useUiStore } from '@/store/uiStore'
import { TitleBar } from '@/components/TitleBar'
import { CaseRail } from '@/components/CaseRail'
import { CaseChat } from '@/components/CaseChat'
import { Inspector } from '@/components/Inspector'

export default function App() {
  const queryClient = useQueryClient()
  const [running, setRunning] = useState(false)
  const [liveEvents, setLiveEvents] = useState<TraceEvent[]>([])
  const [liveStatus, setLiveStatus] = useState<LiveStatus | null>(null)
  const [optimisticMessages, setOptimisticMessages] = useState<ConversationItem[]>([])
  const [pendingApprovals, setPendingApprovals] = useState<ApprovalInterrupt[]>([])
  const liveEventsRef = useRef<TraceEvent[]>([])
  const streamSeqByRunRef = useRef<Record<string, number>>({})
  const selectedRunIdRef = useRef<string>('')
  const selectedCaseId = useUiStore((state) => state.selectedCaseId)
  const selectedRunId = useUiStore((state) => state.selectedRunId)
  const selectedEvent = useUiStore((state) => state.selectedEvent)
  const inspectorTab = useUiStore((state) => state.inspectorTab)
  const setSelectedCaseId = useUiStore((state) => state.setSelectedCaseId)
  const setSelectedRunId = useUiStore((state) => state.setSelectedRunId)
  const setSelectedEvent = useUiStore((state) => state.setSelectedEvent)
  const setInspectorTab = useUiStore((state) => state.setInspectorTab)

  useEffect(() => {
    liveEventsRef.current = liveEvents
  }, [liveEvents])

  useEffect(() => {
    selectedRunIdRef.current = selectedRunId
  }, [selectedRunId])

  const casesQuery = useQuery({ queryKey: ['cases'], queryFn: api.listCases })
  const caseQuery = useQuery({
    queryKey: ['case', selectedCaseId],
    queryFn: () => api.getCase(selectedCaseId),
    enabled: Boolean(selectedCaseId)
  })
  const conversationQuery = useQuery({
    queryKey: ['conversation', selectedCaseId],
    queryFn: () => api.getConversation(selectedCaseId),
    enabled: Boolean(selectedCaseId)
  })
  const evidenceQuery = useQuery({
    queryKey: ['evidence', selectedCaseId],
    queryFn: () => api.getEvidence(selectedCaseId),
    enabled: Boolean(selectedCaseId)
  })
  const artifactsQuery = useQuery({
    queryKey: ['artifacts', selectedCaseId],
    queryFn: () => api.getArtifacts(selectedCaseId),
    enabled: Boolean(selectedCaseId)
  })
  const runsQuery = useQuery({
    queryKey: ['runs', selectedCaseId],
    queryFn: () => api.getRuns(selectedCaseId),
    enabled: Boolean(selectedCaseId)
  })
  const liveStatusQuery = useQuery({
    queryKey: ['liveStatus', selectedCaseId],
    queryFn: () => api.getLiveStatus(selectedCaseId),
    enabled: Boolean(selectedCaseId)
  })
  const eventsQuery = useQuery({
    queryKey: ['runEvents', selectedCaseId, selectedRunId],
    queryFn: () => api.getRunEvents(selectedCaseId, selectedRunId),
    enabled: Boolean(selectedCaseId && selectedRunId)
  })
  const agentRunning = running || Boolean(runsQuery.data?.some((run) => run.status === 'running'))

  useEffect(() => {
    if (!selectedCaseId && casesQuery.data?.length) {
      setSelectedCaseId(casesQuery.data[0].case_id)
    }
  }, [casesQuery.data, selectedCaseId, setSelectedCaseId])

  useEffect(() => {
    if (runsQuery.data?.length && (!selectedRunId || !runsQuery.data.some((run) => run.run_id === selectedRunId))) {
      setSelectedRunId(runsQuery.data[0].run_id)
    }
  }, [runsQuery.data, selectedRunId, setSelectedRunId])

  useEffect(() => {
    if (liveStatusQuery.data) {
      setLiveStatus(liveStatusQuery.data)
    }
  }, [liveStatusQuery.data])

  useEffect(() => {
    if (!running || !selectedCaseId) return
    let closed = false
    let source: EventSource | null = null
    const lastCaseSeq = liveEventsRef.current.reduce((max, event) => Math.max(max, event.case_seq), 0)

    createEventSource(`/api/cases/${encodeURIComponent(selectedCaseId)}/events/stream?after_case_seq=${lastCaseSeq}`).then((eventSource) => {
      if (closed) {
        eventSource.close()
        return
      }
      source = eventSource
      source.addEventListener('trace_event', (event) => {
        const traceEvent = parseTraceEventMessage((event as MessageEvent).data)
        setLiveEvents((current) => mergeEvents(current, [traceEvent]))
        if (!selectedRunIdRef.current && traceEvent.run_id) {
          selectedRunIdRef.current = traceEvent.run_id
          setSelectedRunId(traceEvent.run_id)
        }
        if (traceEvent.run_id) {
          queryClient.setQueryData<TraceEvent[]>(['runEvents', selectedCaseId, traceEvent.run_id], (current = []) =>
            mergeEvents(current, [traceEvent])
          )
        }
      })
    })

    return () => {
      closed = true
      source?.close()
    }
  }, [running, selectedCaseId, queryClient, setSelectedRunId])

  useEffect(() => {
    if (!selectedCaseId || !agentRunning) return
    let closed = false
    let source: EventSource | null = null
    const afterCaseSeq = liveEventsRef.current.reduce((max, event) => Math.max(max, event.case_seq), 0)

    createEventSource(`/api/cases/${encodeURIComponent(selectedCaseId)}/live-status/stream?after_case_seq=${afterCaseSeq}`).then((eventSource) => {
      if (closed) {
        eventSource.close()
        return
      }
      source = eventSource
      source.addEventListener('live_status', (event) => {
        setLiveStatus(parseLiveStatusMessage((event as MessageEvent).data))
      })
    })

    return () => {
      closed = true
      source?.close()
    }
  }, [agentRunning, selectedCaseId])

  const visibleEvents = useMemo(() => {
    const base = eventsQuery.data ?? []
    const liveForRun = selectedRunId ? liveEvents.filter((event) => event.run_id === selectedRunId) : liveEvents
    return mergeEvents(base, liveForRun)
  }, [eventsQuery.data, liveEvents, selectedRunId])

  const visibleMessages = useMemo(
    () => mergeConversationWithOptimistic(conversationQuery.data ?? [], optimisticMessages),
    [conversationQuery.data, optimisticMessages]
  )

  useEffect(() => {
    if (!selectedCaseId || pendingApprovals.length > 0) return
    const waitingRun = (runsQuery.data ?? []).find((run) => run.status === 'waiting_approval')
    if (!waitingRun) return
    if (selectedRunId !== waitingRun.run_id) {
      setSelectedRunId(waitingRun.run_id)
      return
    }
    const approvals = approvalInterruptsFromEvents(selectedCaseId, waitingRun.run_id, visibleEvents)
    if (approvals.length) {
      setPendingApprovals(approvals)
    }
  }, [pendingApprovals.length, runsQuery.data, selectedCaseId, selectedRunId, setSelectedRunId, visibleEvents])

  useEffect(() => {
    if (!selectedEvent && visibleEvents.length) {
      setSelectedEvent(visibleEvents[visibleEvents.length - 1])
    }
  }, [selectedEvent, setSelectedEvent, visibleEvents])

  const createCase = useMutation({
    mutationFn: api.createCase,
    onSuccess: (created) => {
      queryClient.setQueryData(['cases'], (current: unknown) => [created, ...((current as typeof casesQuery.data) ?? [])])
      setSelectedCaseId(created.case_id)
      setPendingApprovals([])
    }
  })

  const deleteCase = useMutation({
    mutationFn: api.deleteCase,
    onSuccess: (_result, caseId) => {
      queryClient.setQueryData(['cases'], (current: unknown) => ((current as typeof casesQuery.data) ?? []).filter((item) => item.case_id !== caseId))
      if (selectedCaseId === caseId) {
        const next = (casesQuery.data ?? []).find((item) => item.case_id !== caseId)
        setSelectedCaseId(next?.case_id ?? '')
        setPendingApprovals([])
      }
    }
  })

  const selectCase = (caseId: string) => {
    if (caseId !== selectedCaseId) {
      setOptimisticMessages([])
      setLiveEvents([])
      setLiveStatus(null)
      setPendingApprovals([])
    }
    setSelectedCaseId(caseId)
  }

  const refreshCaseData = async (caseId: string, runId = '') => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['cases'] }),
      queryClient.invalidateQueries({ queryKey: ['case', caseId] }),
      queryClient.invalidateQueries({ queryKey: ['conversation', caseId] }),
      queryClient.invalidateQueries({ queryKey: ['runs', caseId] }),
      queryClient.invalidateQueries({ queryKey: ['liveStatus', caseId] }),
      queryClient.invalidateQueries({ queryKey: ['artifacts', caseId] }),
      queryClient.invalidateQueries({ queryKey: ['evidence', caseId] }),
      runId ? queryClient.invalidateQueries({ queryKey: ['runEvents', caseId, runId] }) : Promise.resolve()
    ])
  }

  const applyResponse = async (response: AgentTurnResponse) => {
    const approvals = approvalInterruptsFromTrace(response.case_id, response.trace)
    setPendingApprovals(approvals)
    const runId = approvals[0]?.run_id || stringValue(response.trace.run_id)
    if (runId) {
      setSelectedRunId(runId)
    }
    if (response.case_id !== selectedCaseId) {
      setSelectedCaseId(response.case_id)
    }
    await refreshCaseData(response.case_id, runId)
  }

  const upsertStreamingAssistant = (runId: string, content: string) => {
    if (!content) return
    setOptimisticMessages((current) => {
      const id = `stream:${runId}`
      const existing = current.findIndex((item) => item.metadata?.client_id === id)
      if (existing >= 0) {
        return current.map((item, index) => (index === existing ? { ...item, content } : item))
      }
      return [...current, createOptimisticAssistantMessage(content, id)]
    })
  }

  const updateFromRunStreamEvent = (event: AgentRunStreamEvent) => {
    streamSeqByRunRef.current[event.run_id] = Math.max(streamSeqByRunRef.current[event.run_id] ?? 0, event.seq)
    if (!selectedRunIdRef.current && event.run_id) {
      selectedRunIdRef.current = event.run_id
      setSelectedRunId(event.run_id)
    }
    setLiveStatus(liveStatusFromRunStream(event))
  }

  const waitForRunStream = async (caseId: string, runId: string, streamUrl: string) => {
    const afterSeq = streamSeqByRunRef.current[runId] ?? 0
    const path = `${streamUrl}${streamUrl.includes('?') ? '&' : '?'}after_seq=${afterSeq}`
    return new Promise<'final' | 'approval'>((resolve, reject) => {
      let source: EventSource | null = null
      let assistantText = ''
      let settled = false
      const finish = (result: 'final' | 'approval') => {
        if (settled) return
        settled = true
        source?.close()
        resolve(result)
      }
      const fail = (error: unknown) => {
        if (settled) return
        settled = true
        source?.close()
        reject(error)
      }
      const handle = async (raw: MessageEvent) => {
        try {
          const event = parseAgentRunStreamMessage(raw.data)
          updateFromRunStreamEvent(event)
          if (event.kind === 'assistant_delta') {
            const delta = stringValue(event.payload.delta)
            if (delta) {
              assistantText += delta
              upsertStreamingAssistant(runId, assistantText)
            }
          } else if (event.kind === 'approval_required') {
            const approvals = approvalsFromRunStream(event, caseId, runId)
            setPendingApprovals(approvals)
            await refreshCaseData(caseId, runId)
            finish('approval')
          } else if (event.kind === 'final') {
            const response = agentTurnResponseFromPayload(event.payload)
            if (!response) throw new Error('Streaming final event did not include a valid response.')
            await applyResponse(response)
            finish('final')
          } else if (event.kind === 'error') {
            fail(new Error(stringValue(event.payload.message) || event.summary || 'Streaming run failed.'))
          }
        } catch (error) {
          fail(error)
        }
      }
      createEventSource(path)
        .then((eventSource) => {
          source = eventSource
          for (const kind of STREAM_EVENT_KINDS) {
            source.addEventListener(kind, (event) => void handle(event as MessageEvent))
          }
          source.onerror = () => {
            if (!settled) fail(new Error('Streaming connection failed.'))
          }
        })
        .catch(fail)
    })
  }

  const sendTurn = async (message: string, files: File[]) => {
    const caseId = selectedCaseId || caseQuery.data?.case_id
    if (!caseId) return
    const userText = message || '请复核已上传的材料。'
    setRunning(true)
    setPendingApprovals([])
    setLiveEvents([])
    setLiveStatus(null)
    setOptimisticMessages((current) => [
      ...current,
      createOptimisticUserMessage({
        content: userText,
        fileCount: files.length
      })
    ])
    let completed = false
    try {
      const uploads = await Promise.all(files.map((file) => api.uploadAttachment(caseId, file)))
      let runStarted = false
      try {
        const run = await api.startRun(caseId, userText, uploads)
        runStarted = true
        setSelectedRunId(run.run_id)
        selectedRunIdRef.current = run.run_id
        const result = await waitForRunStream(run.case_id, run.run_id, run.stream_url)
        completed = result === 'final' || result === 'approval'
      } catch (streamError) {
        if (runStarted) {
          throw streamError
        }
        const response = await api.sendTurn(caseId, userText, uploads)
        await applyResponse(response)
        completed = true
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      setOptimisticMessages((current) => [...current, createOptimisticSystemMessage(`发送失败：${detail}`)])
    } finally {
      if (completed) {
        setOptimisticMessages([])
      }
      setRunning(false)
    }
  }

  const resumeApproval = async (approved: boolean) => {
    const approval = pendingApprovals[0]
    const caseId = approval?.case_id || selectedCaseId
    const runId = approval?.run_id || selectedRunId
    if (!caseId || !runId) return
    setRunning(true)
    setPendingApprovals([])
    try {
      try {
        const run = await api.resumeRunApproval(caseId, runId, approved, approved ? 'approved_from_desktop' : 'rejected_from_desktop')
        await waitForRunStream(caseId, run.run_id, run.stream_url)
      } catch (_streamError) {
        const response = await api.resumeApproval(caseId, runId, approved, approved ? 'approved_from_desktop' : 'rejected_from_desktop')
        await applyResponse(response)
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      setOptimisticMessages((current) => [...current, createOptimisticSystemMessage(`审批恢复失败：${detail}`)])
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="app-frame">
      <TitleBar />
      <PanelGroup direction="horizontal" className="cockpit-grid">
        <Panel minSize={23} defaultSize={23} maxSize={31}>
          <CaseRail
            cases={casesQuery.data ?? []}
            selectedCaseId={selectedCaseId}
            onSelect={selectCase}
            onCreate={() => createCase.mutate()}
            onDelete={(caseId) => deleteCase.mutate(caseId)}
            onRefresh={() => void casesQuery.refetch()}
          />
        </Panel>
        <PanelResizeHandle className="resize-handle" />
        <Panel minSize={33} defaultSize={41}>
          <CaseChat
            caseState={caseQuery.data}
            messages={visibleMessages}
            liveEvents={liveEvents}
            liveStatus={liveStatus}
            running={running}
            agentRunning={agentRunning}
            pendingApprovals={pendingApprovals}
            onOpenRequirements={() => setInspectorTab('requirements')}
            onSend={(message, files) => void sendTurn(message, files)}
            onApprovalDecision={(approved) => void resumeApproval(approved)}
          />
        </Panel>
        <PanelResizeHandle className="resize-handle" />
        <Panel minSize={31} defaultSize={36} maxSize={44}>
          <Inspector
            caseState={caseQuery.data}
            evidence={evidenceQuery.data ?? caseQuery.data?.evidence_items ?? []}
            artifacts={artifactsQuery.data ?? []}
            runs={runsQuery.data ?? []}
            events={visibleEvents}
            selectedRunId={selectedRunId}
            selectedEvent={selectedEvent}
            tab={inspectorTab}
            onTabChange={setInspectorTab}
            onRunSelect={setSelectedRunId}
            onEventSelect={setSelectedEvent}
          />
        </Panel>
      </PanelGroup>
    </div>
  )
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

const STREAM_EVENT_KINDS = [
  'run_started',
  'context_loaded',
  'model_started',
  'assistant_delta',
  'tool_started',
  'tool_finished',
  'approval_decision',
  'approval_required',
  'final',
  'error'
]

function liveStatusFromRunStream(event: AgentRunStreamEvent): LiveStatus {
  const tool = stringValue(event.payload.tool)
  const role = stringValue(event.payload.role)
  const label = tool || role || event.kind
  const isDone = event.kind === 'final' || event.kind === 'error' || event.kind === 'approval_required'
  return {
    runId: event.run_id,
    phase: event.kind,
    activeAgent: isDone ? statusLabelForStream(event.kind) : label,
    activeRole: role || tool || '',
    currentStep: numberValue(event.payload.step_count),
    latestSummary: event.summary || statusLabelForStream(event.kind),
    latestThinking: '',
    latestEventId: event.event_id,
    isRunning: !isDone,
    thinkingSource: 'run_stream',
    reasoningChars: 0,
    reasoningChunks: 0,
    runStartedAt: '',
    elapsedMs: numberValue(event.payload.duration_ms),
    activeStep: event.summary || label,
    latestThoughtSummary: event.summary || label,
    updatedAt: event.ts
  }
}

function statusLabelForStream(kind: string) {
  const labels: Record<string, string> = {
    run_started: '运行已开始',
    context_loaded: '上下文已加载',
    model_started: '模型调用中',
    assistant_delta: '回复生成中',
    tool_started: '工具执行中',
    tool_finished: '工具已完成',
    approval_decision: '审批已提交',
    approval_required: '等待确认',
    final: '回复已生成',
    error: '运行失败'
  }
  return labels[kind] || kind
}

function approvalsFromRunStream(event: AgentRunStreamEvent, caseId: string, runId: string): ApprovalInterrupt[] {
  const raw = Array.isArray(event.payload.interrupts) ? event.payload.interrupts : []
  return raw
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
    .map((item) => ({
      type: stringValue(item.type) || 'tool_approval',
      case_id: stringValue(item.case_id) || caseId,
      run_id: stringValue(item.run_id) || runId,
      tool: stringValue(item.tool),
      risk_level: stringValue(item.risk_level),
      input_preview: stringValue(item.input_preview),
      input_sha256: stringValue(item.input_sha256),
      reason: stringValue(item.reason)
    }))
}

function agentTurnResponseFromPayload(payload: Record<string, unknown>): AgentTurnResponse | null {
  const response = payload.response
  if (!response || typeof response !== 'object') return null
  const candidate = response as Partial<AgentTurnResponse>
  if (!candidate.case_id || typeof candidate.reply !== 'string' || !candidate.case_state || !candidate.trace) return null
  return candidate as AgentTurnResponse
}

function numberValue(value: unknown): number {
  const parsed = Number(value ?? 0)
  return Number.isFinite(parsed) ? parsed : 0
}
