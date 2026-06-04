import { contextBridge, ipcRenderer } from 'electron'

export interface BackendInfo {
  baseUrl: string
  port: number
  logPath: string
}

const api = {
  getBackendInfo: (): Promise<BackendInfo> => ipcRenderer.invoke('backend:get-info'),
  windowControl: (action: 'minimize' | 'maximize' | 'close'): Promise<void> => ipcRenderer.invoke('window:control', action),
  openCaseFile: (caseId: string, path: string): Promise<void> => ipcRenderer.invoke('case-file:open', caseId, path),
  showCaseFileInFolder: (caseId: string, path: string): Promise<void> => ipcRenderer.invoke('case-file:show-in-folder', caseId, path)
}

contextBridge.exposeInMainWorld('cockpit', api)

export type CockpitApi = typeof api
