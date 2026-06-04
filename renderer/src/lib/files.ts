export interface FileLike {
  name: string
  size: number
  lastModified?: number
}

export function fileKey(file: FileLike) {
  return `${file.name}:${file.size}:${file.lastModified ?? 0}`
}

export function mergeFiles<T extends FileLike>(current: T[], incoming: Iterable<T> | ArrayLike<T> | null | undefined) {
  if (!incoming) return current
  const existing = new Set(current.map(fileKey))
  const next = [...current]
  for (const file of Array.from(incoming)) {
    const key = fileKey(file)
    if (!existing.has(key)) {
      existing.add(key)
      next.push(file)
    }
  }
  return next
}

export function dataTransferHasFiles(dataTransfer: Pick<DataTransfer, 'types'> | null | undefined) {
  return Boolean(dataTransfer && Array.from(dataTransfer.types).includes('Files'))
}
