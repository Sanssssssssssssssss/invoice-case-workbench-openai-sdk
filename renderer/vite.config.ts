import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    watch: {
      ignored: ['**/workspace/**', '**/tmp/**', '**/.pytest_cache/**', '**/backend/storage/**']
    }
  },
  resolve: {
    alias: {
      '@': '/src'
    }
  }
})
