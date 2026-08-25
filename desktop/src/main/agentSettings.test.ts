import assert from 'node:assert/strict'
import test from 'node:test'

// @ts-expect-error Node's test runner loads this TypeScript module directly.
import { publicAgentSettings, validateAgentSettings } from '../shared/agentSettings.ts'

test('validates supported settings and normalizes the Base URL', () => {
  const value = validateAgentSettings({
    provider: 'commandcode',
    baseUrl: 'https://api.commandcode.ai/provider/v1/',
    model: 'deepseek/deepseek-v4-flash',
    thinking: 'high',
    maxSteps: 12,
    contextChars: 240000,
    apiKeyAction: 'replace',
    apiKey: 'secret'
  })
  assert.equal(value.baseUrl, 'https://api.commandcode.ai/provider/v1')
  assert.throws(() => validateAgentSettings({ ...value, baseUrl: 'file:///tmp/model' }), /HTTP/)
  assert.throws(() => validateAgentSettings({ ...value, model: 'wrong-model' }), /CommandCode/)
})

test('public projection reports key presence without exposing the key', () => {
  const projected = publicAgentSettings({
    provider: 'deepseek', baseUrl: 'https://api.deepseek.com', model: 'deepseek-v4-flash',
    thinking: 'high', maxSteps: 10, contextChars: 200000, apiKey: 'must-not-leak'
  }, true)
  assert.equal(projected.hasApiKey, true)
  assert.equal('apiKey' in projected, false)
  assert.equal(JSON.stringify(projected).includes('must-not-leak'), false)
})
