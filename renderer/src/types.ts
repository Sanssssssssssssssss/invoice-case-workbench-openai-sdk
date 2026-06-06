export type RequirementStatus = 'missing' | 'submitted' | 'accepted' | 'weak' | 'rejected' | 'conflict' | 'satisfied'

export interface CaseSummary {
  case_id: string
  status: string
  summary: string
  updated_at: string
  requirement_count: number
  required_count: number
  ready_required_count: number
  evidence_count: number
  missing_count: number
  weak_count: number
  conflict_count: number
  satisfied_count: number
}

export interface Requirement {
  id: string
  label: string
  status: RequirementStatus
  evidence_ids: string[]
  kind: string
  required: boolean
  guidance: string
}

export interface EvidenceItem {
  id: string
  type: string
  credibility: string
  summary: string
  source: string
  content: string
  created_at: string
  review_result: Record<string, unknown>
  supports: Array<{ requirement: string; support_level: string; quoted_text: string }>
  conflicts: string[]
  quoted_text: string[]
  reviewer_notes: string
  metadata: Record<string, unknown>
}

export interface CaseState {
  case_id: string
  case_type: string
  status: string
  summary: string
  requirements: Requirement[]
  evidence_items: EvidenceItem[]
  evidence_cards: Record<string, unknown>[]
  risk_flags: string[]
  missing_materials: string[]
  weak_materials: string[]
  conflict_materials: string[]
  satisfied_materials: string[]
  conversation_summary: string
  next_questions: string[]
  next_action_hint: string
  reply_brief: string
}

export interface ConversationItem {
  ts: string
  role: 'user' | 'assistant' | 'system' | string
  content: string
  metadata: Record<string, unknown>
}

export interface AttachmentUpload {
  case_id: string
  name: string
  path: string
  relative_path: string
  content_type: string
  bytes: number
}

export interface ArtifactItem {
  name: string
  type: string
  run_id: string
  path: string
  updated_at: string
  bytes: number
  content_type: string
  open_url: string
  download_url: string
  generated: boolean
}

export interface RunSummary {
  run_id: string
  status: string
  started_at: string
  completed_at: string
  updated_at: string
  duration_ms: number | null
  phase: string
  tool_count: number
  role_count: number
  model_count: number
  checkpoint_count: number
  error_count: number
  event_count: number
  current_goal: string
  final_answer: string
}

export interface TraceEvent {
  event_id: string
  run_id: string
  case_id: string
  seq: number
  case_seq: number
  ts: string
  kind: 'planner' | 'thinking' | 'model' | 'role' | 'tool' | 'observation' | 'checkpoint' | 'artifact' | 'artifact_summary' | 'error'
  raw_kind: string
  name: string
  status: string
  summary: string
  parent_event_id: string
  caused_by_event_id: string
  duration_ms: number | null
  token_count: number | null
  input_preview: string
  output_preview: string
  payload: Record<string, unknown>
  raw: Record<string, unknown>
}

export interface ApprovalInterrupt {
  type: string
  case_id: string
  run_id: string
  tool: string
  risk_level: string
  input_preview: string
  input_sha256: string
  reason: string
}

export interface AgentTurnResponse {
  case_id: string
  reply: string
  case_state: CaseState
  trace: Record<string, unknown>
}

export interface AgentRunAccepted {
  case_id: string
  run_id: string
  status: string
  stream_url: string
}

export type AgentRunStreamKind =
  | 'run_started'
  | 'context_loaded'
  | 'model_started'
  | 'assistant_delta'
  | 'tool_started'
  | 'tool_finished'
  | 'approval_decision'
  | 'approval_required'
  | 'final'
  | 'error'

export interface AgentRunStreamEvent {
  seq: number
  event_id: string
  run_id: string
  case_id: string
  kind: AgentRunStreamKind
  ts: string
  summary: string
  payload: Record<string, unknown>
}

export interface LiveStatus {
  runId: string
  phase: string
  activeAgent: string
  activeRole: string
  currentStep: number
  latestSummary: string
  latestThinking: string
  latestEventId: string
  isRunning: boolean
  thinkingSource: string
  reasoningChars: number
  reasoningChunks: number
  runStartedAt?: string
  elapsedMs?: number
  activeStep?: string
  latestThoughtSummary?: string
  updatedAt: string
}
