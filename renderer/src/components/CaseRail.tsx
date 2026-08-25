import { useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { BarChart3, FileText, MessageSquare, Plus, RefreshCw, Search, SlidersHorizontal, Trash2 } from 'lucide-react'
import { Virtuoso } from 'react-virtuoso'
import type { CaseSummary } from '@/types'
import { shortTime } from '@/lib/trace'
import { StatusChip } from './StatusChip'
import { loadAgentSettings, persistAgentSettings, settingsDraft, settingsInput, type AgentSettingsDraft } from '@/lib/agentSettings'
import { AgentSettings, SettingsButton } from './AgentSettings'

interface CaseRailProps {
  cases: CaseSummary[]
  selectedCaseId: string
  onSelect: (caseId: string) => void
  onCreate: () => void
  onDelete: (caseId: string) => void
  onRefresh: () => void
  agentRunning: boolean
}

export function CaseRail({ cases, selectedCaseId, onSelect, onCreate, onDelete, onRefresh, agentRunning }: CaseRailProps) {
  const [query, setQuery] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settings, setSettings] = useState<AgentSettingsDraft | null>(null)
  const [settingsLoading, setSettingsLoading] = useState(false)
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [settingsError, setSettingsError] = useState('')
  const [settingsSaved, setSettingsSaved] = useState(false)
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return cases
    return cases.filter((item) => `${item.case_id} ${item.summary} ${item.status}`.toLowerCase().includes(needle))
  }, [cases, query])

  const openSettings = async () => {
    setSettingsOpen(true)
    setSettings(null)
    setSettingsLoading(true)
    setSettingsError('')
    setSettingsSaved(false)
    try {
      setSettings(settingsDraft(await loadAgentSettings()))
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : String(error))
    } finally {
      setSettingsLoading(false)
    }
  }

  const saveSettings = async () => {
    if (!settings || agentRunning || settingsSaving) return
    setSettingsSaving(true)
    setSettingsError('')
    setSettingsSaved(false)
    try {
      const result = await persistAgentSettings(settingsInput(settings))
      setSettings(settingsDraft(result.settings))
      setSettingsSaved(true)
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : String(error))
    } finally {
      setSettingsSaving(false)
    }
  }

  return (
    <>
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
          <SettingsButton onClick={() => void openSettings()} />
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
      <AgentSettings
        open={settingsOpen}
        value={settings}
        onChange={(value) => {
          setSettings(value)
          setSettingsSaved(false)
        }}
        onClose={() => setSettingsOpen(false)}
        onSave={() => void saveSettings()}
        agentRunning={agentRunning}
        loading={settingsLoading}
        saving={settingsSaving}
        error={settingsError}
        saved={settingsSaved}
      />
    </>
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
