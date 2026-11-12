import assert from 'node:assert/strict';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { build } from 'vite';

// Bundle the real entry and its imports: a secure-context-only call at module
// scope used to blank the entire app before even the login screen appeared.
const built = await build({
  configFile: false,
  logLevel: 'silent',
  build: {
    write: false,
    minify: false,
    lib: {
      entry: fileURLToPath(new URL('../src/lib/sharedPlayback.ts', import.meta.url)),
      name: 'SharedPlayback',
      formats: ['iife'],
    },
  },
});
const bundle = (Array.isArray(built) ? built[0] : built).output.find(item => item.type === 'chunk').code;

function httpTab() {
  const requests = [];
  const context = vm.createContext({
    crypto: {}, // LAN HTTP exposes no crypto.randomUUID.
    console, performance, AbortController, URLSearchParams,
    setTimeout, clearTimeout, setInterval, clearInterval,
    process: { env: { NODE_ENV: 'production' } },
    window: { addEventListener() {}, setInterval },
    document: { addEventListener() {} },
    localStorage: { length: 0, getItem: () => null, removeItem() {} },
    fetch: async (path, options) => {
      const body = JSON.parse(options.body);
      requests.push({ path, body });
      return Response.json({
        device_id: body.device_id, owner: null, revision: 0,
        state: null, handoff: null, commands: [], devices: [], error: null,
      });
    },
  });
  vm.runInContext(bundle, context);
  return { playback: context.SharedPlayback, requests };
}

const adapter = {
  snapshot: () => null,
  pause: () => assert.fail('Registering an observer must not pause playback'),
  execute: () => assert.fail('No remote commands were issued'),
  unlock: async () => {},
};

test('LAN HTTP tabs register distinct sessions and retain identity across reconnects', async () => {
  const ids = [];
  for (let tab = 0; tab < 2; tab++) {
    const { playback, requests } = httpTab();
    for (let connection = 0; connection < 2; connection++) {
      try {
        playback.startSharedPlayback(adapter, 1);
        await setImmediate();
      } finally {
        playback.stopSharedPlayback();
      }
    }
    assert.equal(requests.length, 2);
    for (const request of requests) {
      assert.equal(request.path, '/api/playback/heartbeat');
      assert.equal(request.body.kind, 'web');
      assert.match(request.body.device_id, /^[a-zA-Z0-9-]+$/);
    }
    assert.equal(requests[0].body.device_id, requests[1].body.device_id);
    ids.push(requests[0].body.device_id);
  }
  assert.notEqual(ids[0], ids[1]);
});
