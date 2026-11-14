import { defineConfig } from '@playwright/test';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const hub = fileURLToPath(new URL('../hub/', import.meta.url));
const venvPython = fileURLToPath(new URL(process.platform === 'win32'
  ? '../hub/.venv/Scripts/python.exe' : '../hub/.venv/bin/python', import.meta.url));
const python = process.env.HOMEMUSIC_TEST_PYTHON || (existsSync(venvPython) ? venvPython : 'python3');

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:15173',
    browserName: 'chromium',
    launchOptions: {
      ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}),
      args: ['--autoplay-policy=no-user-gesture-required'],
    },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: [
    { command: `"${python}" tests/browser_server.py`, cwd: hub,
      url: 'http://127.0.0.1:18080/healthz', timeout: 60_000, reuseExistingServer: false },
    { command: 'npm run dev -- --host 127.0.0.1 --port 15173 --strictPort',
      env: { HOMEMUSIC_DEV_BACKEND: 'http://127.0.0.1:18080' },
      url: 'http://127.0.0.1:15173', timeout: 30_000, reuseExistingServer: false },
  ],
});
