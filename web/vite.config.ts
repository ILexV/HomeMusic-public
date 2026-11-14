import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mockApi } from './mock/plugin.ts';

const here = fileURLToPath(new URL('.', import.meta.url));
const outDir = resolve(here, '../hub/app/static');

/** One id per build: the service worker cache, `<meta name="hm-build">` in index.html (the Hub reports it in
 * /healthz) and `__HM_BUILD__` in the bundle (lib/update.ts reloads the page when the Hub serves another one). */
const BUILD_ID = new Date().toISOString().replace(/[-:.TZ]/g, '');

function buildId(): Plugin {
  return {
    name: 'homemusic-build-id',
    apply: 'build',
    transformIndexHtml: () => [{ tag: 'meta', attrs: { name: 'hm-build', content: BUILD_ID }, injectTo: 'head' }],
    closeBundle() {
      const file = resolve(outDir, 'sw.js');
      if (!existsSync(file)) return;
      writeFileSync(file, readFileSync(file, 'utf8').replace('__BUILD_ID__', BUILD_ID));
    },
  };
}

const backend = process.env.HOMEMUSIC_DEV_BACKEND || 'http://localhost:8080';

export default defineConfig(({ command, mode }) => ({
  plugins: [react(), tailwindcss(), buildId(), ...(mode === 'mock' ? [mockApi()] : [])],
  define: { __HM_BUILD__: JSON.stringify(command === 'build' ? BUILD_ID : 'dev') },
  build: {
    outDir,
    emptyOutDir: true,
    target: 'es2022',
    assetsDir: 'assets',
  },
  server: {
    port: 5173,
    proxy:
      mode === 'mock'
        ? undefined
        : {
            '/api': { target: backend, changeOrigin: false },
            '/share-target': { target: backend, changeOrigin: false },
            '/healthz': { target: backend, changeOrigin: false },
          },
  },
}));
