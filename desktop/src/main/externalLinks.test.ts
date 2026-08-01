import assert from 'node:assert/strict'
import test from 'node:test'

// @ts-expect-error Node's test runner loads this TypeScript module directly.
import { isAllowedExternalUrl } from './externalLinks.ts'

test('external links only allow http and https', () => {
  assert.equal(isAllowedExternalUrl('https://example.com/report'), true)
  assert.equal(isAllowedExternalUrl('http://127.0.0.1:8010/health'), true)
  assert.equal(isAllowedExternalUrl('file:///C:/Windows/System32/drivers/etc/hosts'), false)
  assert.equal(isAllowedExternalUrl('javascript:alert(1)'), false)
  assert.equal(isAllowedExternalUrl('not a url'), false)
})
