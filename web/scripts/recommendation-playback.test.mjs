import assert from 'node:assert/strict';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { build } from 'vite';

// Real player and real HTTP URL builder; isolate devices and the event journal.
const stubs = {
  react: 'export const useSyncExternalStore = (_, snapshot) => snapshot();',
  './actions': 'export const startDownload = async () => null;',
  './events': 'export const hubEvents = { on() {} };',
  './player': `export const NATIVE_PLAYING_EVENT = 'native'; export const canPlayAll = () => false;
    export const pauseNative = () => {}; export const webPlayerShown = () => {};`,
  './plays': `let id = 0; export const newEventId = () => 'test-' + (++id);
    export const listeningAccount = () => 1; export const routeContext = () => 'other';
    export const newLaunch = (context, origin) => ({ id: newEventId(), origin });
    export const playEvent = (kind, target, extra) => { harness.events.push({ kind, target, ...extra }); return true; };`,
  './sharedPlayback': `export const localPlaybackAllowed = () => true;
    export const startSharedPlayback = () => {}; export const stopSharedPlayback = () => {};`,
};
const built = await build({ configFile: false, logLevel: 'silent',
  plugins: [{ name: 'audio-boundaries', enforce: 'pre',
    resolveId(id, importer) { if (importer?.endsWith('/lib/listen.ts') && id in stubs) return '\0audio:' + id; },
    load(id) { if (id.startsWith('\0audio:')) return stubs[id.slice('\0audio:'.length)]; },
  }],
  build: { write: false, minify: false, lib: {
    entry: fileURLToPath(new URL('../src/lib/listen.ts', import.meta.url)), name: 'Listen', formats: ['iife'],
  } },
});
const bundle = (Array.isArray(built) ? built[0] : built).output.find(item => item.type === 'chunk').code;
const tracks = [1, 2].map(id => ({ ref: `dz:${id}`, title: `Track ${id}`, artist: 'Artist', duration_s: 180 }));
function tab({ rejectPlay = false, rejectResolve = false } = {}) {
  const h = { events: [], requests: [], audio: null };
  class Audio extends EventTarget {
    currentTime = 0; duration = 180; paused = false; ended = false; readyState = 4; src = '';
    constructor() { super(); h.audio = this; }
    play() { this.paused = false; return rejectPlay ? Promise.reject(new Error('Decoder failed')) : Promise.resolve(); }
    pause() { this.paused = true; }
    load() {}
    getAttribute(name) { return name === 'src' ? this.src : null; }
    removeAttribute(name) { if (name === 'src') this.src = ''; }
  }
  const storage = { length: 0, getItem: () => null, setItem() {}, removeItem() {} };
  const context = vm.createContext({ harness: h, console, Audio, Event, performance, AbortController, DOMException, URLSearchParams,
    navigator: {}, window: { addEventListener() {} }, document: { addEventListener() {} },
    localStorage: storage, sessionStorage: storage,
    setInterval: () => 0, setTimeout: () => 0, clearTimeout() {},
    HTMLMediaElement: { HAVE_NOTHING: 0, HAVE_FUTURE_DATA: 3 },
    fetch: async path => {
      h.requests.push(path);
      if (rejectResolve) return Response.json({ detail: 'No full recording' }, { status: 503 });
      return Response.json({ ref: 'dz:1', kind: 'full', duration_s: 180, stream_url: '/audio' });
    },
  });
  vm.runInContext(bundle, context);
  return { listen: context.Listen, h };
}

test('wave stream, resolver and reserve prefetch require full recordings', async () => {
  const { listen, h } = tab();
  listen.listenAll(tracks, { context: 'wave:session', shuffle: false });
  await setImmediate();
  assert.equal(new URL(h.audio.src, 'http://hub').searchParams.get('full'), '1');
  assert.equal(h.requests.length, 2);
  for (const request of h.requests) assert.equal(new URL(request, 'http://hub').searchParams.get('full'), '1');
});

test('manual catalog playback keeps its ordinary resolver contract', async () => {
  const { listen, h } = tab();
  listen.listenAll(tracks, { context: 'album:dz:1', shuffle: false });
  await setImmediate();
  assert.equal(new URL(h.audio.src, 'http://hub').searchParams.has('full'), false);
  for (const request of h.requests) assert.equal(new URL(request, 'http://hub').searchParams.has('full'), false);
});

test('media failure before sound is an error attempt, never a manual skip', async () => {
  const { listen, h } = tab();
  listen.listenAll(tracks, { context: 'wave:session', shuffle: false });
  await setImmediate();
  h.audio.dispatchEvent(new Event('error'));
  listen.next();
  assert.deepEqual(h.events.map(event => event.kind), ['error']);
  assert.ok(h.events[0].playback_id);
  assert.equal(h.events[0].listened_ms, 0);
  assert.equal(h.events[0].context, 'wave:session');
});

test('audio failure after start closes the same attempt without a later skip', async () => {
  const { listen, h } = tab();
  listen.listenAll(tracks, { context: 'wave:session', shuffle: false });
  await setImmediate();
  h.audio.dispatchEvent(new Event('playing'));
  h.audio.dispatchEvent(new Event('error'));
  listen.next();
  assert.deepEqual(h.events.filter(e => e.kind !== 'progress').map(e => e.kind), ['start', 'error']);
  assert.equal(h.events[0].playback_id, h.events.find(e => e.kind === 'error').playback_id);
});

test('decoder promise rejection and resolver failure record errors, not skips', async () => {
  for (const options of [{ rejectPlay: true }, { rejectResolve: true }]) {
    const { listen, h } = tab(options);
    listen.listenAll(tracks, { context: 'wave:session', shuffle: false });
    await setImmediate();
    assert.equal(listen.getListenState().phase, 'error');
    assert.deepEqual(h.events.map(event => event.kind), ['error']);
  }
});
