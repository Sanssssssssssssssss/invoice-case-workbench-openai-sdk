import { describe, expect, it, vi } from 'vitest'
import { getBaseUrl } from './api'
import { loadAgentSettings, persistAgentSettings, selectProvider, settingsDraft, settingsInput } from './agentSettings'

const publicSettings = {
  provider: 'deepseek' as const,
  baseUrl: 'https://api.deepseek.com',
  model: 'deepseek-v4-flash',
  thinking: 'high' as const,
  maxSteps: 10,
  contextChars: 200000,
  hasApiKey: true
}

it('wires settings through desktop IPC without receiving a secret', async () => {
  const bridge = {
    getAgentSettings: vi.fn(async () => publicSettings),
    saveAgentSettings: vi.fn(async () => ({ settings: publicSettings, backend: { baseUrl: 'http://127.0.0.1:8765', port: 8765, logPath: 'backend.log' } }))
  }
  expect(await loadAgentSettings(bridge)).toEqual(publicSettings)
  const input = settingsInput({ ...settingsDraft(publicSettings), apiKey: 'new-secret', apiKeyAction: 'replace' })
  const result = await persistAgentSettings(input, bridge)
  expect(bridge.saveAgentSettings).toHaveBeenCalledWith(expect.objectContaining({ apiKeyAction: 'replace', apiKey: 'new-secret' }))
  expect(result.settings).not.toHaveProperty('apiKey')
  expect(await getBaseUrl()).toBe('http://127.0.0.1:8765')
})

describe('provider presets', () => {
  it('selects the supported CommandCode endpoint and model', () => {
    expect(selectProvider(settingsDraft(publicSettings), 'commandcode')).toMatchObject({
      provider: 'commandcode',
      baseUrl: 'https://api.commandcode.ai/provider/v1',
      model: 'deepseek/deepseek-v4-flash'
    })
  })
})
