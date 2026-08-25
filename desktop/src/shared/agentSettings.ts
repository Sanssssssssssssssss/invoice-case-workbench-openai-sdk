export const AGENT_PROVIDERS = ['openai', 'deepseek', 'commandcode', 'compatible'] as const
export const THINKING_MODES = ['disabled', 'enabled', 'low', 'high', 'max'] as const

export type AgentProvider = (typeof AGENT_PROVIDERS)[number]
export type ThinkingMode = (typeof THINKING_MODES)[number]
export type ApiKeyAction = 'keep' | 'replace' | 'clear'

export interface AgentSettingsPublic {
  provider: AgentProvider
  baseUrl: string
  model: string
  thinking: ThinkingMode
  maxSteps: number
  contextChars: number
  hasApiKey: boolean
}

export interface SaveAgentSettingsInput extends Omit<AgentSettingsPublic, 'hasApiKey'> {
  apiKeyAction: ApiKeyAction
  apiKey?: string
}

export interface SavedAgentSettingsResult {
  settings: AgentSettingsPublic
  backend: { baseUrl: string; port: number; logPath: string }
}

export function publicAgentSettings(
  value: Omit<AgentSettingsPublic, 'hasApiKey'> & { apiKey?: unknown },
  hasApiKey: boolean
): AgentSettingsPublic {
  return {
    provider: value.provider,
    baseUrl: value.baseUrl,
    model: value.model,
    thinking: value.thinking,
    maxSteps: value.maxSteps,
    contextChars: value.contextChars,
    hasApiKey
  }
}

export function validateAgentSettings(input: SaveAgentSettingsInput): SaveAgentSettingsInput {
  if (!AGENT_PROVIDERS.includes(input.provider)) throw new Error('不支持的 Provider')
  const baseUrl = input.baseUrl.trim().replace(/\/+$/, '')
  const parsed = new URL(baseUrl)
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error('Base URL 必须是无凭据的 HTTP(S) 地址')
  if (parsed.search || parsed.hash) throw new Error('Base URL 不能包含 query 或 fragment')
  const model = input.model.trim()
  if (!model || model.length > 160 || /[\r\n]/.test(model)) throw new Error('Model 必须为 1-160 个字符')
  if (input.provider === 'commandcode' && (baseUrl !== 'https://api.commandcode.ai/provider/v1' || model !== 'deepseek/deepseek-v4-flash')) throw new Error('CommandCode Provider 必须使用其 DeepSeek V4 Flash endpoint 和 model')
  if (!THINKING_MODES.includes(input.thinking)) throw new Error('不支持的 thinking 模式')
  if (!Number.isInteger(input.maxSteps) || input.maxSteps < 1 || input.maxSteps > 100) throw new Error('最大步数必须在 1-100 之间')
  if (!Number.isInteger(input.contextChars) || input.contextChars < 1_000 || input.contextChars > 2_000_000) throw new Error('Context 字符预算必须在 1,000-2,000,000 之间')
  if (!['keep', 'replace', 'clear'].includes(input.apiKeyAction)) throw new Error('不支持的 API Key 操作')
  const apiKey = input.apiKey?.trim() || ''
  if (input.apiKeyAction === 'replace' && (!apiKey || apiKey.length > 8192 || /[\r\n]/.test(apiKey))) throw new Error('API Key 无效')
  return { ...input, baseUrl, model, apiKey: input.apiKeyAction === 'replace' ? apiKey : undefined }
}
