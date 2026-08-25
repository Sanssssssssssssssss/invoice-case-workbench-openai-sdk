import { useMemo, useState } from 'react'
import { AlertTriangle, Braces, CheckCircle2, CircleDashed, FileSearch, GitBranch, ShieldCheck } from 'lucide-react'
import type { CaseState, DecisionProof, ProofNode, TraceEvent } from '@/types'
import { compilerArtifactStats, compilerChildRunView, flattenProofTree, proofStatusLabel, proofStatusTone, type CompilerChildRunView } from '@/lib/compilerView'

type CompilerView = 'proof' | 'plan' | 'ir' | 'diagnostics'

const views: Array<{ id: CompilerView; label: string }> = [
  { id: 'proof', label: '证明结论' },
  { id: 'plan', label: 'ProofPlan' },
  { id: 'ir', label: 'Evidence IR' },
  { id: 'diagnostics', label: '缺口与诊断' }
]

export function CompilerPanel({ caseState, events = [] }: { caseState?: CaseState; events?: TraceEvent[] }) {
  const [view, setView] = useState<CompilerView>('proof')
  const artifact = caseState?.review_artifact
  const proof = caseState?.compiled_proof
  const stats = compilerArtifactStats(caseState)
  const childRun = useMemo(() => compilerChildRunView(events), [events])

  if (!artifact || !proof) {
    if (childRun) {
      return (
        <div className="compiler-panel">
          <ChildRunStatus childRun={childRun} />
          <CompilerEmpty />
        </div>
      )
    }
    return (
      <CompilerEmpty />
    )
  }

  return (
    <div className="compiler-panel">
      <header className="compiler-header">
        <div>
          <span className="eyebrow">最新案件快照 · {artifact.compiler_version}</span>
          <h3>{artifact.plan.objective}</h3>
          <p>{artifact.model} · Plan v{artifact.plan.version} · {shortHash(artifact.plan_hash)}</p>
        </div>
        <span className="compiler-health"><ShieldCheck size={16} /> 已经 Kernel 验证</span>
      </header>

      {childRun && <ChildRunStatus childRun={childRun} />}

      <div className="compiler-artifact-flow" aria-label="compiler artifacts">
        <ArtifactStep label="ProofPlan" value={`${stats.checks} checks`} icon={<GitBranch size={15} />} />
        <ArtifactStep label="Evidence IR" value={`${stats.claims} claims`} icon={<Braces size={15} />} />
        <ArtifactStep label="Assessments" value={`${artifact.assessments.length} results`} icon={<FileSearch size={15} />} />
        <ArtifactStep label="DecisionProof" value={`${stats.decisions} roots`} icon={<CheckCircle2 size={15} />} last />
      </div>

      <nav className="compiler-view-tabs" aria-label="compiler view">
        {views.map((item) => (
          <button key={item.id} className={view === item.id ? 'active' : ''} onClick={() => setView(item.id)}>
            {item.label}
            {item.id === 'diagnostics' && stats.obligations > 0 && <span>{stats.obligations}</span>}
          </button>
        ))}
      </nav>

      {view === 'proof' && <ProofView caseState={caseState} />}
      {view === 'plan' && <PlanView caseState={caseState} />}
      {view === 'ir' && <EvidenceIrView caseState={caseState} />}
      {view === 'diagnostics' && <DiagnosticsView caseState={caseState} />}
    </div>
  )
}

function CompilerEmpty() {
  return (
    <div className="compiler-empty">
      <span className="compiler-empty-icon"><CircleDashed size={22} /></span>
      <strong>尚无 Evidence Compiler 产物</strong>
      <p>这个案件可能来自旧版本，或还没有执行证据审核。需求和已保存证据仍可正常查看。</p>
    </div>
  )
}

function ChildRunStatus({ childRun }: { childRun: CompilerChildRunView }) {
  const progress = childRun.totalChecks > 0
    ? `${childRun.completedChecks}/${childRun.totalChecks} CHECK`
    : '正在准备 CHECK'
  return (
    <section className="compiler-child-run" aria-label="Compiler child run">
      <div className="compiler-child-head">
        <div>
          <span className="eyebrow">Durable child run · revision {childRun.revision}</span>
          <strong title={childRun.compilerRunId}>{childRun.compilerRunId}</strong>
        </div>
        <span className={`compiler-run-status ${childRun.status}`}>{childRunStatusLabel(childRun.status)}</span>
      </div>
      <div className="compiler-child-facts">
        <span>{progress}</span>
        {childRun.activeCheckId && <span>当前：{childRun.activeCheckId}</span>}
      </div>
      <div className="compiler-child-events">
        {childRun.events.map((event) => (
          <article key={event.eventId}>
            <div>
              <strong>{event.action || event.stage || 'Compiler 进度'}</strong>
              <span>{[event.status, event.checkId, event.diagnosticCode].filter(Boolean).join(' · ')}</span>
            </div>
            {event.publicReason && <p>{event.publicReason}</p>}
          </article>
        ))}
      </div>
    </section>
  )
}

function childRunStatusLabel(status: string) {
  if (status === 'completed') return '已完成'
  if (status === 'error' || status === 'fatal') return '失败'
  if (status === 'cancelled') return '已取消'
  return '运行中'
}

function ArtifactStep({ label, value, icon, last = false }: { label: string; value: string; icon: React.ReactNode; last?: boolean }) {
  return (
    <div className={`compiler-artifact-step ${last ? 'last' : ''}`}>
      <span>{icon}</span>
      <strong>{label}</strong>
      <small>{value}</small>
    </div>
  )
}

function ProofView({ caseState }: { caseState: CaseState }) {
  const artifact = caseState.review_artifact!
  const proof = caseState.compiled_proof!
  const requirements = new Map(caseState.requirements.map((item) => [item.id, item]))
  return (
    <div className="compiler-content proof-list">
      {proof.decisions.length === 0 && <EmptySection text="Kernel 尚未生成 DecisionProof。" />}
      {proof.decisions.map((decision) => (
        <DecisionCard
          key={decision.requirement_id}
          decision={decision}
          requirementLabel={requirements.get(decision.requirement_id)?.label ?? decision.requirement_id}
          caseState={caseState}
        />
      ))}
    </div>
  )
}

function DecisionCard({ decision, requirementLabel, caseState }: { decision: DecisionProof; requirementLabel: string; caseState: CaseState }) {
  const artifact = caseState.review_artifact!
  const proof = caseState.compiled_proof!
  const rows = useMemo(() => flattenProofTree(artifact.plan, decision.root_node_id), [artifact.plan, decision.root_node_id])
  const results = new Map(proof.node_results.map((item) => [item.node_id, item]))
  const assessments = new Map(artifact.assessments.map((item) => [item.check_id, item]))
  return (
    <section className="decision-card">
      <div className="decision-head">
        <div>
          <strong>{requirementLabel}</strong>
          <span>{decision.requirement_id} · root {decision.root_node_id}</span>
        </div>
        <ProofBadge status={decision.status} />
      </div>
      <div className="proof-tree">
        {rows.map(({ node, depth, repeated }) => {
          const result = results.get(node.id)
          const assessment = assessments.get(node.id)
          return (
            <div className="proof-node" style={{ marginLeft: `${depth * 18}px` }} key={`${decision.requirement_id}:${node.id}`}>
              <span className={`node-kind kind-${node.kind.toLowerCase()}`}>{node.kind}</span>
              <div>
                <strong>{node.statement || aggregateLabel(node)}</strong>
                <span>{node.id}{repeated ? ' · 已在上方展开' : ''}</span>
                {assessment?.reason && <p>{assessment.reason}</p>}
              </div>
              {result && <ProofBadge status={result.status} compact />}
            </div>
          )
        })}
      </div>
      <footer className="decision-foot">
        <span>{decision.stop_reason}</span>
        <span>{decision.supporting_check_ids.length} 支持 · {decision.contradicting_check_ids.length} 反驳 · {decision.unresolved_check_ids.length} 未决</span>
      </footer>
    </section>
  )
}

function PlanView({ caseState }: { caseState: CaseState }) {
  const artifact = caseState.review_artifact!
  const roots = Object.entries(artifact.plan.roots)
  return (
    <div className="compiler-content">
      <section className="compiler-section">
        <div className="section-title"><strong>Requirement roots</strong><span>{roots.length}</span></div>
        <div className="root-map">
          {roots.map(([requirement, node]) => <span key={requirement}><b>{requirement}</b><i>→</i>{node}</span>)}
        </div>
      </section>
      <section className="compiler-section">
        <div className="section-title"><strong>Plan nodes</strong><span>{artifact.plan.nodes.length}</span></div>
        {artifact.plan.nodes.map((node) => <PlanNodeRow key={node.id} node={node} />)}
      </section>
      {artifact.plan.policy_refs.length > 0 && (
        <section className="compiler-section">
          <div className="section-title"><strong>Policy refs</strong><span>{artifact.plan.policy_refs.length}</span></div>
          <div className="token-list">{artifact.plan.policy_refs.map((item) => <span key={item}>{item}</span>)}</div>
        </section>
      )}
    </div>
  )
}

function PlanNodeRow({ node }: { node: ProofNode }) {
  return (
    <div className="plan-node-row">
      <span className={`node-kind kind-${node.kind.toLowerCase()}`}>{node.kind}</span>
      <div>
        <strong>{node.id}</strong>
        <p>{node.statement || aggregateLabel(node)}</p>
        {(node.requirement_refs.length > 0 || node.policy_refs.length > 0) && (
          <span>{[...node.requirement_refs, ...node.policy_refs].join(' · ')}</span>
        )}
      </div>
    </div>
  )
}

function EvidenceIrView({ caseState }: { caseState: CaseState }) {
  const ir = caseState.review_artifact!.evidence_ir
  const sources = new Map(caseState.evidence_items.map((item) => [item.id, item]))
  return (
    <div className="compiler-content">
      <section className="compiler-section">
        <div className="section-title"><strong>可信来源</strong><span>{ir.source_ids.length}</span></div>
        <div className="source-list">
          {ir.source_ids.map((sourceId) => (
            <div key={sourceId}>
              <strong>{sources.get(sourceId)?.summary || sourceId}</strong>
              <span>{sources.get(sourceId)?.type || 'source'} · {shortHash(ir.source_fingerprints[sourceId])}</span>
            </div>
          ))}
        </div>
      </section>
      <section className="compiler-section">
        <div className="section-title"><strong>Grounded Claims</strong><span>{ir.claims.length}</span></div>
        {ir.claims.map((claim) => (
          <article className="claim-card" key={claim.id}>
            <div className="claim-head">
              <span>{claim.subject}</span>
              <strong>{claim.predicate}</strong>
              <b>{displayValue(claim.value)}</b>
            </div>
            <blockquote>“{claim.quote}”</blockquote>
            <footer>{claim.locator} · {claim.confidence} confidence · {claim.source_id}</footer>
          </article>
        ))}
      </section>
    </div>
  )
}

function DiagnosticsView({ caseState }: { caseState: CaseState }) {
  const artifact = caseState.review_artifact!
  const proof = caseState.compiled_proof!
  const hasAnything = proof.obligations.length > 0 || proof.diagnostics.length > 0 || artifact.unconfigured_policy_refs.length > 0
  return (
    <div className="compiler-content diagnostics-list">
      {!hasAnything && <EmptySection text="当前没有阻塞义务、编译诊断或未配置 Policy。" success />}
      {proof.obligations.map((item) => (
        <article className="diagnostic-card" key={item.id}>
          <AlertTriangle size={16} />
          <div>
            <strong>{item.requirement_id} · {item.check_id}</strong>
            <p>{item.missing_fact || '该检查仍缺少可核查事实。'}</p>
            <span>{item.blocking ? '阻塞' : '非阻塞'} · 建议：{item.candidate_actions.join(' / ')}</span>
          </div>
        </article>
      ))}
      {proof.diagnostics.map((item, index) => (
        <article className="diagnostic-card" key={`${item.code}:${index}`}>
          <AlertTriangle size={16} />
          <div>
            <strong>{item.code}</strong>
            <p>{item.message}</p>
            <span>{[item.requirement_id, item.node_id].filter(Boolean).join(' · ') || 'compiler'}</span>
          </div>
        </article>
      ))}
      {artifact.unconfigured_policy_refs.map((item) => (
        <article className="diagnostic-card" key={item}>
          <CircleDashed size={16} />
          <div><strong>Policy 未配置</strong><p>{item}</p></div>
        </article>
      ))}
    </div>
  )
}

function ProofBadge({ status, compact = false }: { status: string; compact?: boolean }) {
  return <span className={`proof-badge ${proofStatusTone(status)} ${compact ? 'compact' : ''}`}>{proofStatusLabel(status)}</span>
}

function EmptySection({ text, success = false }: { text: string; success?: boolean }) {
  return (
    <div className={`compiler-section-empty ${success ? 'success' : ''}`}>
      {success ? <CheckCircle2 size={18} /> : <CircleDashed size={18} />}
      <span>{text}</span>
    </div>
  )
}

function aggregateLabel(node: ProofNode) {
  return `${node.kind} (${node.depends_on.join(', ')})`
}

function shortHash(value = '') {
  if (!value) return '无快照 hash'
  return `${value.slice(0, 8)}…${value.slice(-6)}`
}

function displayValue(value: unknown) {
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  if (!text) return '—'
  return text.length > 90 ? `${text.slice(0, 87)}…` : text
}
