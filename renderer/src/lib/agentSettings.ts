import type { AgentProvider, AgentSettingsPublic, ApiKeyAction, SaveAgentSettingsInput, SavedAgentSettingsResult } from '../../../desktop/src/shared/agentSettings'
import { setBackendBaseUrl } from './api'

export interface AgentSettingsDraft extends Omit<AgentSettingsPublic, 'maxSteps' | 'contextChars'> {
  maxSteps: string
  contextChars: string
  apiKey: string
  apiKeyAction: ApiKeyAction
}

type SettingsBridge = Pick<NonNullable<Window['cockpit']>, 'getAgentSettings' | 'saveAgentSettings'>

export async function loadAgentSettings(bridge: SettingsBridge | undefined = window.cockpit) {
  if (!bridge) throw new Error('Agent 设置仅在桌面应用中可用')
  return bridge.getAgentSettings()
}

export async function persistAgentSettings(input: SaveAgentSettingsInput, bridge: SettingsBridge | undefined = window.cockpit) {
  if (!bridge) throw new Error('Agent 设置仅在桌面应用中可用')
  const result = await bridge.saveAgentSettings(input)
  setBackendBaseUrl(result.backend.baseUrl)
  return result
}

export function settingsDraft(value: AgentSettingsPublic): AgentSettingsDraft {
  return { ...value, maxSteps: String(value.maxSteps), contextChars: String(value.contextChars), apiKey: '', apiKeyAction: 'keep' }
}

export function settingsInput(value: AgentSettingsDraft): SaveAgentSettingsInput {
  return {
    provider: value.provider,
    baseUrl: value.baseUrl,
    model: value.model,
    thinking: value.thinking,
    maxSteps: Number(value.maxSteps),
    contextChars: Number(value.contextChars),
    apiKeyAction: value.apiKeyAction,
    apiKey: value.apiKeyAction === 'replace' ? value.apiKey : undefined
  }
}

export function selectProvider(value: AgentSettingsDraft, provider: AgentProvider): AgentSettingsDraft {
  if (provider === 'openai') return { ...value, provider, baseUrl: 'https://api.openai.com/v1', model: 'gpt-4.1-mini' }
  if (provider === 'deepseek') return { ...value, provider, baseUrl: 'https://api.deepseek.com', model: 'deepseek-v4-flash' }
  if (provider === 'commandcode') return { ...value, provider, baseUrl: 'https://api.commandcode.ai/provider/v1', model: 'deepseek/deepseek-v4-flash' }
  return { ...value, provider, baseUrl: '', model: '' }
}

export type { AgentProvider, AgentSettingsPublic, SavedAgentSettingsResult }
