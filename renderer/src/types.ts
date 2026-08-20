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
  conflicts: ConflictRecord[]
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
  review_artifact?: ReviewArtifact | null
  compiled_proof?: CompiledProof | null
}

export type ProofNodeKind = 'CHECK' | 'ALL' | 'ANY'
export type ProofStatus = 'SUPPORTED' | 'CONTRADICTED' | 'NOT_FOUND'

export interface ProofNode {
  id: string
  kind: ProofNodeKind
  statement: string
  depends_on: string[]
  requirement_refs: string[]
  policy_refs: string[]
}

export interface ProofPlan {
  plan_id: string
  version: string
  objective: string
  active_requirement_ids: string[]
  policy_refs: string[]
  roots: Record<string, string>
  nodes: ProofNode[]
}

export interface EvidenceClaim {
  id: string
  subject: string
  predicate: string
  value: unknown
  source_id: string
  quote: string
  locator: string
  confidence: 'low' | 'medium' | 'high'
  attributes: Record<string, unknown>
}

export interface EvidenceIR {
  schema_version: string
  source_ids: string[]
  source_fingerprints: Record<string, string>
  claims: EvidenceClaim[]
}

export interface CheckAssessment {
  check_id: string
  status: ProofStatus
  claim_ids: string[]
  source_ids: string[]
  examined_source_ids: string[]
  reason: string
  missing_fact: string
}

export interface ReviewArtifact {
  plan: ProofPlan
  plan_hash: string
  evidence_ir: EvidenceIR
  evidence_snapshot_hash: string
  assessments: CheckAssessment[]
  submitted_claim_refs: Record<string, string[]>
  policy_hash: string
  unconfigured_policy_refs: string[]
  compiler_version: string
  model: string
  prompt_versions: Record<string, string>
}

export interface ProofNodeResult {
  node_id: string
  kind: ProofNodeKind
  status: ProofStatus
  reason: string
  claim_ids: string[]
  source_ids: string[]
}

export interface DecisionProof {
  requirement_id: string
  root_node_id: string
  status: ProofStatus
  supporting_check_ids: string[]
  contradicting_check_ids: string[]
  unresolved_check_ids: string[]
  obligation_ids: string[]
  plan_hash: string
  evidence_snapshot_hash: string
  policy_hash: string
  stop_reason: string
}

export interface ProofObligation {
  id: string
  requirement_id: string
  check_id: string
  missing_fact: string
  blocking: boolean
  candidate_actions: string[]
}

export interface CompilationDiagnostic {
  code: string
  message: string
  node_id: string
  requirement_id: string
  blocking: boolean
}

export interface CompiledProof {
  node_results: ProofNodeResult[]
  decisions: DecisionProof[]
  obligations: ProofObligation[]
  diagnostics: CompilationDiagnostic[]
}

export interface ConversationItem {
  ts: string
  role: 'user' | 'assistant' | 'system' | string
  content: string
  attachments: ConversationAttachment[]
  metadata: Record<string, unknown>
}

export interface ConversationAttachment {
  name: string
  path: string
  content_type: string
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

export interface ConflictRecord {
  type: string
  conflict_type: string
  requirement: string | null
  severity: 'low' | 'medium' | 'high'
  field: string
  description: string
  details: string
  quoted_text: string
  affected_fields: string[]
  evidence_ids: string[]
  source_values: Record<string, unknown>
  suggested_resolution: string
  resolution_status: string
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
  | 'model_thinking'
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
