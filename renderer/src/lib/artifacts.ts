import type { ArtifactItem } from '@/types'
import { absoluteApiUrl } from './api'

export type ArtifactAction = 'open' | 'show' | 'download'

export interface ArtifactRuntime {
  cockpit?: Window['cockpit']
  openExternal?: (url: string) => void
  downloadExternal?: (url: string) => void
  resolveUrl?: (path: string) => Promise<string>
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
