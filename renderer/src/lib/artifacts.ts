import type { ArtifactItem } from '@/types'
import { absoluteApiUrl } from './api'

export type ArtifactAction = 'open' | 'show' | 'download'

export interface ArtifactRuntime {
  cockpit?: Window['cockpit']
  openExternal?: (url: string) => void
  downloadExternal?: (url: string) => void
  resolveUrl?: (path: string) => Promise<string>
}

export function latestReportArtifacts(artifacts: ArtifactItem[]): ArtifactItem[] {
  return artifacts
    .filter((item) => item.type === 'report' || item.path.startsWith('reports/'))
    .slice()
    .sort((left, right) => reportPriority(left) - reportPriority(right) || Date.parse(right.updated_at || '') - Date.parse(left.updated_at || ''))
    .slice(0, 6)
}

export function isPdfArtifact(item: ArtifactItem): boolean {
  return item.path.toLowerCase().endsWith('.pdf') || item.content_type === 'application/pdf'
}

function reportPriority(item: ArtifactItem): number {
  if (isPdfArtifact(item)) return 0
  if (item.path.toLowerCase().endsWith('.md') || item.content_type === 'text/markdown') return 1
  return 2
}

export async function runArtifactAction(caseId: string, item: ArtifactItem, action: ArtifactAction, runtime: ArtifactRuntime = {}) {
  const browserWindow = typeof window === 'undefined' ? undefined : window
  const cockpit = runtime.cockpit ?? browserWindow?.cockpit
  const resolveUrl = runtime.resolveUrl ?? absoluteApiUrl
  if (action === 'open' && cockpit?.openCaseFile && caseId) {
    await cockpit.openCaseFile(caseId, item.path)
    return 'electron-open'
  }
  if (action === 'show' && cockpit?.showCaseFileInFolder && caseId) {
    await cockpit.showCaseFileInFolder(caseId, item.path)
    return 'electron-show'
  }

  const fallbackPath = action === 'download' || action === 'show' ? item.download_url || item.open_url : item.open_url || item.download_url
  const url = await resolveUrl(fallbackPath)
  if (action === 'download') {
    ;(runtime.downloadExternal ?? ((target) => browserWindow?.location.assign(target)))(url)
    return 'browser-download'
  }
  ;(runtime.openExternal ?? ((target) => browserWindow?.open(target, '_blank')))(url)
  return 'browser-open'
}
