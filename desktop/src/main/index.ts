import { app, BrowserWindow, ipcMain, safeStorage, shell } from 'electron'
import { join } from 'node:path'
import { startBackend, type BackendHandle } from './backendProcess.js'
import { isAllowedExternalUrl } from './externalLinks.js'
import { AgentSettingsStore } from './agentSettingsStore.js'
import type { SaveAgentSettingsInput } from '../shared/agentSettings.js'

let mainWindow: BrowserWindow | null = null
let backend: BackendHandle | null = null
let agentSettings: AgentSettingsStore | null = null
let isQuitting = false

const RENDERER_LOAD_ATTEMPTS = 40
const RENDERER_LOAD_RETRY_MS = 250

interface CaseFileMetadata {
  absolute_path: string
  path: string
}

async function loadRenderer(window: BrowserWindow): Promise<void> {
  if (app.isPackaged || !process.env.ELECTRON_RENDERER_URL) {
    await window.loadFile(join(__dirname, '../renderer/index.html'))
    return
  }

  let lastError: unknown
  for (let attempt = 0; attempt < RENDERER_LOAD_ATTEMPTS; attempt += 1) {
    if (window.isDestroyed()) return
    try {
      await window.loadURL(process.env.ELECTRON_RENDERER_URL)
      return
    } catch (error) {
      lastError = error
      await new Promise((resolve) => setTimeout(resolve, RENDERER_LOAD_RETRY_MS))
    }
  }
  throw lastError
}

function createWindow(handle: BackendHandle): void {
  mainWindow = new BrowserWindow({
    width: 1536,
    height: 1024,
    minWidth: 980,
    minHeight: 680,
    show: false,
    frame: false,
    titleBarStyle: 'hidden',
    backgroundColor: '#f7f9fb',
    webPreferences: {
      preload: join(__dirname, '../preload/index.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })

  mainWindow.once('ready-to-show', () => {
    mainWindow?.show()
  })

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowedExternalUrl(url)) {
      void shell.openExternal(url)
    }
    return { action: 'deny' }
  })

  void loadRenderer(mainWindow).catch((error) => {
    console.error('Failed to load renderer after retries', error)
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

function registerWindowIpc(): void {
  ipcMain.handle('window:control', (_event, action: 'minimize' | 'maximize' | 'close') => {
    if (!mainWindow) return
    if (action === 'minimize') {
      mainWindow.minimize()
    } else if (action === 'maximize') {
      if (mainWindow.isMaximized()) mainWindow.unmaximize()
      else mainWindow.maximize()
    } else if (action === 'close') {
      mainWindow.close()
    }
  })
}

function activeBackend(): BackendHandle {
  if (!backend) throw new Error('Backend is not running')
  return backend
}

function registerBackendIpc(): void {
  ipcMain.handle('backend:get-info', () => ({
    baseUrl: activeBackend().baseUrl,
    port: activeBackend().port,
    logPath: activeBackend().logPath
  }))

  ipcMain.handle('case-file:open', async (_event, caseId: string, path: string) => {
    const metadata = await resolveCaseFile(activeBackend(), caseId, path)
    const error = await shell.openPath(metadata.absolute_path)
    if (error) {
      throw new Error(error)
    }
  })

  ipcMain.handle('case-file:show-in-folder', async (_event, caseId: string, path: string) => {
    const metadata = await resolveCaseFile(activeBackend(), caseId, path)
    shell.showItemInFolder(metadata.absolute_path)
  })

  ipcMain.handle('agent-settings:get', () => {
    if (!agentSettings) throw new Error('Agent settings are not ready')
    return agentSettings.public()
  })

  ipcMain.handle('agent-settings:save', async (_event, input: SaveAgentSettingsInput) => {
    if (!agentSettings) throw new Error('Agent settings are not ready')
    const candidate = agentSettings.candidate(input)
    const replacement = await startBackend(agentSettings.backendEnv(candidate))
    try {
      agentSettings.save(candidate)
    } catch (error) {
      replacement.stop()
      throw error
    }
    const previous = backend
    backend = replacement
    previous?.stop()
    return {
      settings: agentSettings.public(candidate),
      backend: { baseUrl: replacement.baseUrl, port: replacement.port, logPath: replacement.logPath }
    }
  })
}

async function resolveCaseFile(handle: BackendHandle, caseId: string, path: string): Promise<CaseFileMetadata> {
  if (!caseId || typeof caseId !== 'string') {
    throw new Error('Missing case id')
  }
  if (!path || typeof path !== 'string') {
    throw new Error('Missing case file path')
  }
  const url = new URL(`/api/cases/${encodeURIComponent(caseId)}/files/metadata`, handle.baseUrl)
  url.searchParams.set('path', path)
  const response = await fetch(url)
  if (!response.ok) {
    const body = await response.text()
    throw new Error(`Case file validation failed: ${response.status} ${body}`)
  }
  const metadata = (await response.json()) as Partial<CaseFileMetadata>
  if (!metadata.absolute_path || typeof metadata.absolute_path !== 'string') {
    throw new Error('Case file metadata did not include an absolute path')
  }
  if (metadata.path !== path) {
    throw new Error('Case file metadata path mismatch')
  }
  return metadata as CaseFileMetadata
}

app.whenReady().then(async () => {
  registerWindowIpc()
  agentSettings = new AgentSettingsStore(
    join(app.getPath('userData'), 'agent-settings.json'),
    {
      available: () => safeStorage.isEncryptionAvailable(),
      encrypt: (value) => safeStorage.encryptString(value),
      decrypt: (value) => safeStorage.decryptString(value)
    }
  )
  backend = await startBackend(agentSettings.backendEnv())
  registerBackendIpc()
  createWindow(backend)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0 && backend) {
      createWindow(backend)
    }
  })
})

app.on('before-quit', () => {
  isQuitting = true
  backend?.stop()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin' || isQuitting) {
    backend?.stop()
    app.quit()
  }
})
