import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels'
import { api, createEventSource } from '@/lib/api'
import { approvalInterruptsFromEvents, approvalInterruptsFromTrace } from '@/lib/approvals'
import { createOptimisticSystemMessage, createOptimisticUserMessage, mergeConversationWithOptimistic } from '@/lib/chat'
import { parseLiveStatusMessage, parseTraceEventMessage } from '@/lib/eventStream'
import { mergeEvents } from '@/lib/trace'
import type { AgentTurnResponse, ApprovalInterrupt, AttachmentUpload, ConversationItem, LiveStatus, TraceEvent } from '@/types'
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
      const uploads: AttachmentUpload[] = []
      for (const file of files) {
        uploads.push(await api.uploadAttachment(caseId, file))
      }
      const response = await api.sendTurn(caseId, userText, uploads)
      await applyResponse(response)
      completed = true
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
    try {
      const response = await api.resumeApproval(caseId, runId, approved, approved ? 'approved_from_desktop' : 'rejected_from_desktop')
      await applyResponse(response)
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
