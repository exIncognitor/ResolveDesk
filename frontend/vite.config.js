import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  publicDir: false,
  // Preserve adjacent history, and never copy it from public/ into distributable assets.
  build: { emptyOutDir: false },
  // The backend supplies the production config; Vite gets an explicit dev-only endpoint.
  define: { 'window.__RESOLVEDESK_DEV_API__': JSON.stringify('/api/python') },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api/python': {
        target: process.env.VITE_PYTHON_API_URL || 'http://127.0.0.1:8080',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/python/, '')
      },
      '/api/java': {
        target: process.env.VITE_JAVA_API_URL || 'http://localhost:8080',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/java/, '')
      }
    }
  }
})
