import { defineConfig } from '@playwright/test'

// e2e runs against the built UI (vite preview) proxied to a real FastAPI backend started by webServer
// (fixtures/recorded data sources, so no containers are needed).
const backendPort = 8081
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:4173',
    trace: 'retain-on-failure',
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROME_PATH || undefined },
  },
  webServer: [
    {
      command:
        `${process.env.BACKEND_PYTHON || '../backend/.venv/bin/python'} -m uvicorn app.api.app:app_factory --factory --port ${backendPort} --app-dir ../backend`,
      url: `http://localhost:${backendPort}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: {
        MEDSAFE_FHIR_MODE: 'fixtures',
        MEDSAFE_VISTA_MODE: 'recorded',
        MEDSAFE_AUDIT_DB: ':memory:',
        MEDSAFE_LOG_LEVEL: 'WARNING',
        MEDSAFE_RATE_LIMIT_PER_MINUTE: '100000',
        MEDSAFE_CORS_ORIGINS: 'http://localhost:4173',
      },
    },
    {
      command: `VITE_BACKEND_URL=http://localhost:${backendPort} npm run build && VITE_BACKEND_URL=http://localhost:${backendPort} npm run preview`,
      url: 'http://localhost:4173',
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
})
