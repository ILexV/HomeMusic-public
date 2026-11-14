import assert from 'node:assert/strict';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { build } from 'vite';

// Exercise the real wave coordinator against deterministic transport/audio boundaries.
// A preference update must never invoke listenAll or let a stale response replace audio.
const stubs = {
  react: 'export const useSyncExternalStore = (_, snapshot) => snapshot();',
  '../api/client': `export class ApiError extends Error { constructor(status, message) { super(message); this.status = status; } }
    harness.ApiError = ApiError;
    export const errorMessage = e => e.message;
    export const api = { waveStart: body => harness.start(body), waveNext: (id, body) => harness.next(id, body) };`,
  './embedded': 'export const EMBEDDED = false;',
  './listen': `export const getListenState = () => harness.state;
    export const listenAll = (tracks, options) => { harness.starts++; harness.state = { track: tracks[0], queue: tracks, index: 0, context: options.context }; };
    export const replaceUpcoming = tracks => { harness.replacements++; harness.state = { ...harness.state, queue: [...harness.state.queue.slice(0, harness.state.index + 1), ...tracks] }; };`,
  './player': 'export const canPlayAll = () => false; export const canPlayWave = () => false; export const playWave = () => { throw Error("native"); };',
  './plays': 'export const onPlayEvent = listener => { harness.event = listener; };',
  './ui': 'export const toastError = message => harness.errors.push(message);',
};
const built = await build({
  configFile: false, logLevel: 'silent',
  plugins: [{ name: 'wave-boundaries', enforce: 'pre',
    resolveId(id, importer) { if (importer?.endsWith('/lib/wave.ts') && id in stubs) return '\0boundary:' + id; },
    load(id) { if (id.startsWith('\0boundary:')) return stubs[id.slice('\0boundary:'.length)]; },
  }],
  build: { write: false, minify: false, lib: {
    entry: fileURLToPath(new URL('../src/lib/wave.ts', import.meta.url)), name: 'Wave', formats: ['iife'],
  } },
});
const bundle = (Array.isArray(built) ? built[0] : built).output.find(item => item.type === 'chunk').code;
const neutral = { energy: 'any', vocals: 'any', discovery: 'balanced' };
const calm = { energy: 'calm', vocals: 'instrumental', discovery: 'new' };
const track = ref => ({ ref, title: ref, artist: 'Artist', explanation: ['Stored decision'] });
const response = (id, refs, preferences = neutral) => ({
  session_id: id, title: 'Моя волна', tracks: refs.map(track), pending: false, preferences,
  preferences_status: null, preparation: { ready: refs.length, pending: 0, unavailable: 0 },
});
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function tab() {
  const saved = new Map();
  const h = { state: { track: null, queue: [], index: -1, context: 'other' }, starts: 0, replacements: 0, errors: [],
    start: async () => response('first', ['dz:1', 'dz:2']), next: async () => response('first', ['dz:3']) };
  const context = vm.createContext({ harness: h, console, setTimeout: () => 0,
    window: { addEventListener() {} },
    localStorage: { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key) },
  });
  vm.runInContext(bundle, context);
  context.Wave.restoreWave(1);
  return { wave: context.Wave, h, saved };
}

test('preference changes replace only upcoming and persist within the active wave', async () => {
  const { wave, h, saved } = tab();
  wave.setStartPreferences(calm);
  h.start = async body => { assert.deepEqual(JSON.parse(JSON.stringify(body.preferences)), calm); return response('first', ['dz:1', 'dz:2'], calm); };
  assert.equal(await wave.startWave(null, 'home_card'), true);
  const current = h.state.track;
  h.next = async (id, body) => {
    assert.equal(id, 'first');
    assert.deepEqual(JSON.parse(JSON.stringify(body.preferences)), neutral);
    return response('first', ['dz:3']);
  };
  wave.updateWavePreferences(neutral);
  await setImmediate();
  assert.equal(h.starts, 1);
  assert.equal(h.state.track, current);
  assert.deepEqual(Array.from(h.state.queue, t => t.ref), ['dz:1', 'dz:3']);
  assert.deepEqual(JSON.parse(saved.get('hm-wave-session:1')).preferences, neutral);
  h.start = async body => { assert.deepEqual(JSON.parse(JSON.stringify(body.preferences)), neutral); return response('second', ['dz:4']); };
  assert.equal(await wave.startWave(null, 'home_card'), true);
});

test('late start after account switch cannot start music or paint another account', async () => {
  const { wave, h } = tab();
  const request = deferred(); h.start = () => request.promise;
  const starting = wave.startWave(null, 'home_card');
  wave.restoreWave(2);
  request.resolve(response('old-account', ['dz:1']));
  assert.equal(await starting, false);
  assert.equal(h.starts, 0);
  assert.equal(wave.useWave().title, null);
});

test('late failed next from old wave cannot end a newer wave', async () => {
  const { wave, h } = tab();
  await wave.startWave(null, 'home_card');
  const request = deferred(); h.next = () => request.promise;
  wave.updateWavePreferences(calm);
  h.start = async () => response('second', ['dz:9']);
  await wave.startWave(null, 'home_card');
  request.reject(new h.ApiError(404, 'expired'));
  await setImmediate();
  assert.equal(wave.useWave().id, 'second');
  assert.equal(h.state.track.ref, 'dz:9');
  assert.equal(h.errors.length, 0);
});

test('empty verified reserve removes stale upcoming without replacing current audio', async () => {
  const { wave, h } = tab();
  await wave.startWave(null, 'home_card');
  const current = h.state.track;
  h.next = async () => ({ ...response('first', [], calm), pending: true, preparation: { ready: 0, pending: 2, unavailable: 1 } });
  wave.updateWavePreferences(calm);
  await setImmediate();
  assert.equal(h.state.track, current);
  assert.equal(h.state.queue.length, 1);
  assert.equal(h.starts, 1);
  assert.equal(wave.useWave().preparation.pending, 2);
});

test('late next after playback advances is re-requested rather than requeueing current track', async () => {
  const { wave, h } = tab();
  await wave.startWave(null, 'home_card');
  const request = deferred(); let calls = 0;
  h.next = () => ++calls === 1 ? request.promise : Promise.resolve(response('first', ['dz:4']));
  wave.updateWavePreferences(calm);
  const current = h.state.queue[1]; h.state = { ...h.state, track: current, index: 1 };
  request.resolve(response('first', ['dz:2', 'dz:3'], calm));
  await setImmediate();
  assert.equal(h.state.track, current);
  assert.equal(calls, 2);
  assert.deepEqual(Array.from(h.state.queue, t => t.ref), ['dz:1', 'dz:2', 'dz:4']);
  assert.equal(h.replacements, 1);
});

test('retrying a pending explicit seed continues its existing server session', async () => {
  const { wave, h } = tab();
  let starts = 0;
  h.start = async () => {
    starts++;
    return { ...response('pending-seed', []), pending: true };
  };
  assert.equal(await wave.startWave({ ref: 'dz:7' }, 'player'), false);
  assert.equal(h.starts, 0);
  h.next = async (id, body) => {
    assert.equal(id, 'pending-seed');
    assert.equal(body.queued.length, 0);
    return response('pending-seed', ['dz:7']);
  };
  assert.equal(await wave.startWave({ ref: 'dz:7' }, 'player'), true);
  assert.equal(starts, 1);
  assert.equal(h.state.track.ref, 'dz:7');
});
