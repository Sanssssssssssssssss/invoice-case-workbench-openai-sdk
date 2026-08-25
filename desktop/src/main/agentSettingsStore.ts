import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import type { AgentProvider, AgentSettingsPublic, SaveAgentSettingsInput, ThinkingMode } from '../shared/agentSettings.js'
import { publicAgentSettings, validateAgentSettings } from '../shared/agentSettings.js'

interface PersistedAgentSettings {
  version: 1
  provider: AgentProvider
  baseUrl: string
  model: string
  thinking: ThinkingMode
  maxSteps: number
  contextChars: number
  apiKeyMode: 'inherit' | 'encrypted' | 'clear'
  encryptedApiKey?: string
}

export interface SecretCipher {
  available: () => boolean
  encrypt: (value: string) => Buffer
  decrypt: (value: Buffer) => string
}

export class AgentSettingsStore {
  private readonly path: string
  private readonly cipher: SecretCipher
  private readonly inheritedEnv: NodeJS.ProcessEnv

  constructor(
    path: string,
    cipher: SecretCipher,
    inheritedEnv: NodeJS.ProcessEnv = process.env
  ) {
    this.path = path
    this.cipher = cipher
    this.inheritedEnv = inheritedEnv
  }

  load(): PersistedAgentSettings {
    try {
      return persisted(JSON.parse(readFileSync(this.path, 'utf8')))
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
      return this.defaults()
    }
  }

  candidate(input: SaveAgentSettingsInput): PersistedAgentSettings {
    const value = validateAgentSettings(input)
    const current = this.load()
    if (value.apiKeyAction === 'replace' && !this.cipher.available()) throw new Error('系统安全存储当前不可用，无法保存 API Key')
    return {
      version: 1,
      provider: value.provider,
      baseUrl: value.baseUrl,
      model: value.model,
      thinking: value.thinking,
      maxSteps: value.maxSteps,
      contextChars: value.contextChars,
      apiKeyMode: value.apiKeyAction === 'keep' ? current.apiKeyMode : value.apiKeyAction === 'clear' ? 'clear' : 'encrypted',
      encryptedApiKey: value.apiKeyAction === 'keep'
        ? current.encryptedApiKey
        : value.apiKeyAction === 'replace'
          ? this.cipher.encrypt(value.apiKey!).toString('base64')
          : undefined
    }
  }

  save(value: PersistedAgentSettings): void {
    mkdirSync(dirname(this.path), { recursive: true })
    const temporary = `${this.path}.tmp`
    writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 })
    renameSync(temporary, this.path)
  }

  public(value: PersistedAgentSettings = this.load()): AgentSettingsPublic {
    return publicAgentSettings(value, value.apiKeyMode === 'encrypted' || (value.apiKeyMode === 'inherit' && Boolean(this.inheritedEnv.LLM_API_KEY)))
  }

  backendEnv(value: PersistedAgentSettings = this.load()): NodeJS.ProcessEnv {
    const env: NodeJS.ProcessEnv = {
      LLM_PROVIDER: ['openai', 'deepseek'].includes(value.provider) ? value.provider : 'compatible',
      LLM_BASE_URL: value.baseUrl,
      LLM_MODEL: value.model,
      LLM_THINKING_TYPE: value.thinking,
      INVOICE_AGENT_MAX_STEPS: String(value.maxSteps),
      INVOICE_AGENT_CONTEXT_CHAR_LIMIT: String(value.contextChars)
    }
    if (value.apiKeyMode === 'clear') env.LLM_API_KEY = ''
    if (value.apiKeyMode === 'encrypted') {
      if (!value.encryptedApiKey || !this.cipher.available()) throw new Error('无法解密已保存的 API Key')
      env.LLM_API_KEY = this.cipher.decrypt(Buffer.from(value.encryptedApiKey, 'base64'))
    }
    return env
  }

  private defaults(): PersistedAgentSettings {
    const baseUrl = this.inheritedEnv.LLM_BASE_URL?.trim() || 'https://api.openai.com/v1'
    const commandCode = baseUrl.replace(/\/+$/, '') === 'https://api.commandcode.ai/provider/v1'
    const inheritedProvider = (this.inheritedEnv.LLM_PROVIDER || 'openai').toLowerCase()
    return {
      version: 1,
      provider: commandCode
        ? 'commandcode'
        : ['openai', 'deepseek'].includes(inheritedProvider)
          ? inheritedProvider as AgentProvider
          : 'compatible',
      baseUrl,
      model: this.inheritedEnv.LLM_MODEL?.trim() || (commandCode ? 'deepseek/deepseek-v4-flash' : 'gpt-4.1-mini'),
      thinking: thinkingMode(this.inheritedEnv.LLM_THINKING_TYPE),
      maxSteps: integerEnv(this.inheritedEnv.INVOICE_AGENT_MAX_STEPS, 10),
      contextChars: integerEnv(this.inheritedEnv.INVOICE_AGENT_CONTEXT_CHAR_LIMIT, 200_000),
      apiKeyMode: 'inherit'
    }
  }
}

function persisted(value: unknown): PersistedAgentSettings {
  if (!value || typeof value !== 'object') throw new Error('Agent settings file is invalid')
  const raw = value as Record<string, unknown>
  const checked = validateAgentSettings({
    provider: raw.provider as AgentProvider,
    baseUrl: String(raw.baseUrl || ''),
    model: String(raw.model || ''),
    thinking: raw.thinking as ThinkingMode,
    maxSteps: Number(raw.maxSteps),
    contextChars: Number(raw.contextChars),
    apiKeyAction: 'keep'
  })
  const apiKeyMode = raw.apiKeyMode
  if (!['inherit', 'encrypted', 'clear'].includes(String(apiKeyMode))) throw new Error('Agent settings API Key mode is invalid')
  if (apiKeyMode === 'encrypted' && typeof raw.encryptedApiKey !== 'string') throw new Error('Encrypted API Key is missing')
  return {
    version: 1,
    provider: checked.provider,
    baseUrl: checked.baseUrl,
    model: checked.model,
    thinking: checked.thinking,
    maxSteps: checked.maxSteps,
    contextChars: checked.contextChars,
    apiKeyMode: apiKeyMode as PersistedAgentSettings['apiKeyMode'],
    encryptedApiKey: typeof raw.encryptedApiKey === 'string' ? raw.encryptedApiKey : undefined
  }
}

function thinkingMode(value: string | undefined): ThinkingMode {
  return ['disabled', 'enabled', 'low', 'high', 'max'].includes(value || '') ? value as ThinkingMode : 'enabled'
}

function integerEnv(value: string | undefined, fallback: number) {
  const parsed = Number(value)
  return Number.isInteger(parsed) ? parsed : fallback
}
