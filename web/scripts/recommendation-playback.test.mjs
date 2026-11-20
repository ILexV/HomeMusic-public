import assert from 'node:assert/strict';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { build } from 'vite';

// Real player, HTTP URL builder, checkpoint/outbox and native bridge; isolate devices.
const lib = (name) => JSON.stringify(fileURLToPath(new URL(`../src/lib/${name}.ts`, import.meta.url)));
const stubs = {
  react: 'export const useSyncExternalStore = (_, snapshot) => snapshot();',
  './actions': 'export const startDownload = async () => null;',
  './events': 'export const hubEvents = { on() {} };',
  './player': `export * from ${lib('player')}; import * as bridge from ${lib('player')}; harness.bridge = bridge;`,
  './plays': `export * from ${lib('plays')}; import { setListeningAccount, onPlayEvent } from ${lib('plays')};
    setListeningAccount(1); onPlayEvent(e => harness.events.push(e));`,
  './sharedPlayback': `export const localPlaybackAllowed = () => true;
    export const startSharedPlayback = adapter => { harness.adapter = adapter; }; export const stopSharedPlayback = () => {};`,
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
function tab({ rejectPlay = false, rejectResolve = false, saved = new Map(), offline = false } = {}) {
  const h = { events: [], requests: [], batches: [], audio: null, saved };
  class Audio extends EventTarget {
    currentTime = 0; duration = 180; paused = false; ended = false; readyState = 4; src = '';
    constructor() { super(); h.audio = this; }
    play() { this.paused = false; return rejectPlay ? Promise.reject(new Error('Decoder failed')) : Promise.resolve(); }
    pause() { this.paused = true; }
    load() {}
    getAttribute(name) { return name === 'src' ? this.src : null; }
    removeAttribute(name) { if (name === 'src') this.src = ''; }
  }
  const storage = { get length() { return saved.size; }, key: i => [...saved.keys()][i],
    getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key) };
  const window = Object.assign(new EventTarget(), { location: { href: 'http://hub/#/playlist/mine', hash: '#/playlist/mine' } });
  h.window = window;
  const context = vm.createContext({ harness: h, console, Audio, Event, performance, AbortController, AbortSignal, DOMException, URLSearchParams, URL,
    process: { env: { NODE_ENV: 'production' } },
    navigator: {}, window, document: new EventTarget(),
    localStorage: storage, sessionStorage: { getItem: () => 'test-tab', setItem() {} },
    setInterval: (fn, ms) => ms === 100 ? setInterval(fn, 1) : 0, clearInterval,
    setTimeout: (fn, ms) => { if (ms === 3000) h.deliver = fn; return 0; }, clearTimeout() {},
    HTMLMediaElement: { HAVE_NOTHING: 0, HAVE_FUTURE_DATA: 3 },
    fetch: async (path, options) => {
      if (path === '/healthz') return Response.json({ capabilities: ['listening_checkpoints_v1'] });
      if (path === '/api/plays/events') {
        if (offline) throw new Error('offline');
        h.batches.push(JSON.parse(options.body));
        return Response.json({ accepted: h.batches.at(-1).events.length, duplicates: 0, rejected: [] });
      }
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

test('jumping to the last seconds and pressing Next reports a skip, not a finish', async () => {
  const { listen, h } = tab();
  listen.listenAll(tracks, { context: 'wave:session', shuffle: false });
  await setImmediate();
  h.audio.dispatchEvent(new Event('playing'));
  h.audio.currentTime = 175;
  h.audio.dispatchEvent(new Event('timeupdate'));
  listen.next();
  const end = h.events.find(e => e.kind === 'skip' || e.kind === 'finish');
  assert.equal(end.position_s, 175);
  assert.equal(end.kind, 'skip');
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


async function sounding(h) {
  await setImmediate();
  h.audio.dispatchEvent(new Event('playing'));
}

test('each row, play-all, successor, Next/Previous and repeat carries its own intent on every event', async () => {
  const { listen, h } = tab();
  listen.listen(tracks[1], tracks);
  await sounding(h);
  listen.prev(); await sounding(h);
  listen.next(); await sounding(h);
  listen.listenAll(tracks, { shuffle: false }); await sounding(h);
  listen.cycleRepeat(); // repeat-all, but an ordinary successor isn't a repeat
  h.audio.dispatchEvent(new Event('ended')); await sounding(h);
  h.audio.dispatchEvent(new Event('ended')); await sounding(h);
  listen.cycleRepeat(); // repeat-one
  h.audio.dispatchEvent(new Event('ended')); await sounding(h);
  listen.playQueueAt(1); await sounding(h);
  listen.stopListening();
  const starts = h.events.filter(e => e.kind === 'start');
  assert.deepEqual(starts.map(e => e.play_trigger), ['manual', 'auto', 'auto', 'auto', 'auto', 'repeat', 'repeat', 'manual']);
  assert.equal(new Set(starts.map(e => e.playback_id)).size, starts.length);
  for (const start of starts) {
    const events = h.events.filter(e => e.playback_id === start.playback_id);
    assert.ok(events.some(e => e.kind === 'progress') && events.some(e => e.kind === 'finish' || e.kind === 'skip'));
    assert.ok(events.every(e => e.play_trigger === start.play_trigger && e.context === 'playlist:mine'));
  }
});

test('implicit playback after enqueue is not a manual choice to play now', async () => {
  for (const method of ['playNext', 'addToQueue']) {
    const { listen, h } = tab();
    listen[method]([tracks[0]]);
    await sounding(h);
    assert.equal(h.events.find(e => e.kind === 'start').play_trigger, 'auto');
  }
});

test('crash recovery replays the original intent offline; a restored queue starts a new unknown attempt', async () => {
  const { listen, h } = tab({ offline: true });
  listen.restoreListening(1);
  listen.listen(tracks[0], tracks);
  await sounding(h);
  const original = h.events.find(e => e.kind === 'start');
  await h.deliver(); await setImmediate();
  assert.ok(h.saved.has(`hm-play-event:${original.event_id}`));
  const reload = tab({ saved: h.saved });
  const interrupted = reload.h.events.find(e => e.kind === 'interrupted');
  assert.equal(interrupted.playback_id, original.playback_id);
  assert.equal(interrupted.play_trigger, 'manual');
  reload.listen.restoreListening(1);
  reload.listen.toggle(); await sounding(reload.h);
  const restored = reload.h.events.find(e => e.kind === 'start');
  assert.notEqual(restored.playback_id, original.playback_id);
  assert.equal(restored.play_trigger, null);
  await reload.h.deliver(); await setImmediate();
  const sent = reload.h.batches.flatMap(b => b.events);
  assert.ok(sent.some(e => e.event_id === original.event_id && e.play_trigger === 'manual'));
  assert.ok(sent.some(e => e.event_id === interrupted.event_id && e.play_trigger === 'manual'));
  assert.ok(sent.some(e => e.event_id === restored.event_id && e.play_trigger === null));
});

test('recovery of an old untagged checkpoint and outbox never invents a trigger', async () => {
  const old = { ref: 'dz:1', context: 'search', playback_id: 'legacy', device_id: 'phone', listened_ms: 34000, account: 1 };
  const start = { event_id: 'old-start', ts: new Date().toISOString(), kind: 'start', ref: 'dz:1', context: 'search', client: 'pwa', _account: 1 };
  const saved = new Map([['hm-open-play:test-tab', JSON.stringify(old)], ['hm-play-events', JSON.stringify([start])]]);
  const { h } = tab({ saved });
  await h.deliver(); await setImmediate();
  const sent = h.batches.flatMap(b => b.events);
  assert.deepEqual(sent.map(e => e.kind).sort(), ['interrupted', 'start']);
  assert.ok(sent.every(e => !('play_trigger' in e)));
});

test('shared handoff starts unknown, not another manual return', async () => {
  const { listen, h } = tab();
  listen.restoreListening(1);
  const state = { queue: tracks, index: 0, occurrence: 'source', shuffle: false, repeat: 'off', volume: 1, position: 0, playing: true };
  await h.adapter.execute({ kind: 'start', state, expires_at: Date.now() / 1000 + 30 }, new AbortController().signal);
  await sounding(h);
  assert.equal(h.events.find(e => e.kind === 'start').play_trigger, null);
});

test('modern WebView gets one-shot initial intent; the old bridge still receives bare song IDs', () => {
  const { h } = tab();
  const queues = [];
  h.window.HomeMusicAndroid = { playQueue: (items, index) => queues.push([JSON.parse(items), index]) };
  const songs = tracks.map((t, i) => ({ ...t, owned: true, nd_song_id: `song${i}` }));
  h.bridge.playTracks(songs, songs[0]);
  h.bridge.playTracks(songs);
  h.bridge.playList(songs, s => s.nd_song_id, songs[1]);
  assert.deepEqual(queues.map(([items, index]) => [index, items.map(t => t.initial_play_trigger ?? null)]),
    [[0, ['manual', null]], [0, ['auto', null]], [1, [null, 'manual']]]);
  assert.ok(queues.every(([items]) => items.every(t => t.context === 'playlist:mine' && t.launch_origin === 'playlist')));
  let legacy;
  h.window.HomeMusicAndroid = { play: (items, index) => { legacy = [JSON.parse(items), index]; } };
  h.bridge.playList(songs, s => s.nd_song_id, songs[1]);
  assert.deepEqual(legacy, [['song0', 'song1'], 1]);
});
