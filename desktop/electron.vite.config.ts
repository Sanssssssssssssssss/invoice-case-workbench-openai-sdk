import { resolve } from 'node:path'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import react from '@vitejs/plugin-react'

const root = resolve(__dirname, '..')

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    build: {
      rollupOptions: {
        input: resolve(__dirname, 'src/main/index.ts')
      }
    }
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    build: {
      rollupOptions: {
        input: resolve(__dirname, 'src/preload/index.ts')
      }
    }
  },
  renderer: {
    root: resolve(root, 'renderer'),
    plugins: [react()],
    build: {
      rollupOptions: {
        input: resolve(root, 'renderer/index.html')
      }
    },
    server: {
      host: '127.0.0.1',
      watch: {
        ignored: ['**/workspace/**', '**/tmp/**', '**/.pytest_cache/**', '**/backend/storage/**']
      }
    },
    resolve: {
      alias: {
        '@': resolve(root, 'renderer/src')
      }
    }
  }
})
