import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { api, createEventSource } from '@/lib/api'
import { approvalInterruptsFromEvents, approvalInterruptsFromTrace } from '@/lib/approvals'
import { createOptimisticSystemMessage, createOptimisticUserMessage, mergeConversationWithOptimistic } from '@/lib/chat'
import { isNewAgentRunStreamEvent, parseAgentRunStreamMessage, parseLiveStatusMessage, parseTraceEventMessage } from '@/lib/eventStream'
import { mergeEvents, selectActivityEvents } from '@/lib/trace'
import type { AgentRunStreamEvent, AgentTurnResponse, ApprovalInterrupt, LiveStatus, TraceEvent } from '@/types'
import { latestReportArtifacts } from '@/lib/artifacts'
import {
  acceptApproval,
  beginApproval,
  canStartRun,
  caseRuntime,
  createCaseRuntimeState,
  finishRun,
  receiveApprovals,
  replaceCaseRuntime,
  retryApproval,
  type CaseRuntimeMap,
  type CaseRuntimeState
} from '@/lib/caseRuntime'
import { useUiStore } from '@/store/uiStore'
import { TitleBar } from '@/components/TitleBar'
import { CaseRail } from '@/components/CaseRail'
import { CaseChat } from '@/components/CaseChat'
import { Inspector } from '@/components/Inspector'

export default function App() {
  const queryClient = useQueryClient()
  const [runtimeByCase, setRuntimeByCase] = useState<CaseRuntimeMap>({})
  const runtimeByCaseRef = useRef<CaseRuntimeMap>({})
  const streamSeqByRunRef = useRef<Record<string, number>>({})
  const selectedCaseId = useUiStore((state) => state.selectedCaseId)
  const selectedEvent = useUiStore((state) => state.selectedEvent)
  const inspectorTab = useUiStore((state) => state.inspectorTab)
  const setSelectedCaseId = useUiStore((state) => state.setSelectedCaseId)
  const setSelectedEvent = useUiStore((state) => state.setSelectedEvent)
  const setInspectorTab = useUiStore((state) => state.setInspectorTab)

  const updateCaseRuntime = useCallback((caseId: string, update: (current: CaseRuntimeState) => CaseRuntimeState) => {
    if (!caseId) return
    const next = replaceCaseRuntime(runtimeByCaseRef.current, caseId, update)
    runtimeByCaseRef.current = next
    setRuntimeByCase(next)
  }, [])
  const runtime = caseRuntime(runtimeByCase, selectedCaseId)
  const { running, liveEvents, liveStatus, optimisticMessages, pendingApprovals, selectedRunId } = runtime

  const setSelectedRunId = useCallback((caseId: string, runId: string) => {
    updateCaseRuntime(caseId, (current) => ({ ...current, selectedRunId: runId }))
    if (caseId === useUiStore.getState().selectedCaseId) setSelectedEvent(null)
  }, [setSelectedEvent, updateCaseRuntime])

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
      setSelectedRunId(selectedCaseId, runsQuery.data[0].run_id)
    }
  }, [runsQuery.data, selectedCaseId, selectedRunId, setSelectedRunId])

  useEffect(() => {
    if (liveStatusQuery.data) {
      updateCaseRuntime(selectedCaseId, (current) => ({
        ...current,
        liveStatus: mergeLiveStatusFromServer(liveStatusQuery.data!, current.liveStatus)
      }))
    }
  }, [liveStatusQuery.data, selectedCaseId, updateCaseRuntime])

  useEffect(() => {
    if (!agentRunning || !selectedCaseId) return
    let closed = false
    let source: EventSource | null = null
    const lastCaseSeq = caseRuntime(runtimeByCaseRef.current, selectedCaseId).liveEvents.reduce((max, event) => Math.max(max, event.case_seq), 0)

    createEventSource(`/api/cases/${encodeURIComponent(selectedCaseId)}/events/stream?after_case_seq=${lastCaseSeq}`).then((eventSource) => {
      if (closed) {
        eventSource.close()
        return
      }
      source = eventSource
      source.addEventListener('trace_event', (event) => {
        const traceEvent = parseTraceEventMessage((event as MessageEvent).data)
        updateCaseRuntime(selectedCaseId, (current) => ({
          ...current,
          liveEvents: mergeEvents(current.liveEvents, [traceEvent]),
          selectedRunId: current.selectedRunId || traceEvent.run_id
        }))
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
  }, [agentRunning, selectedCaseId, queryClient, updateCaseRuntime])

  useEffect(() => {
    if (!selectedCaseId || !agentRunning) return
    let closed = false
    let source: EventSource | null = null
    const afterCaseSeq = caseRuntime(runtimeByCaseRef.current, selectedCaseId).liveEvents.reduce((max, event) => Math.max(max, event.case_seq), 0)

    createEventSource(`/api/cases/${encodeURIComponent(selectedCaseId)}/live-status/stream?after_case_seq=${afterCaseSeq}`).then((eventSource) => {
      if (closed) {
        eventSource.close()
        return
      }
      source = eventSource
      source.addEventListener('live_status', (event) => {
        const status = parseLiveStatusMessage((event as MessageEvent).data)
        updateCaseRuntime(selectedCaseId, (current) => ({
          ...current,
          liveStatus: mergeLiveStatusFromServer(status, current.liveStatus)
        }))
      })
    })

    return () => {
      closed = true
      source?.close()
    }
  }, [agentRunning, selectedCaseId, updateCaseRuntime])

  const visibleEvents = useMemo(() => {
    const base = eventsQuery.data ?? []
    const liveForRun = selectedRunId ? liveEvents.filter((event) => event.run_id === selectedRunId) : liveEvents
    return mergeEvents(base, liveForRun)
  }, [eventsQuery.data, liveEvents, selectedRunId])
  const activityEvents = selectActivityEvents(liveEvents, visibleEvents, agentRunning)

  const visibleMessages = useMemo(
    () => mergeConversationWithOptimistic(conversationQuery.data ?? [], optimisticMessages),
    [conversationQuery.data, optimisticMessages]
  )
  const reportArtifacts = useMemo(
    () => latestReportArtifacts(artifactsQuery.data ?? []),
    [artifactsQuery.data]
  )

  useEffect(() => {
    if (!selectedCaseId || pendingApprovals.length > 0 || runtime.approvalPhase === 'executing') return
    const waitingRun = (runsQuery.data ?? []).find((run) => run.status === 'waiting_approval')
    if (!waitingRun) return
    if (selectedRunId !== waitingRun.run_id) {
      setSelectedRunId(selectedCaseId, waitingRun.run_id)
      return
    }
    const approvals = approvalInterruptsFromEvents(selectedCaseId, waitingRun.run_id, visibleEvents)
    if (approvals.length) {
      updateCaseRuntime(selectedCaseId, (current) => receiveApprovals(current, approvals))
    }
  }, [pendingApprovals.length, runsQuery.data, runtime.approvalPhase, selectedCaseId, selectedRunId, setSelectedRunId, updateCaseRuntime, visibleEvents])

  const createCase = useMutation({
    mutationFn: api.createCase,
    onSuccess: (created) => {
      queryClient.setQueryData(['cases'], (current: unknown) => [created, ...((current as typeof casesQuery.data) ?? [])])
      setSelectedCaseId(created.case_id)
      updateCaseRuntime(created.case_id, () => createCaseRuntimeState())
    }
  })

  const deleteCase = useMutation({
    mutationFn: api.deleteCase,
    onSuccess: (_result, caseId) => {
      queryClient.setQueryData(['cases'], (current: unknown) => ((current as typeof casesQuery.data) ?? []).filter((item) => item.case_id !== caseId))
      if (selectedCaseId === caseId) {
        const next = (casesQuery.data ?? []).find((item) => item.case_id !== caseId)
        setSelectedCaseId(next?.case_id ?? '')
      }
      const nextRuntimes = { ...runtimeByCaseRef.current }
      delete nextRuntimes[caseId]
      runtimeByCaseRef.current = nextRuntimes
      setRuntimeByCase(nextRuntimes)
    }
  })

  const selectCase = (caseId: string) => {
    setSelectedCaseId(caseId)
  }

  const refreshCaseData = async (caseId: string, runId = '', options: { deferSecondary?: boolean } = {}) => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['cases'] }),
      queryClient.invalidateQueries({ queryKey: ['case', caseId] }),
      queryClient.invalidateQueries({ queryKey: ['conversation', caseId] })
    ])
    const secondaryRefresh = Promise.all([
      queryClient.invalidateQueries({ queryKey: ['runs', caseId] }),
      queryClient.invalidateQueries({ queryKey: ['liveStatus', caseId] }),
      queryClient.invalidateQueries({ queryKey: ['artifacts', caseId] }),
      queryClient.invalidateQueries({ queryKey: ['evidence', caseId] }),
      runId ? queryClient.invalidateQueries({ queryKey: ['runEvents', caseId, runId] }) : Promise.resolve()
    ])
    if (options.deferSecondary) {
      void secondaryRefresh.catch(() => undefined)
      return
    }
    await secondaryRefresh
  }

  const applyResponse = async (response: AgentTurnResponse) => {
    const approvals = approvalInterruptsFromTrace(response.case_id, response.trace)
    const runId = approvals[0]?.run_id || stringValue(response.trace.run_id)
    updateCaseRuntime(response.case_id, (current) => {
      const withRun = { ...current, selectedRunId: runId || current.selectedRunId }
      return approvals.length ? receiveApprovals(withRun, approvals) : finishRun(withRun)
    })
    await refreshCaseData(response.case_id, runId, { deferSecondary: true })
  }

  const updateFromRunStreamEvent = (event: AgentRunStreamEvent) => {
    streamSeqByRunRef.current[event.run_id] = Math.max(streamSeqByRunRef.current[event.run_id] ?? 0, event.seq)
    updateCaseRuntime(event.case_id, (current) => {
      const updated = {
        ...current,
        selectedRunId: current.selectedRunId || event.run_id,
        liveStatus: liveStatusFromRunStream(event, current.liveStatus)
      }
      return event.kind === 'final' || event.kind === 'error' ? finishRun(updated) : updated
    })
  }

  const waitForRunStream = async (caseId: string, runId: string, streamUrl: string) => {
    const afterSeq = streamSeqByRunRef.current[runId] ?? 0
    const path = `${streamUrl}${streamUrl.includes('?') ? '&' : '?'}after_seq=${afterSeq}`
    return new Promise<'final' | 'approval'>((resolve, reject) => {
      let source: EventSource | null = null
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
          if (!event) return
          if (!isNewAgentRunStreamEvent(event, streamSeqByRunRef.current[event.run_id] ?? 0)) return
          updateFromRunStreamEvent(event)
          if (event.kind === 'assistant_delta') {
            return
          } else if (event.kind === 'approval_required') {
            const approvals = approvalsFromRunStream(event, caseId, runId)
            updateCaseRuntime(caseId, (current) => receiveApprovals(current, approvals))
            await refreshCaseData(caseId, runId, { deferSecondary: true })
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
        })
        .catch(fail)
    })
  }

  const sendTurn = async (message: string, files: File[]) => {
    const caseId = selectedCaseId || caseQuery.data?.case_id
    if (!caseId) return
    const currentRuntime = caseRuntime(runtimeByCaseRef.current, caseId)
    if (!canStartRun(currentRuntime, runsQuery.data?.some((run) => run.status === 'running'))) return
    const userText = message || '请复核已上传的材料。'
    updateCaseRuntime(caseId, (current) => ({
      ...current,
      running: true,
      liveEvents: [],
      liveStatus: null,
      pendingApprovals: [],
      approvalPhase: 'idle',
      approvalInFlight: null,
      resolvedApprovalKeys: [],
      optimisticMessages: [
        ...current.optimisticMessages,
        createOptimisticUserMessage({
          content: userText,
          attachments: files.map((file) => ({ name: file.name, path: '', content_type: file.type || 'application/octet-stream' }))
        })
      ]
    }))
    let completed = false
    try {
      const uploads = await Promise.all(files.map((file) => api.uploadAttachment(caseId, file)))
      const run = await api.startRun(caseId, userText, uploads)
      setSelectedRunId(caseId, run.run_id)
      const result = await waitForRunStream(run.case_id, run.run_id, run.stream_url)
      completed = result === 'final' || result === 'approval'
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      updateCaseRuntime(caseId, (current) => ({
        ...finishRun(current),
        optimisticMessages: [...current.optimisticMessages, createOptimisticSystemMessage(`发送失败：${detail}`)]
      }))
    } finally {
      updateCaseRuntime(caseId, (current) => ({
        ...current,
        running: false,
        optimisticMessages: completed ? [] : current.optimisticMessages
      }))
    }
  }

  const resumeApproval = async (approved: boolean) => {
    const caseId = selectedCaseId
    const current = caseRuntime(runtimeByCaseRef.current, caseId)
    const approval = current.pendingApprovals[0]
    const runId = approval?.run_id || current.selectedRunId
    if (!caseId || !runId) return
    updateCaseRuntime(caseId, beginApproval)
    let accepted = false
    try {
      const run = await api.resumeRunApproval(caseId, runId, approved, approved ? 'approved_from_desktop' : 'rejected_from_desktop')
      accepted = true
      updateCaseRuntime(caseId, acceptApproval)
      await waitForRunStream(caseId, run.run_id, run.stream_url)
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      updateCaseRuntime(caseId, (state) => ({
        ...(accepted ? finishRun(state) : retryApproval(state)),
        optimisticMessages: [...state.optimisticMessages, createOptimisticSystemMessage(`审批恢复失败：${detail}`)]
      }))
    } finally {
      updateCaseRuntime(caseId, (state) => ({ ...state, running: false }))
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
            agentRunning={agentRunning}
          />
        </Panel>
        <PanelResizeHandle className="resize-handle" />
        <Panel minSize={33} defaultSize={41}>
          <CaseChat
            caseId={caseQuery.data?.case_id ?? selectedCaseId}
            caseState={caseQuery.data}
            messages={visibleMessages}
            reportArtifacts={reportArtifacts}
            liveEvents={activityEvents}
            liveStatus={liveStatus}
            running={agentRunning}
            agentRunning={agentRunning}
            pendingApprovals={pendingApprovals}
            approvalPhase={runtime.approvalPhase}
            approvalInFlight={runtime.approvalInFlight}
            completedApprovalSteps={runtime.resolvedApprovalKeys.length}
            draft={runtime.draft}
            files={runtime.files}
            onOpenRequirements={() => setInspectorTab('requirements')}
            onSend={(message, files) => void sendTurn(message, files)}
            onDraftChange={(draft) => updateCaseRuntime(selectedCaseId, (current) => ({ ...current, draft }))}
            onFilesChange={(files) => updateCaseRuntime(selectedCaseId, (current) => ({ ...current, files }))}
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
            onRunSelect={(runId) => setSelectedRunId(selectedCaseId, runId)}
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
  'model_thinking',
  'assistant_delta',
  'tool_started',
  'tool_finished',
  'approval_decision',
  'approval_required',
  'final',
  'error'
]

function liveStatusFromRunStream(event: AgentRunStreamEvent, current: LiveStatus | null): LiveStatus | null {
  if (event.kind === 'assistant_delta') {
    return current
  }
  const tool = stringValue(event.payload.tool)
  const role = stringValue(event.payload.role)
  const label = tool || role || event.kind
  const isDone = event.kind === 'final' || event.kind === 'error' || event.kind === 'approval_required'
  const thoughtSummary = streamThoughtSummary(event, label)
  const isThinking = event.kind === 'model_thinking'
  const reasoningText = isThinking ? stringValue(event.payload.public_reason || event.payload.action || event.summary) : ''
  const sameRun = current?.runId === event.run_id
  const latestThinking = reasoningText || (sameRun ? current.latestThinking : '')
  const latestThoughtSummary = isThinking && reasoningText ? thoughtSummary || current?.latestThoughtSummary || '' : sameRun ? current.latestThoughtSummary || '' : ''
  const activeStep = isThinking && reasoningText ? thoughtSummary || event.summary || label : sameRun ? current.activeStep || '' : ''
  const hasCurrentReasoning = sameRun && current?.thinkingSource === 'public_work_log' && Boolean(current.latestThinking?.trim())
  return {
    runId: event.run_id,
    phase: event.kind,
    activeAgent: isDone ? statusLabelForStream(event.kind) : label,
    activeRole: isThinking ? role || tool || '' : hasCurrentReasoning ? current?.activeRole || '' : role || tool || '',
    currentStep: numberValue(event.payload.step_count),
    latestSummary: event.summary || statusLabelForStream(event.kind),
    latestThinking,
    latestEventId: event.event_id,
    isRunning: !isDone,
    thinkingSource: isThinking && reasoningText ? 'public_work_log' : sameRun ? current.thinkingSource : '',
    reasoningChars: isThinking ? reasoningText.length : sameRun ? current.reasoningChars : 0,
    reasoningChunks: isThinking && reasoningText ? 1 : sameRun ? current.reasoningChunks : 0,
    runStartedAt: stringValue(event.payload.run_started_at) || (sameRun ? current?.runStartedAt || '' : '') || event.ts,
    elapsedMs: numberValue(event.payload.duration_ms),
    activeStep,
    latestThoughtSummary,
    updatedAt: event.ts
  }
}

function mergeLiveStatusFromServer(status: LiveStatus, current: LiveStatus | null): LiveStatus {
  if (
    current?.runId === status.runId &&
    current.thinkingSource === 'public_work_log' &&
    current.latestThinking?.trim() &&
    status.thinkingSource !== 'public_work_log'
  ) {
    return {
      ...status,
      activeRole: current.activeRole,
      latestThinking: current.latestThinking,
      thinkingSource: current.thinkingSource,
      reasoningChars: current.reasoningChars,
      reasoningChunks: current.reasoningChunks,
      activeStep: current.activeStep,
      latestThoughtSummary: current.latestThoughtSummary
    }
  }
  if (
    current?.runId === status.runId &&
    current.thinkingSource === 'public_work_log' &&
    status.thinkingSource === 'public_work_log' &&
    current.reasoningChars > status.reasoningChars
  ) {
    return {
      ...status,
      activeRole: current.activeRole,
      latestThinking: current.latestThinking,
      reasoningChars: current.reasoningChars,
      reasoningChunks: current.reasoningChunks,
      activeStep: current.activeStep,
      latestThoughtSummary: current.latestThoughtSummary
    }
  }
  return status
}

function streamThoughtSummary(event: AgentRunStreamEvent, label: string) {
  if (event.summary && event.summary !== 'delta') return event.summary
  const role = stringValue(event.payload.role)
  const tool = stringValue(event.payload.tool)
  if (event.kind === 'context_loaded') return '正在读取案卷、附件摘要和当前材料状态。'
  if (event.kind === 'model_started') return `${roleLabel(role || label)}正在判断下一步处理路径。`
  if (event.kind === 'tool_started') return `正在执行工具：${tool || label}。`
  if (event.kind === 'tool_finished') return `工具已返回：${tool || label}，正在复核结果。`
  if (event.kind === 'approval_decision') return '已收到人工确认，正在继续执行。'
  if (event.kind === 'approval_required') return '需要人工确认后才能继续写入或渲染文件。'
  if (event.kind === 'final') return '运行已完成，正在刷新聊天和产物。'
  if (event.kind === 'error') return '运行失败，正在保留已有 trace 供排查。'
  return label
}

function roleLabel(role: string) {
  const labels: Record<string, string> = {
    planner: '规划器',
    materials_advisor: '材料顾问',
    evidence_reviewer: '证据审核员',
    case_patch_writer: '案卷更新员',
    report_writer: '报告撰写员',
    summarizer: '附件摘要器'
  }
  return labels[role] ?? (role || 'Agent')
}

function statusLabelForStream(kind: string) {
  const labels: Record<string, string> = {
    run_started: '运行已开始',
    context_loaded: '上下文已加载',
    model_started: '模型调用中',
    assistant_delta: '',
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
