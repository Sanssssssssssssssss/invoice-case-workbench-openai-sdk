import type {
  AgentTurnResponse,
  AgentRunAccepted,
  ArtifactItem,
  AttachmentUpload,
  CaseState,
  CaseSummary,
  ConversationItem,
  EvidenceItem,
  LiveStatus,
  Requirement,
  RunSummary,
  TraceEvent
} from '@/types'

let cachedBaseUrl: string | null = null

export async function getBaseUrl() {
  if (cachedBaseUrl) return cachedBaseUrl
  if (typeof window !== 'undefined' && window.cockpit) {
    const info = await window.cockpit.getBackendInfo()
    cachedBaseUrl = info.baseUrl
    return cachedBaseUrl
  }
  cachedBaseUrl = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8010'
  return cachedBaseUrl
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const baseUrl = await getBaseUrl()
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...init?.headers
    }
  })
  if (!response.ok) {
    const text = await response.text()
    throw new Error(`${response.status} ${response.statusText}: ${text}`)
  }
  return response.json() as Promise<T>
}

export const api = {
  listCases: () => requestJson<CaseSummary[]>('/api/cases'),
  createCase: () => requestJson<CaseSummary>('/api/cases', { method: 'POST' }),
  deleteCase: (caseId: string) => requestJson<{ case_id: string; archived_to: string }>(`/api/cases/${encodeURIComponent(caseId)}`, { method: 'DELETE' }),
  getCase: (caseId: string) => requestJson<CaseState>(`/api/cases/${encodeURIComponent(caseId)}`),
  getConversation: (caseId: string) => requestJson<ConversationItem[]>(`/api/cases/${encodeURIComponent(caseId)}/conversation`),
  getRequirements: (caseId: string) => requestJson<Requirement[]>(`/api/cases/${encodeURIComponent(caseId)}/requirements`),
  getEvidence: (caseId: string) => requestJson<EvidenceItem[]>(`/api/cases/${encodeURIComponent(caseId)}/evidence`),
  getArtifacts: (caseId: string) => requestJson<ArtifactItem[]>(`/api/cases/${encodeURIComponent(caseId)}/artifacts`),
  getRuns: (caseId: string) => requestJson<RunSummary[]>(`/api/cases/${encodeURIComponent(caseId)}/runs`),
  getLiveStatus: (caseId: string) => requestJson<LiveStatus>(`/api/cases/${encodeURIComponent(caseId)}/live-status`),
  getRunEvents: (caseId: string, runId: string) =>
    requestJson<TraceEvent[]>(`/api/cases/${encodeURIComponent(caseId)}/runs/${encodeURIComponent(runId)}/events`),
  startRun: (caseId: string, message: string, attachments: AttachmentUpload[]) =>
    requestJson<AgentRunAccepted>('/api/agent/runs', {
      method: 'POST',
      body: JSON.stringify({
        case_id: caseId,
        message,
        attachments: attachments.map((item) => ({ name: item.name, path: item.path, content_type: item.content_type }))
      })
    }),
  sendTurn: (caseId: string, message: string, attachments: AttachmentUpload[]) =>
    requestJson<AgentTurnResponse>('/api/agent/turn', {
      method: 'POST',
      body: JSON.stringify({
        case_id: caseId,
        message,
        attachments: attachments.map((item) => ({ name: item.name, path: item.path, content_type: item.content_type }))
      })
    }),
  resumeApproval: (caseId: string, runId: string, approved: boolean, reason = '') =>
    requestJson<AgentTurnResponse>(`/api/cases/${encodeURIComponent(caseId)}/runs/${encodeURIComponent(runId)}/approval`, {
      method: 'POST',
      body: JSON.stringify({ approved, reason })
    }),
  resumeRunApproval: (caseId: string, runId: string, approved: boolean, reason = '') =>
    requestJson<AgentRunAccepted>(`/api/agent/runs/${encodeURIComponent(runId)}/approval`, {
      method: 'POST',
      body: JSON.stringify({ case_id: caseId, approved, reason })
    }),
  uploadAttachment: async (caseId: string, file: File) => {
    const data = new FormData()
    data.append('file', file)
    return requestJson<AttachmentUpload>(`/api/cases/${encodeURIComponent(caseId)}/attachments`, {
      method: 'POST',
      body: data
    })
  }
}

export async function createEventSource(path: string) {
  const baseUrl = await getBaseUrl()
  return new EventSource(`${baseUrl}${path}`)
}

export async function absoluteApiUrl(path: string) {
  const baseUrl = await getBaseUrl()
  return `${baseUrl}${path.startsWith('/') ? path : `/${path}`}`
}
