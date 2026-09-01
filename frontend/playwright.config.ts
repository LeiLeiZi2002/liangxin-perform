import { defineConfig, devices } from '@playwright/test'

const onWindows = process.platform === 'win32'
const backendCommand = onWindows
  ? 'pwsh -NoProfile -File ../scripts/start-e2e-server.ps1 -Service backend'
  : 'cd ../backend && ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000'
const frontendCommand = onWindows
  ? 'pwsh -NoProfile -File ../scripts/start-e2e-server.ps1 -Service frontend'
  : 'npm run dev -- --host 127.0.0.1 --port 5173 --strictPort'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], channel: process.env.E2E_BROWSER_CHANNEL ?? 'chrome' } },
  ],
  webServer: [
    {
      command: backendCommand,
      env: {
        ...process.env,
        DATABASE_URL: 'sqlite:///../data/e2e.db',
        FRONTEND_ORIGIN: 'http://127.0.0.1:5173',
      },
      url: 'http://127.0.0.1:8000/api/health',
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: frontendCommand,
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
  ],
})
