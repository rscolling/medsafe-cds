import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies API calls to the FastAPI backend (default http://localhost:8080).
const backend = process.env.VITE_BACKEND_URL ?? 'http://localhost:8080'

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { '/api': backend, '/cds-services': backend, '/health': backend } },
  preview: { port: 4173, proxy: { '/api': backend, '/cds-services': backend, '/health': backend } },
})
