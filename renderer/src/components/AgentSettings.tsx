import { Settings2, X } from 'lucide-react'
import { selectProvider, type AgentSettingsDraft } from '@/lib/agentSettings'

export function SettingsButton({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" className="rail-settings" onClick={onClick} aria-label="Agent 设置">
      <Settings2 size={17} />
      <span>设置</span>
    </button>
  )
}

export function AgentSettings({
  open,
  value,
  onChange,
  onClose,
  onSave,
  agentRunning,
  loading,
  saving,
  error,
  saved
}: {
  open: boolean
  value: AgentSettingsDraft | null
  onChange: (value: AgentSettingsDraft) => void
  onClose: () => void
  onSave: () => void
  agentRunning: boolean
  loading: boolean
  saving: boolean
  error: string
  saved: boolean
}) {
  if (!open) return null
  const update = (field: keyof AgentSettingsDraft, next: string) => value && onChange({ ...value, [field]: next })

  return (
    <div className="settings-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="agent-settings" role="dialog" aria-modal="true" aria-labelledby="agent-settings-title">
        <header>
          <div>
            <span>BUSINESS AGENT</span>
            <h2 id="agent-settings-title">Agent 设置</h2>
            <p>连接、推理与运行预算</p>
          </div>
          <button type="button" className="ghost-icon" onClick={onClose} aria-label="关闭设置"><X size={18} /></button>
        </header>

        <div className={`settings-notice ${agentRunning ? 'blocked' : ''}`}>
          {agentRunning
            ? '当前 Agent 正在运行，不能保存设置。运行结束后保存会安全重启本地 Backend。'
            : '保存后将安全重启本地 Backend，下一次运行立即使用新配置。API Key 只在 Electron 主进程加密保存，不回传页面或写入 trace。'}
        </div>

        <div className="settings-body">
          {loading && <p className="settings-state">正在读取安全设置…</p>}
          {error && <p className="settings-state error">{error}</p>}
          {saved && <p className="settings-state success">设置已保存，本地 Backend 已切换。</p>}
          {value && <>
          <SettingsSection title="模型连接" description="对应后端 LLM_PROVIDER、LLM_BASE_URL、LLM_MODEL 与 LLM_API_KEY。">
            <label>
              <span>Provider</span>
              <select value={value.provider} onChange={(event) => onChange(selectProvider(value, event.target.value as AgentSettingsDraft['provider']))}>
                <option value="openai">OpenAI 官方</option>
                <option value="deepseek">DeepSeek 官方</option>
                <option value="commandcode">CommandCode · DeepSeek V4 Flash</option>
                <option value="compatible">自定义 OpenAI-compatible</option>
              </select>
            </label>
            <label>
              <span>Base URL</span>
              <input type="url" value={value.baseUrl} onChange={(event) => update('baseUrl', event.target.value)} placeholder="https://api.example.com/v1" autoComplete="url" readOnly={value.provider !== 'compatible'} />
            </label>
            <label>
              <span>Model</span>
              <input value={value.model} onChange={(event) => update('model', event.target.value)} placeholder="模型名称" autoComplete="off" readOnly={value.provider === 'commandcode'} />
            </label>
            <label>
              <span>API Key</span>
              <div className="settings-secret">
                <input
                  type="password"
                  value={value.apiKey}
                  onChange={(event) => onChange({ ...value, apiKey: event.target.value, apiKeyAction: event.target.value ? 'replace' : 'keep' })}
                  placeholder={value.apiKeyAction === 'clear' ? '保存后清除' : value.hasApiKey ? '已安全保存；留空保持' : '输入 API Key'}
                  autoComplete="new-password"
                  spellCheck={false}
                />
                {(value.hasApiKey || value.apiKeyAction === 'replace') && (
                  <button type="button" onClick={() => onChange({ ...value, apiKey: '', apiKeyAction: value.apiKeyAction === 'clear' ? 'keep' : 'clear' })}>
                    {value.apiKeyAction === 'clear' ? '撤销' : '清除'}
                  </button>
                )}
              </div>
              <small>{value.apiKeyAction === 'clear' ? '保存后删除已存密钥。' : '密钥不会回显、进入 trace 或明文落盘。'}</small>
            </label>
          </SettingsSection>

          <SettingsSection title="推理与预算" description="Manager 默认 high，Fine Verifier 固定 high；这里控制其余 Worker。">
            <label>
              <span>Worker thinking / reasoning</span>
              <select value={value.thinking} onChange={(event) => update('thinking', event.target.value)}>
                <option value="disabled">Disabled</option>
                <option value="enabled">Enabled</option>
                <option value="low">Low</option>
                <option value="high">High</option>
                <option value="max">Max</option>
              </select>
            </label>
            <label>
              <span>Manager 最大步数</span>
              <input type="number" min="1" step="1" value={value.maxSteps} onChange={(event) => update('maxSteps', event.target.value)} placeholder="沿用 INVOICE_AGENT_MAX_STEPS" />
            </label>
            <label>
              <span>Context 字符预算</span>
              <input type="number" min="1000" step="1000" value={value.contextChars} onChange={(event) => update('contextChars', event.target.value)} placeholder="沿用 INVOICE_AGENT_CONTEXT_CHAR_LIMIT" />
            </label>
          </SettingsSection>

          <SettingsSection title="安全审批" description="当前不是用户可调开关。">
            <div className="settings-policy">
              <strong>后端 Policy Gate 生效</strong>
              <p>外部写入、破坏性及高权限动作按工具风险触发 HITL；本页面不能关闭或降低审批要求。</p>
            </div>
          </SettingsSection>
          </>}
        </div>

        <footer>
          <span>{value ? settingsSummary(value) : '尚未读取设置'}</span>
          <div>
            <button type="button" className="settings-secondary" onClick={onClose}>关闭</button>
            <button type="button" className="settings-primary" onClick={onSave} disabled={!value || loading || saving || agentRunning}>{saving ? '正在重启…' : '保存并重启 Backend'}</button>
          </div>
        </footer>
      </section>
    </div>
  )
}

function SettingsSection({ title, description, children }: { title: string; description: string; children: React.ReactNode }) {
  return (
    <section className="settings-section">
      <header><strong>{title}</strong><span>{description}</span></header>
      <div>{children}</div>
    </section>
  )
}

export function settingsSummary(value: AgentSettingsDraft) {
  const key = value.apiKeyAction === 'clear' ? 'API Key 将清除' : value.apiKeyAction === 'replace' ? 'API Key 将替换' : value.hasApiKey ? 'API Key 已配置' : '未配置 API Key'
  return `${value.provider} · ${value.model || '未选择模型'} · ${key}`
}
