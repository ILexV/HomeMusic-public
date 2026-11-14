import { useSyncExternalStore } from 'react';
import { ApiError, api, errorMessage } from '../api/client';
import type { PlayEventIn, WaveOrigin, WaveOut, WavePreferences, WaveSeedIn, WaveTrackOut } from '../api/types';
import { EMBEDDED } from './embedded';
import { getListenState, listenAll, replaceUpcoming, type ListenTrack } from './listen';
import { canPlayAll, canPlayWave, playWave } from './player';
import { onPlayEvent } from './plays';
import { toastError } from './ui';

/** «Моя волна» (MIXES.md 7): an endless stream the Hub picks for the user, 5 tracks at a time
 * (`POST /api/wave/start`, then `POST /api/wave/{id}/next`).
 *
 * In a browser (and app versions whose player doesn't play catalog tracks) the page's own mini player plays it
 * (`lib/listen.ts`, context `wave:<id>`): each time a wave track starts, and after a ♥ of one, the events since the
 * last call go to the Hub with the refs still ahead, and the answer replaces them (`replaceUpcoming`) — so the
 * stream is picked again after every action. The same events go to /api/plays/events as always (the Hub stores an
 * event_id once).
 *
 * Inside the app (1.2.0+) one player plays, the native one: the wave is handed to it whole through the bridge
 * `playWave(waveJson)` (app 1.6.0+, `lib/player.ts`): the answer of /start; the app plays it and asks `next`
 * itself (`HubWave.kt`), with the same events. An app with `playQueue` but without `playWave` gets no wave
 * buttons (5 tracks without a continuation would be worse). Another list started in the page player ends the wave. */

/** The wave can play here: the page's own player, or the app's player that knows waves. */
export function canWave(): boolean {
  return !canPlayAll() || canPlayWave();
}

const RETRY_MS = 4000;

interface Session {
  id: string;
  preferences: WavePreferences;
  requestedPreferences?: WavePreferences;
  preferencesStatus: string | null;
  preparation: WaveOut['preparation'] | null;
  error: string | null;
  title: string;
  context: string;
  refs: Set<string>;
  songs: Set<string>;
  /** events of the wave's tracks not yet handed to the Hub */
  pending: PlayEventIn[];
  inFlight: PlayEventIn[];
  busy: boolean;
  again: boolean;
}

let session: Session | null = null;
let sessionAccount: number | null = null;
const SESSION_KEY = 'hm-wave-session:';
export const DEFAULT_WAVE_PREFERENCES: WavePreferences = { energy: 'any', vocals: 'any', discovery: 'balanced' };
let startPreferences = { ...DEFAULT_WAVE_PREFERENCES };
let generation = 0;
let startStatus: string | null = null;
let startPreparation: WaveOut['preparation'] | null = null;
let pendingStart: { id: string; seed: string; track: ListenTrack | null; context: string } | null = null;
let starting: string | null = null; // the seed being started ('' — the user's own wave)
const subscribers = new Set<() => void>();
let snapshot = waveSnapshot();

function waveSnapshot() {
  return { id: session?.id ?? null, title: session?.title ?? null, starting,
    preferences: session?.preferences ?? startPreferences,
    startPreferences,
    preferencesStatus: session?.preferencesStatus ?? startStatus,
    preparation: session?.preparation ?? startPreparation,
    updating: !!session?.requestedPreferences, error: session?.error ?? null };
}

export function setStartPreferences(preferences: WavePreferences) {
  startPreferences = preferences;
  emit();
}

/** Only the server's upcoming choices change; the current audio element is untouched. */
export function updateWavePreferences(preferences: WavePreferences) {
  const s = session;
  if (!s || getListenState().context !== s.context) return;
  s.requestedPreferences = preferences;
  s.error = null;
  emit();
  void ask();
}

export function refreshWaveRecommendations() {
  if (session && getListenState().context === session.context) void ask();
}

function emit() {
  persistWave();
  snapshot = waveSnapshot();
  for (const fn of subscribers) fn();
}

function persistWave() {
  if (sessionAccount === null) return;
  try {
    const key = SESSION_KEY + sessionAccount;
    if (!session) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify({ id: session.id, title: session.title,
      preferences: session.preferences, preferencesStatus: session.preferencesStatus,
      preparation: session.preparation, refs: [...session.refs], songs: [...session.songs],
      pending: [...session.inFlight, ...session.pending] }));
  } catch { /* The current wave can still play without storage. */ }
}

/** Continue the same server session, including events that were still awaiting acknowledgement. */
export function restoreWave(account: number | null) {
  if (sessionAccount === account) return;
  generation++;
  starting = null;
  startPreferences = { ...DEFAULT_WAVE_PREFERENCES };
  startStatus = null;
  startPreparation = null;
  pendingStart = null;
  session = null;
  sessionAccount = account;
  const st = getListenState();
  if (account !== null && !canPlayAll() && st.track && st.context.startsWith('wave:')) {
    const id = st.context.slice('wave:'.length);
    let saved: { id?: string; title?: string; refs?: string[]; songs?: string[]; pending?: PlayEventIn[];
      preferences?: WavePreferences; preferencesStatus?: string | null; preparation?: WaveOut['preparation'] | null } | null = null;
    try { saved = JSON.parse(localStorage.getItem(SESSION_KEY + account) ?? 'null'); } catch { /* rebuild from the queue */ }
    if (saved?.id !== id) saved = null;
    session = { id, title: typeof saved?.title === 'string' ? saved.title : 'Моя волна', context: st.context,
      preferences: saved?.preferences ?? { ...DEFAULT_WAVE_PREFERENCES },
      preferencesStatus: saved?.preferencesStatus ?? null, preparation: saved?.preparation ?? null, error: null,
      refs: new Set([...(Array.isArray(saved?.refs) ? saved.refs.filter((r) => typeof r === 'string') : []), ...st.queue.map((t) => t.ref)]),
      songs: new Set(Array.isArray(saved?.songs) ? saved.songs.filter((r) => typeof r === 'string') : []),
      pending: Array.isArray(saved?.pending) ? saved.pending.filter((e) => e && typeof e.event_id === 'string') : [],
      inFlight: [], busy: false, again: false };
  }
  emit();
}

window.addEventListener('pagehide', persistWave);

function subscribe(fn: () => void) {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

/** The playing wave's title (null — none) and the seed being started. */
export function useWave() {
  return useSyncExternalStore(subscribe, () => snapshot);
}

export const seedKey = (seed?: WaveSeedIn | null) => (seed?.by_artist ? 'artist:' : '') + (seed?.ref ?? seed?.nd_song_id ?? seed?.artist_ref ?? seed?.nd_artist_id ?? '');

type SeedTrack = { ref?: string | null; nd_song_id?: string | null; artist_ref?: string | null };
export function trackSeed(t: SeedTrack): WaveSeedIn | null {
  const ref = t.ref?.trim();
  if (ref && /^(dz|nd):.+/.test(ref)) return { ref };
  const id = t.nd_song_id?.trim();
  if (id) return { nd_song_id: id };
  return ref && /^file:[^:]+:.+/.test(ref) ? { ref } : null;
}
export function artistSeed(t: SeedTrack): WaveSeedIn | null {
  const ref = t.artist_ref?.trim();
  if (ref && /^dz:.+/.test(ref)) return { artist_ref: ref };
  const track = trackSeed(t);
  return track && !track.ref?.startsWith('file:') ? { ...track, by_artist: true } : null;
}

const listenTrack = (t: WaveTrackOut): ListenTrack => ({
  ref: t.ref,
  nd_song_id: t.nd_song_id,
  explanation: t.explanation,
  title: t.title,
  artist: t.artists?.length ? t.artists.join(', ') : t.artist,
  cover_url: t.cover_url,
  cover_small_url: t.cover_small_url,
  album_ref: t.album_ref,
  artist_ref: t.artist_ref,
  artists: (t.artists?.length ? t.artists : [t.artist]).map((name) =>
    ({ name, ...(t.artists?.length && name === t.artist && t.artist_ref ? { ref: t.artist_ref } : {}) })),
  album: t.album_title ?? undefined, isrc: t.isrc ?? undefined,
  year: t.release_date ? Number(t.release_date.slice(0, 4)) || undefined : undefined,
  duration_s: t.duration_s,
});

function remember(s: Session, w: WaveOut) {
  s.preferences = w.preferences ?? { ...DEFAULT_WAVE_PREFERENCES };
  s.preferencesStatus = w.preferences_status;
  s.preparation = w.preparation;
  s.error = null;
  for (const t of w.tracks) {
    s.refs.add(t.ref);
    if (t.nd_song_id) s.songs.add(t.nd_song_id);
  }
}

/** Starts the user's own wave, or one from a track or an artist («Волна по треку»). `origin` — where it was started
 * (INSIGHTS.md 6.3: analytics only, the Hub keeps it with the session). */
export async function startWave(seed: WaveSeedIn | null | undefined, origin: WaveOrigin): Promise<boolean> {
  if (starting !== null) return false;
  const epoch = ++generation;
  const account = sessionAccount;
  const before = getListenState();
  starting = seedKey(seed);
  startStatus = null;
  startPreparation = null;
  emit();
  try {
    const pending = pendingStart;
    const w = pending && pending.seed === seedKey(seed) && pending.track === before.track && pending.context === before.context
      ? await api.waveNext(pending.id, { played: [], queued: [], preferences: startPreferences })
      : await api.waveStart({ seed: seed ?? null, origin, client: EMBEDDED ? 'pwa-app' : 'pwa', preferences: startPreferences });
    if (generation !== epoch || sessionAccount !== account || getListenState().track !== before.track || getListenState().context !== before.context) return false;
    startPreparation = w.preparation;
    startStatus = w.preferences_status;
    if (!w.tracks.length) {
      pendingStart = w.pending ? { id: w.session_id, seed: seedKey(seed), track: before.track, context: before.context } : null;
      startStatus = w.pending ? 'Полные записи ещё готовятся. Попробуйте запустить волну позже.' : 'Нет доступных полных записей для этой волны.';
      toastError(startStatus);
      return false;
    }
    pendingStart = null;
    if (canPlayWave()) {
      end(); // a wave the page's own player had is over: the app's player plays now
      playWave(w);
      return true;
    }
    const s: Session = { id: w.session_id, title: w.title, context: `wave:${w.session_id}`, refs: new Set(), songs: new Set(), pending: [], inFlight: [], busy: false, again: false,
      preferences: w.preferences, preferencesStatus: w.preferences_status, preparation: w.preparation, error: null };
    remember(s, w);
    const tracks = w.tracks.map(listenTrack);
    // A wave is a new list even when its seed is already playing; listen() would only toggle pause.
    listenAll(tracks, { shuffle: false, context: s.context });
    session = s;
    startPreferences = { ...DEFAULT_WAVE_PREFERENCES };
    return true;
  } catch (e) {
    if (generation === epoch && sessionAccount === account) {
      if (e instanceof ApiError && e.status === 404) pendingStart = null;
      startStatus = errorMessage(e);
      toastError(startStatus);
    }
    return false;
  } finally {
    if (generation === epoch) {
      starting = null;
      emit();
    }
  }
}

/** The events since the last call and the refs still ahead go to the Hub; its picks replace them. */
async function ask() {
  const s = session;
  if (!s || getListenState().context !== s.context) return;
  if (s.busy) {
    s.again = true;
    return;
  }
  s.busy = true;
  const played = s.pending.splice(0);
  s.inFlight = played;
  const preferences = s.requestedPreferences;
  persistWave();
  const st = getListenState();
  const queued = st.queue.slice(st.index + 1).map((t) => t.ref);
  try {
    const w = await api.waveNext(s.id, { played, queued, preferences });
    if (session !== s || getListenState().context !== s.context) return;
    remember(s, w);
    if (s.requestedPreferences === preferences) s.requestedPreferences = undefined;
    // Empty means there is no verified reserve, not permission to keep stale choices.
    if (getListenState().track !== st.track) s.again = true;
    else replaceUpcoming(w.tracks.map(listenTrack));
    if (w.pending) setTimeout(() => { if (session === s) void ask(); }, RETRY_MS);
  } catch (e) {
    if (session !== s || getListenState().context !== s.context) return;
    s.error = errorMessage(e);
    if (s.requestedPreferences === preferences) s.requestedPreferences = undefined;
    if (e instanceof ApiError && e.status === 404) {
      toastError('Сессия волны завершена. Запустите новую волну.');
      end(); // the session is over (a day without it): the queue plays out
    } else s.pending.unshift(...played); // offline: with the next call
  } finally {
    s.inFlight = [];
    persistWave();
    s.busy = false;
    if (session === s) emit();
    if (s.again && session === s) {
      s.again = false;
      void ask();
    }
  }
}

function end() {
  session = null;
  emit();
}

onPlayEvent((e) => {
  if (e.kind === 'progress' || e.kind === 'interrupted') return;
  if ((e.target_type && e.target_type !== 'track') || e.reverts_event_id) return;
  const s = session;
  if (!s) return;
  if (getListenState().context !== s.context) return end(); // another list plays now
  if (!(e.ref && s.refs.has(e.ref)) && !(e.nd_song_id && s.songs.has(e.nd_song_id))) return;
  s.pending.push(e);
  persistWave();
  if (e.kind === 'start' || e.kind === 'like' || e.kind === 'dislike' || e.kind === 'error') void ask();
});
