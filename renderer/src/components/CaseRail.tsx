import { useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { BarChart3, FileText, MessageSquare, Plus, RefreshCw, Search, Settings2, SlidersHorizontal, Trash2 } from 'lucide-react'
import { Virtuoso } from 'react-virtuoso'
import type { CaseSummary } from '@/types'
import { shortTime } from '@/lib/trace'
import { StatusChip } from './StatusChip'

interface CaseRailProps {
  cases: CaseSummary[]
  selectedCaseId: string
  onSelect: (caseId: string) => void
  onCreate: () => void
  onDelete: (caseId: string) => void
  onRefresh: () => void
}

export function CaseRail({ cases, selectedCaseId, onSelect, onCreate, onDelete, onRefresh }: CaseRailProps) {
  const [query, setQuery] = useState('')
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return cases
    return cases.filter((item) => `${item.case_id} ${item.summary} ${item.status}`.toLowerCase().includes(needle))
  }, [cases, query])

  return (
    <aside className="case-shell">
      <nav className="icon-rail">
        <button className="rail-icon active" aria-label="案件">
          <MessageSquare size={19} />
        </button>
        <button className="rail-icon" aria-label="文档">
          <FileText size={18} />
        </button>
        <button className="rail-icon" aria-label="分析">
          <BarChart3 size={18} />
        </button>
        <button className="rail-icon" aria-label="设置">
          <Settings2 size={18} />
        </button>
        <div className="rail-user">AR</div>
      </nav>

      <section className="case-rail">
        <div className="case-header">
          <h1>案件</h1>
          <button className="primary-square" onClick={onCreate} aria-label="新建案件">
            <Plus size={20} />
          </button>
        </div>

        <div className="case-search">
          <Search size={18} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索案件" />
          <SlidersHorizontal size={17} className="muted-icon" />
        </div>

        <div className="case-list">
          <Virtuoso
            data={filtered}
            itemContent={(_index, item) => (
              <CaseRow item={item} active={item.case_id === selectedCaseId} onSelect={() => onSelect(item.case_id)} onDelete={() => onDelete(item.case_id)} />
            )}
          />
        </div>

        <footer className="case-footer">
          <span>显示 {Math.min(filtered.length, 10)} / {cases.length || 0}</span>
          <button className="ghost-icon" onClick={onRefresh} aria-label="刷新">
            <RefreshCw size={16} />
          </button>
        </footer>
      </section>
    </aside>
  )
}

function CaseRow({ item, active, onSelect, onDelete }: { item: CaseSummary; active: boolean; onSelect: () => void; onDelete: () => void }) {
  const dot = item.status.includes('ready') || item.status === 'satisfied' ? 'green' : item.status.includes('blocked') || item.status === 'conflict' ? 'red' : item.status === 'missing' ? 'amber' : 'teal'
  return (
    <motion.div
      layout
      className={`case-row ${active ? 'selected' : ''}`}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') onSelect()
      }}
      role="button"
      tabIndex={0}
      initial={{ opacity: 0, y: 7 }}
      animate={{ opacity: 1, y: 0 }}
      whileHover={{ y: -1 }}
      transition={{ duration: 0.16 }}
    >
      <span className={`case-dot ${dot}`} />
      <span className="case-row-main">
        <span className="case-row-top">
          <strong>{item.case_id}</strong>
          <time>{shortTime(item.updated_at)}</time>
        </span>
        <span className="case-row-meta">
          <StatusChip status={item.status} compact />
          {active && (
            <button
              className="case-delete"
              title="删除案件"
              onClick={(event) => {
                event.stopPropagation()
                onDelete()
              }}
            >
              <Trash2 size={13} />
            </button>
          )}
        </span>
      </span>
    </motion.div>
  )
}
