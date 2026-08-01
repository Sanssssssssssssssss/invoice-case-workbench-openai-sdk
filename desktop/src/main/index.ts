import { app, BrowserWindow, ipcMain, shell } from 'electron'
import { join } from 'node:path'
import { startBackend, type BackendHandle } from './backendProcess.js'
import { isAllowedExternalUrl } from './externalLinks.js'

let mainWindow: BrowserWindow | null = null
let backend: BackendHandle | null = null
let isQuitting = false

interface CaseFileMetadata {
  absolute_path: string
  path: string
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

  if (!app.isPackaged && process.env.ELECTRON_RENDERER_URL) {
    mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }

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

function registerBackendIpc(handle: BackendHandle): void {
  ipcMain.handle('backend:get-info', () => ({
    baseUrl: handle.baseUrl,
    port: handle.port,
    logPath: handle.logPath
  }))

  ipcMain.handle('case-file:open', async (_event, caseId: string, path: string) => {
    const metadata = await resolveCaseFile(handle, caseId, path)
    const error = await shell.openPath(metadata.absolute_path)
    if (error) {
      throw new Error(error)
    }
  })

  ipcMain.handle('case-file:show-in-folder', async (_event, caseId: string, path: string) => {
    const metadata = await resolveCaseFile(handle, caseId, path)
    shell.showItemInFolder(metadata.absolute_path)
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
  backend = await startBackend()
  registerBackendIpc(backend)
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
