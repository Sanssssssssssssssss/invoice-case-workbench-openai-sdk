import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { settingsDraft } from '@/lib/agentSettings'
import { AgentSettings, SettingsButton, settingsSummary } from './AgentSettings'

describe('Agent settings', () => {
  it('renders a clear settings entry', () => {
    const html = renderToStaticMarkup(<SettingsButton onClick={() => undefined} />)
    expect(html).toContain('aria-label="Agent 设置"')
    expect(html).toContain('设置')
  })

  it('keeps the API key masked and out of the public summary', () => {
    const value = { ...settingsDraft({ provider: 'deepseek', baseUrl: 'https://api.deepseek.com', model: 'test-model', thinking: 'high', maxSteps: 10, contextChars: 200000, hasApiKey: true }), apiKey: 'secret-never-log', apiKeyAction: 'replace' as const }
    const html = renderToStaticMarkup(<AgentSettings open value={value} onChange={() => undefined} onClose={() => undefined} onSave={() => undefined} agentRunning={false} loading={false} saving={false} error="" saved={false} />)
    expect(html).toContain('type="password"')
    expect(settingsSummary(value)).toContain('API Key 将替换')
    expect(settingsSummary(value)).not.toContain(value.apiKey)
  })

  it('disables restart while an Agent run is active', () => {
    const value = settingsDraft({ provider: 'deepseek', baseUrl: 'https://api.deepseek.com', model: 'deepseek-v4-flash', thinking: 'high', maxSteps: 10, contextChars: 200000, hasApiKey: true })
    const html = renderToStaticMarkup(<AgentSettings open value={value} onChange={() => undefined} onClose={() => undefined} onSave={() => undefined} agentRunning loading={false} saving={false} error="" saved={false} />)
    expect(html).toContain('当前 Agent 正在运行')
    expect(html).toContain('disabled=""')
  })
})
