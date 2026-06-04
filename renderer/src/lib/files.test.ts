import { describe, expect, it } from 'vitest'
import { dataTransferHasFiles, mergeFiles } from './files'

describe('file helpers', () => {
  it('merges dropped files without duplicating the same file instance identity', () => {
    const first = { name: 'invoice.pdf', size: 1200, lastModified: 10 }
    const duplicate = { name: 'invoice.pdf', size: 1200, lastModified: 10 }
    const second = { name: 'notes.md', size: 24, lastModified: 11 }

    expect(mergeFiles([first], [duplicate, second])).toEqual([first, second])
  })

  it('distinguishes files with the same name and size but different modified time', () => {
    const first = { name: 'invoice.pdf', size: 1200, lastModified: 10 }
    const newer = { name: 'invoice.pdf', size: 1200, lastModified: 20 }

    expect(mergeFiles([first], [newer])).toEqual([first, newer])
  })

  it('detects file drops and ignores non-file drops', () => {
    expect(dataTransferHasFiles({ types: ['Files'] as unknown as DataTransfer['types'] })).toBe(true)
    expect(dataTransferHasFiles({ types: ['text/plain'] as unknown as DataTransfer['types'] })).toBe(false)
  })
})
