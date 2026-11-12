import { useSyncExternalStore } from 'react';
import { api, errorMessage, isAbort, listenStreamUrl } from '../api/client';
import type { LaunchOrigin, ListenOut } from '../api/types';
import { startDownload } from './actions';
import { hubEvents } from './events';
import { NATIVE_PLAYING_EVENT, canPlayAll, pauseNative, webPlayerShown } from './player';
import { listeningAccount, newEventId, newLaunch, playEvent, routeContext, type Launch } from './plays';
import type { PlaybackCommand, PlaybackSnapshot } from '../api/playback';
import { localPlaybackAllowed, startSharedPlayback, stopSharedPlayback } from './sharedPlayback';

/** Listening to catalog tracks before downloading them, in the page itself (one `<audio>`): the Hub picks the
 * file a download would take and streams it (GET /api/listen). Works in a plain browser too. Inside the Android
 * app only one player plays: starting here pauses the app's player, and its start pauses this one.
 *
 * A tap in a list (album, search results) plays that list from the tapped track on: when a track ends the next
 * one starts, and while one plays the Hub already picks the file for the next (the pick is what takes seconds).
 * The tracks on screen are named to the Hub as they appear (`watchOnScreen`): it picks their files in the
 * background and remembers them, so most taps start at once.
 *
 * `<audio src>` is set and `play()` is called within the tap itself: the app's WebView (and Safari) allow sound
 * only from a user gesture, and the Hub picking the file may take seconds. The stream request waits for the
 * same pick that `api.listen` reports on.
 *
 * What plays here is reported to the Hub for mixes (`lib/plays.ts`, MIXES.md 3.2): `start` once sound plays, then
 * `finish` (played to its end, or left after ≥ 90 %) or `skip` (left earlier: another track, the player closed),
 * with the screen the list was started from. A Deezer preview is no listen of the track and is not reported. */

export interface ListenTrack {
  ref: string;
  /** the library song, when the track is one: the Hub knows a playlist's song by this id (`idOf`) */
  nd_song_id?: string | null;
  /** a recommendation of the smart shuffle (`lib/smartShuffle.ts`): ✨ in the player and the queue; `label` — «Рекомендация
   * на основе …», `session` — the Hub's session (its events carry the context `smart:<session>`) */
  rec?: { label: string; session: string } | null;
  title: string;
  artist: string;
  cover_url?: string | null;
  /** the small cover for the mini player (the system notification keeps the big one) */
  cover_small_url?: string | null;
  album_ref?: string | null;
  artist_ref?: string | null;
  artists?: { name: string; ref?: string }[];
  album?: string;
  isrc?: string;
  year?: number;
  duration_s?: number | null;
}

/** off; all: the queue starts over after its end; one: the track plays again. */
export type RepeatMode = 'off' | 'all' | 'one';

/** resolving: the Hub is picking/opening the file; loading: buffering after a seek or a stall. */
export type ListenPhase = 'resolving' | 'loading' | 'playing' | 'paused' | 'ended' | 'error';

export interface ListenState {
  track: ListenTrack | null;
  /** the list the track was started from (album, search results); `index` is the track's place in it */
  queue: readonly ListenTrack[];
  index: number;
  /** where the list was started from (`routeContext()`, `wave:<id>`) */
  context: string;
  phase: ListenPhase;
  info: ListenOut | null;
  error: string | null;
  position: number;
  duration: number;
  volume: number;
  /** Catalog tracks downloaded from the player or a track row this session: rows show «качается», then «есть». */
  downloads: ReadonlyMap<string, 'queued' | 'done'>;
  /** The queue after the current track is in random order (not for «Моя волна»: the Hub picks its order). */
  shuffle: boolean;
  /** ... and the Hub puts recommendations between the playlist's songs (the smart shuffle, `lib/smartShuffle.ts`):
   * for the list that plays now; `shuffle` is on too */
  smart: boolean;
  repeat: RepeatMode;
}

/** The id the Hub knows a track of a playlist by: its Navidrome song, else the catalog ref. */
export const idOf = (t: Pick<ListenTrack, 'ref' | 'nd_song_id'>) => t.nd_song_id || t.ref;

const VOLUME_KEY = 'hm-listen-volume';
const savedVolume = (() => {
  try {
    const value = localStorage.getItem(VOLUME_KEY);
    const volume = value === null ? 1 : Number(value);
    return Number.isFinite(volume) && volume >= 0 && volume <= 1 ? volume : 1;
  } catch { return 1; }
})();

const MODES_KEY = 'hm-listen-modes';
const savedModes = ((): { shuffle: boolean; smart: boolean; repeat: RepeatMode } => {
  try {
    const m = JSON.parse(localStorage.getItem(MODES_KEY) ?? 'null') as { shuffle?: unknown; smart?: unknown; repeat?: unknown } | null;
    return { shuffle: m?.shuffle === true, smart: m?.shuffle === true && m?.smart === true,
      repeat: m?.repeat === 'all' || m?.repeat === 'one' ? m.repeat : 'off' };
  } catch {
    return { shuffle: false, smart: false, repeat: 'off' };
  }
})();
/** The shuffle button's choice (off, on, smart) lives on past lists that cannot be smart: an album plays in the plain
 * shuffle, the next playlist in the smart one again. `state.smart` is whether the playing list has the smart one. */
let smartChosen = savedModes.smart;

let state: ListenState = {
  track: null,
  queue: [],
  index: 0,
  context: 'other',
  phase: 'paused',
  info: null,
  error: null,
  position: 0,
  duration: 0,
  volume: savedVolume,
  downloads: new Map(),
  shuffle: savedModes.shuffle,
  smart: false,
  repeat: savedModes.repeat,
};
const subscribers = new Set<() => void>();

function set(patch: Partial<ListenState>) {
  const shown = !!state.track;
  state = { ...state, ...patch };
  // Time updates are frequent; queue edits and controls must survive an immediate reload.
  persistListening(Object.keys(patch).some((key) => key !== 'position' && key !== 'duration'));
  if (!!state.track !== shown) webPlayerShown(!!state.track);
  for (const fn of subscribers) fn();
}

function subscribe(fn: () => void) {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

/** The page's own player is playing or about to (a reload would cut the sound). */
export const listenBusy = () => !!state.track && (state.phase === 'playing' || state.phase === 'resolving' || state.phase === 'loading');

export const getListenState = (): ListenState => state;

/** A slice of the listening state; `select` must return a primitive or a stored object (not a new one). */
export function useListen<T>(select: (s: ListenState) => T): T {
  return useSyncExternalStore(subscribe, () => select(state));
}

let audio: HTMLAudioElement | null = null;

/** The page player's `<audio>` (null until the first track): `lib/waveAudio.ts` reads its loudness. */
export const listenAudio = (): HTMLAudioElement | null => audio;
let pendingSeek = 0;
let occurrence = newEventId();
let applyingShared = false;
let resolving: AbortController | null = null;
let prefetching: { ref: string; ctl: AbortController } | null = null;
/** Seconds into a track after which ⏮ restarts it instead of going to the previous one. */
const RESTART_AFTER_S = 3;
/** Left after this share of a track: it counts as played to the end (`finish`), earlier — `skip`. */
const FINISH_SHARE = 0.9;

/** The track being played, as reported to the Hub: `started` once its `start` was sent. */
type OpenPlay = { ref: string; context: string; launch: Launch; started: boolean; playbackId: string; listenedMs: number; anchor: number | null; account: number | null; ndSongId?: string | null };
let played: OpenPlay | null = null;
let OPEN_PLAY_KEY = (() => {
  try {
    let tabId = sessionStorage.getItem('hm-listening-tab');
    if (!tabId) { tabId = newEventId(); sessionStorage.setItem('hm-listening-tab', tabId); }
    return `hm-open-play:${tabId}`;
  } catch { return `hm-open-play:${newEventId()}`; }
})();
const DEVICE_KEY = 'hm-listening-device';
const deviceId = (() => {
  try {
    const existing = localStorage.getItem(DEVICE_KEY);
    if (existing) return existing;
    const id = newEventId();
    localStorage.setItem(DEVICE_KEY, id);
    return id;
  } catch { return newEventId(); }
})();

function accrue(p: OpenPlay) {
  if (p.anchor !== null) {
    const now = performance.now();
    p.listenedMs += Math.max(0, now - p.anchor);
    p.anchor = now;
  }
}

function checkpoint() {
  const p = played;
  if (!p?.started) return;
  accrue(p);
  const snapshot = { ref: p.ref, context: p.context, playback_id: p.playbackId, device_id: deviceId,
    listened_ms: Math.round(p.listenedMs), nd_song_id: p.ndSongId, track_metadata: metadata(), account: p.account, updated_ms: Date.now(),
    launch_id: p.launch.id, launch_origin: p.launch.origin };
  try { localStorage.setItem(OPEN_PLAY_KEY, JSON.stringify(snapshot)); } catch (e) { console.error('Could not persist listening checkpoint', e); }
  playEvent('progress', { ref: p.ref, nd_song_id: p.ndSongId }, snapshot);
}

type SavedPlay = { ref: string; context: string; playback_id: string; device_id: string; listened_ms: number;
  nd_song_id?: string | null; account?: number | null; updated_ms?: number; launch_id?: string | null; launch_origin?: LaunchOrigin | null };

function recover(key: string) {
  try {
    const old = JSON.parse(localStorage.getItem(key) ?? 'null') as SavedPlay | null;
    if (old?.ref && old.playback_id && old.device_id && old.account != null && Number.isFinite(old.listened_ms)) {
      if (playEvent('interrupted', { ref: old.ref, nd_song_id: old.nd_song_id }, old)) localStorage.removeItem(key);
    }
  } catch (e) { console.error('Could not recover listening checkpoint', e); }
}

let releaseTabLock: (() => void) | null = null;
function scanAbandoned() {
  try {
    const keys = Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i))
      .filter((key): key is string => !!key && key.startsWith('hm-open-play:') && key !== OPEN_PLAY_KEY);
    for (const key of keys) {
      if (navigator.locks) {
        void navigator.locks.request(key, { ifAvailable: true }, (lock) => { if (lock) recover(key); });
      } else {
        const old = JSON.parse(localStorage.getItem(key) ?? 'null') as SavedPlay | null;
        if (old?.updated_ms && Date.now() - old.updated_ms > 300_000) recover(key);
      }
    }
  } catch (e) { console.error('Could not inspect listening checkpoints', e); }
}

function lockTab() {
  if (!navigator.locks) { recover(OPEN_PLAY_KEY); scanAbandoned(); return; }
  void navigator.locks.request(OPEN_PLAY_KEY, { ifAvailable: true }, async (lock) => {
    if (!lock) {
      OPEN_PLAY_KEY = `hm-open-play:${newEventId()}`;
      try { sessionStorage.setItem('hm-listening-tab', OPEN_PLAY_KEY.slice('hm-open-play:'.length)); } catch {}
      lockTab();
      return;
    }
    recover(OPEN_PLAY_KEY);
    scanAbandoned();
    await new Promise<void>((resolve) => { releaseTabLock = resolve; });
  });
}
lockTab();
setInterval(() => { checkpoint(); scanAbandoned(); }, 15_000);
window.addEventListener('pagehide', () => {
  stopSharedPlayback();
  audio?.pause();
  pause();
  persistListening(true);
  checkpoint();
  reportEnd(false, false, true);
  releaseTabLock?.();
  releaseTabLock = null;
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') persistListening(true);
});
window.addEventListener('pageshow', (event) => {
  if (!event.persisted) return;
  lockTab();
  if (sessionAccount !== null) startSharedPlayback(sharedAdapter, sessionAccount);
  const t = state.track;
  if (t) {
    played = { ref: t.ref, context: state.context, launch: listLaunch, started: false, playbackId: newEventId(), listenedMs: 0, anchor: null, account: listeningAccount() };
    reportStart();
  }
});

/** The screen the playing list was started from. */
let listContext = 'other';
/** The launch of the playing list: its tracks' events carry it (INSIGHTS.md 6.5). */
let listLaunch: Launch = newLaunch(listContext);

function metadata() {
  const t = state.track;
  return t ? { title: t.title, artist_credit: t.artist, duration_s: t.duration_s, album: t.album,
    isrc: t.isrc, year: t.year, ...(t.artists?.length ? { artists: t.artists } : {}) } : undefined;
}

/** Sends `start` once the track sounds and the Hub has said what plays (not a preview). */
function reportStart() {
  const p = played;
  if (!p || p.started || state.track?.ref !== p.ref || !state.info || state.info.kind === 'preview') return;
  if (state.phase !== 'playing') return;
  p.started = true;
  p.ndSongId = state.info.nd_song_id;
  p.anchor = performance.now();
  playEvent('start', { ref: p.ref, nd_song_id: p.ndSongId }, { context: p.context, playback_id: p.playbackId, device_id: deviceId, listened_ms: 0, track_metadata: metadata(), account: p.account, launch_id: p.launch.id, launch_origin: p.launch.origin });
  checkpoint();
}

/** The current track is left (`ended`: it played to its end): `finish` or `skip` by where the player was. */
function reportEnd(ended = false, failed = false, interrupted = false) {
  const p = played;
  if (p?.started) accrue(p);
  played = null;
  if (!p?.started || state.track?.ref !== p.ref) return;
  const duration = state.duration || state.track.duration_s || 0;
  const position = ended ? duration : state.position;
  const kind = interrupted ? 'interrupted' : failed ? 'error' : ended || (duration > 0 && position >= duration * FINISH_SHARE) ? 'finish' : 'skip';
  const persisted = playEvent(kind, { ref: p.ref, nd_song_id: p.ndSongId }, { position_s: position, duration_s: duration || null, context: p.context, playback_id: p.playbackId, device_id: deviceId, listened_ms: Math.round(p.listenedMs), track_metadata: metadata(), account: p.account, launch_id: p.launch.id, launch_origin: p.launch.origin });
  if (persisted) try { localStorage.removeItem(OPEN_PLAY_KEY); } catch (e) { console.error('Could not close listening checkpoint', e); }
}

function el(): HTMLAudioElement {
  if (audio) return audio;
  const a = new Audio();
  a.preload = 'auto';
  a.volume = state.volume;
  const sync = () =>
    set({ position: pendingSeek || a.currentTime, duration: Number.isFinite(a.duration) && a.duration > 0 ? a.duration : state.duration });
  a.addEventListener('loadedmetadata', () => {
    if (pendingSeek > 0) {
      a.currentTime = Number.isFinite(a.duration) ? Math.min(pendingSeek, Math.max(0, a.duration - 0.1)) : pendingSeek;
      pendingSeek = 0;
    }
    sync();
  });
  a.addEventListener('timeupdate', sync);
  a.addEventListener('durationchange', sync);
  a.addEventListener('play', pauseNative);
  a.addEventListener('playing', () => {
    set({ phase: 'playing', error: null });
    refreshInfo();
    reportStart();
    if (played?.started && played.anchor === null) played.anchor = performance.now();
  });
  const suspendClock = () => { if (played?.started) { accrue(played); played.anchor = null; checkpoint(); } };
  a.addEventListener('seeking', suspendClock);
  a.addEventListener('seeked', () => {
    if (played?.started && !a.paused && a.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) played.anchor = performance.now();
  });
  a.addEventListener('waiting', () => {
    suspendClock();
    if (state.phase === 'playing') set({ phase: 'loading' });
  });
  // Pauses from outside (audio focus, lock screen); switching tracks also fires one, during 'resolving'.
  a.addEventListener('pause', () => {
    suspendClock();
    if (!a.ended && (state.phase === 'playing' || state.phase === 'loading')) set({ phase: 'paused' });
  });
  // the same element plays on: it was unlocked by the first tap, so the next track may start without one
  a.addEventListener('ended', () => {
    reportEnd(true);
    if (state.repeat === 'one') start(state.queue, state.index, true);
    else if (hasNext(state)) start(state.queue, state.index + 1, true);
    else if (state.repeat === 'all' && !onWave()) start(state.queue, 0, true);
    else set({ phase: 'ended' });
  });
  a.addEventListener('error', () => {
    if (a.getAttribute('src')) { reportEnd(false, true); set({ phase: 'error', error: state.error ?? 'Не удалось воспроизвести' }); }
  });
  // the app's player started: one player on screen, so this one closes (the app shows its own again)
  window.addEventListener(NATIVE_PLAYING_EVENT, stopListening);
  audio = a;
  return a;
}

function play(a: HTMLAudioElement) {
  // NotAllowedError (no user gesture): wait for the play button; AbortError: a newer track or a pause won.
  a.play().catch((e: unknown) => {
    if ((e as Error)?.name === 'NotAllowedError') set({ phase: 'paused' });
  });
}

/** `auto`: reached by the previous track ending; a track that can't be played is then skipped. */
function start(queue: readonly ListenTrack[], index: number, auto = false, position = 0, autoplay = true) {
  if (!applyingShared && !localPlaybackAllowed()) return;
  occurrence = newEventId();
  const track = queue[index];
  const a = el();
  reportEnd();
  // a recommendation of the smart shuffle reports under its session, the playlist's own songs under their list
  played = { ref: track.ref, context: track.rec ? `smart:${track.rec.session}` : listContext, launch: listLaunch, started: false, playbackId: newEventId(), listenedMs: 0, anchor: null, account: listeningAccount() };
  resolving?.abort();
  const ctl = new AbortController();
  resolving = ctl;
  const resolveTimeout = setTimeout(() => {
    if (resolving !== ctl) return;
    ctl.abort();
    a.pause();
    set({ phase: 'error', error: 'Истекло время загрузки аудио' });
  }, 20_000);
  pendingSeek = position;
  set({ track, queue, index, context: listContext, phase: autoplay ? 'resolving' : 'paused', info: null, error: null, position, duration: track.duration_s ?? 0 });
  a.src = listenStreamUrl(track.nd_song_id ? `nd:${track.nd_song_id}` : track.ref);
  if (autoplay) play(a);
  else a.load();
  mediaSession(track);
  // the prefetch of this very track (it was next) is the request to wait for: don't abort it
  if (prefetching?.ref !== track.ref) prefetching?.ctl.abort();
  prefetching = null;
  api.listen(track.nd_song_id ? `nd:${track.nd_song_id}` : track.ref, ctl.signal).then(
    (info) => {
      if (resolving !== ctl) return;
      set({ info, duration: info.kind === 'preview' ? (info.duration_s ?? 30) : state.duration });
      reportStart();
      prefetchNext();
    },
    (e: unknown) => {
      if (isAbort(e) || resolving !== ctl) return;
      if (auto && hasNext(state)) return void start(state.queue, state.index + 1, true);
      set({ phase: 'error', error: errorMessage(e) });
      a.pause();
    },
  ).finally(() => clearTimeout(resolveTimeout));
}

/** The Hub opens the picked file only when the stream asks for it, and moves on to the next file if it doesn't
 * open: once sound plays, the source shown is asked for again (the Hub has it in memory). */
let refreshed: string | null = null;
function refreshInfo() {
  const ref = state.track?.ref;
  if (!ref || refreshed === ref || state.info?.kind !== 'full') return;
  refreshed = ref;
  const ctl = new AbortController();
  const timeout = setTimeout(() => ctl.abort(), 8000);
  api.listen(ref, ctl.signal).then(
    (info) => state.track?.ref === ref && set({ info }),
    () => {},
  ).finally(() => clearTimeout(timeout));
}

/** Asks the Hub to pick the next track's file now (it remembers the pick), so it starts at once. */
function prefetchNext() {
  const nextTrack = state.queue[state.index + 1] ?? (state.repeat === 'all' && !onWave() ? state.queue[0] : undefined);
  if (!nextTrack || prefetching?.ref === nextTrack.ref) return;
  prefetching?.ctl.abort();
  const ctl = new AbortController();
  prefetching = { ref: nextTrack.ref, ctl };
  const timeout = setTimeout(() => ctl.abort(), 20_000);
  api.listen(nextTrack.nd_song_id ? `nd:${nextTrack.nd_song_id}` : nextTrack.ref, ctl.signal).catch(() => {
    /* the track itself will show the error when it is reached */
  }).finally(() => clearTimeout(timeout));
}

// --- tracks on screen -------------------------------------------------------------------------------------

/** Rows of catalog tracks currently on (or just below) the screen. */
const onScreen = new Map<Element, string>();
let screenObserver: IntersectionObserver | null = null;
let sendTimer: ReturnType<typeof setTimeout> | undefined;
let lastSent = '';
let firstRef: string | null = null;
const SEND_DELAY_MS = 300;

function observer(): IntersectionObserver | null {
  if (screenObserver || typeof IntersectionObserver === 'undefined') return screenObserver;
  screenObserver = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        const ref = (e.target as HTMLElement).dataset.listenRef;
        if (e.isIntersecting && ref) onScreen.set(e.target, ref);
        else onScreen.delete(e.target);
      }
      scheduleSend();
    },
    { rootMargin: '0px 0px 25% 0px' }, // a little ahead of scrolling down
  );
  return screenObserver;
}

function scheduleSend(now = false) {
  clearTimeout(sendTimer);
  sendTimer = setTimeout(sendOnScreen, now ? 0 : SEND_DELAY_MS);
}

/** Tells the Hub which tracks are on screen, top first (a touched one first of all); it drops the rest. */
function sendOnScreen() {
  const rows = [...onScreen].map(([el, ref]) => ({ ref, top: el.getBoundingClientRect().top }));
  rows.sort((a, b) => a.top - b.top);
  const refs = [...new Set([...(firstRef && rows.some((r) => r.ref === firstRef) ? [firstRef] : []), ...rows.map((r) => r.ref)])];
  const key = refs.join('|');
  if (key === lastSent) return;
  lastSent = key;
  api.listenPrefetch(refs).catch(() => {
    lastSent = ''; // try again with the next change
  });
}

/** Watches a catalog track's row: while it is on screen, the Hub picks its file in the background. Returns the
 * cleanup for an effect. */
export function watchOnScreen(el: HTMLElement, ref: string): () => void {
  const o = observer();
  if (!o) return () => {};
  el.dataset.listenRef = ref;
  o.observe(el);
  return () => {
    o.unobserve(el);
    if (onScreen.delete(el)) scheduleSend();
  };
}

/** A finger is down on this track: it goes first in the Hub's background picks (the tap itself comes ~0.1–0.3 s
 * later). Not a pick of its own: a touch that turns into a scroll costs nothing extra. */
export function touchTrack(ref: string) {
  if (firstRef === ref) return;
  firstRef = ref;
  scheduleSend(true);
}

/** Tap on a catalog track: starts it (with `queue`, the list it belongs to, playing on after it), or
 * pauses/resumes it if it is the current one. `origin` — where the list was launched (by default by `context`). */
export function listen(track: ListenTrack, queue?: readonly ListenTrack[], context?: string, origin?: LaunchOrigin) {
  if (state.track?.ref === track.ref && state.phase !== 'error') return toggle();
  startList(track, queue, context, origin);
}

function startList(track: ListenTrack, queue?: readonly ListenTrack[], context?: string, origin?: LaunchOrigin) {
  if (!applyingShared && !localPlaybackAllowed()) return;
  listContext = context ?? routeContext();
  listLaunch = newLaunch(listContext, origin);
  const source = queue?.some((t) => t === track || t.ref === track.ref) ? queue : [track];
  const identityIndex = source.indexOf(track);
  const at = identityIndex >= 0 ? identityIndex : source.findIndex((t) => t.ref === track.ref);
  const list = source.map((t) => ({ ...t }));
  smartHooks?.listStarted(listContext); // the previous list's smart session is over; this list may have its own
  // shuffle stays on for the next list: the tapped track first, the rest in random order
  if (state.shuffle && !listContext.startsWith('wave:') && list.length > 1) {
    unshuffled = list;
    set({ smart: false });
    start([list[at], ...shuffled(list.filter((_, i) => i !== at))], 0);
    // the smart shuffle chosen and this list is a playlist / «Понравившиеся»: the Hub orders it and puts recommendations
    // in, a moment after the tapped track has started (the plain shuffle plays until then)
    if (smartChosen && smartHooks?.canStart()) {
      set({ smart: true });
      smartHooks.begin(list, list[at]);
    }
  } else {
    unshuffled = null;
    set({ smart: false });
    start(list, at);
  }
}

/** «Слушать» of a whole list: from its first track (with shuffle on, a random one). `shuffle` sets the mode. */
export function listenAll(list: readonly ListenTrack[], opts: { shuffle?: boolean; context?: string; origin?: LaunchOrigin } = {}) {
  if (!list.length) return;
  if (opts.shuffle !== undefined) setModes({ shuffle: opts.shuffle });
  // starts over even when the first track is the one playing (listen() would pause it)
  startList(state.shuffle ? list[Math.floor(Math.random() * list.length)] : list[0], list, opts.context, opts.origin);
}

// --- the queue: shuffle, repeat, «Играть следующим», «В очередь» -----------------------------------------

/** The queue in its own order while shuffle is on (tracks added since go after it when shuffle is turned off). */
let unshuffled: readonly ListenTrack[] | null = null;

const SESSION_KEY = 'hm-listen-session:';
let sessionAccount: number | null = null;
let lastSaved = 0;

interface SavedSession {
  version: 1;
  queue: ListenTrack[];
  index: number;
  context: string;
  /** the list's launch: a reload goes on with the same one */
  launch?: Launch;
  position: number;
  autoplay: boolean;
  shuffle: boolean;
  repeat: RepeatMode;
  /** Indices retain object identity, including duplicate tracks, after JSON serialization. */
  originalOrder: number[] | null;
  smart?: boolean;
}

function persistListening(force = false) {
  if (sessionAccount === null || (!force && Date.now() - lastSaved < 1000)) return;
  try {
    const key = SESSION_KEY + sessionAccount;
    if (!state.track) localStorage.removeItem(key);
    else {
      const saved: SavedSession = {
        version: 1, queue: [...state.queue], index: state.index, context: state.context, launch: listLaunch,
        position: state.position, autoplay: listenBusy(), shuffle: state.shuffle, smart: state.smart, repeat: state.repeat,
        originalOrder: unshuffled?.map((t) => state.queue.indexOf(t)).filter((i) => i >= 0) ?? null,
      };
      localStorage.setItem(key, JSON.stringify(saved));
    }
    lastSaved = Date.now();
  } catch { /* Playback remains available when storage is full or disabled. */ }
}

/** Restore only after /me confirms the account. Native Android playback owns its own session. */
export function restoreListening(account: number | null) {
  if (sessionAccount === account) return;
  stopSharedPlayback();
  if (sessionAccount !== null) stopListening();
  sessionAccount = account;
  if (account !== null) startSharedPlayback(sharedAdapter, account);
  if (account === null || canPlayAll()) return;
  try {
    const saved = JSON.parse(localStorage.getItem(SESSION_KEY + account) ?? 'null') as SavedSession | null;
    const validTrack = (t: ListenTrack) => t && typeof t.ref === 'string' && !!t.ref && typeof t.title === 'string' && typeof t.artist === 'string';
    if (!saved || saved.version !== 1 || !Array.isArray(saved.queue) || !saved.queue.length || !saved.queue.every(validTrack)
      || !Number.isInteger(saved.index) || saved.index < 0 || saved.index >= saved.queue.length
      || typeof saved.context !== 'string' || !Number.isFinite(saved.position) || saved.position < 0) return;
    unshuffled = Array.isArray(saved.originalOrder) && saved.originalOrder.every((i) => Number.isInteger(i) && i >= 0 && i < saved.queue.length)
      ? saved.originalOrder.map((i) => saved.queue[i]) : null;
    listContext = saved.context;
    listLaunch = saved.launch && typeof saved.launch.id === 'string' && saved.launch.id
      ? { id: saved.launch.id, origin: typeof saved.launch.origin === 'string' ? saved.launch.origin : null }
      : newLaunch(saved.context);
    set({ shuffle: saved.shuffle === true, smart: saved.smart === true, repeat: saved.repeat === 'all' || saved.repeat === 'one' ? saved.repeat : 'off' });
    // Restored queues never resume behind an existing remote owner or after a lost lease.
    start(saved.queue, saved.index, false, saved.position, false);
    smartHooks?.restored();
  } catch { /* Ignore an incomplete or obsolete snapshot. */ }
}

const onWave = () => state.context.startsWith('wave:');

export function shuffled<T>(list: readonly T[]): T[] {
  const out = [...list];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

function setModes(patch: Partial<Pick<ListenState, 'shuffle' | 'repeat'>>) {
  set(patch);
  if (!state.shuffle) smartChosen = false;
  try {
    localStorage.setItem(MODES_KEY, JSON.stringify({ shuffle: state.shuffle, smart: smartChosen, repeat: state.repeat }));
  } catch {
    /* the modes are just not remembered */
  }
}

/** On: the tracks after the current one go in random order; off: back to the list's own order (a smart shuffle ends:
 * its recommendations leave the queue). */
export function setShuffle(on: boolean) {
  const wasSmart = state.smart;
  if (!on) smartChosen = false;
  setModes({ shuffle: on });
  if (wasSmart && !on) {
    smartHooks?.end();
    set({ smart: false });
  }
  if (!state.track || onWave()) return;
  const current = state.queue[state.index];
  if (on) {
    if (wasSmart) return;
    unshuffled = state.queue;
    set({ queue: [current, ...shuffled(state.queue.filter((_, i) => i !== state.index))], index: 0 });
  } else {
    const base = unshuffled ?? state.queue;
    const own = state.queue.filter((t) => !t.rec);
    let queue: readonly ListenTrack[];
    if (current.rec) {
      // a recommendation plays now: what is left of the list goes on after it, in the list's own order
      const left = new Set(state.queue.slice(state.index + 1).filter((t) => !t.rec));
      queue = [current, ...base.filter((t) => left.has(t)), ...[...left].filter((t) => !base.includes(t))];
    } else {
      queue = [...base.filter((t) => own.includes(t)), ...own.filter((t) => !base.includes(t))];
    }
    unshuffled = null;
    set({ queue, index: Math.max(0, queue.indexOf(current)) });
  }
  prefetchNext();
}

/** The shuffle button: off → shuffle → smart shuffle (only where the playing list can have one: a playlist or
 * «Понравившиеся», and the setting on) → off. */
export function cycleShuffle() {
  if (onWave()) return;
  if (!state.shuffle) return setShuffle(true);
  if (state.smart) return setShuffle(false);
  if (state.track && smartHooks?.canStart()) {
    smartChosen = true;
    setModes({});
    set({ smart: true });
    smartHooks.begin((unshuffled ?? state.queue).filter((t) => !t.rec), state.queue[state.index]);
    return;
  }
  setShuffle(false);
}

/** The smart shuffle's own calls (`lib/smartShuffle.ts` registers them). */
export interface SmartHooks {
  /** a new list was started: its screen's smart context (a playlist, «Понравившиеся») is noted, an old session ends */
  listStarted(context: string): void;
  /** the playing list can have a smart shuffle (and the setting is on) */
  canStart(): boolean;
  /** asks the Hub for the order and the recommendations of `list` (its own order) with `current` playing */
  begin(list: readonly ListenTrack[], current: ListenTrack): void;
  /** the smart shuffle is over: the session is dropped (the caller deals with the queue) */
  end(): void;
  /** the page restored its queue: a session of its recommendations goes on */
  restored(): void;
}
let smartHooks: SmartHooks | null = null;
export function registerSmart(hooks: SmartHooks) {
  smartHooks = hooks;
}

/** The smart shuffle chosen (the last choice of the button); `smart` of the state is whether the playing list has it. */
export function useSmartChosen(): boolean {
  return useSyncExternalStore(subscribe, () => smartChosen);
}

/** The Hub did not answer or the session is over: the plain shuffle goes on. */
export function endSmart() {
  if (state.smart) set({ smart: false });
}

/** Recommendations into the queue, each right after the own song it names (not behind the player). */
export function insertAfterOwn(items: ReadonlyArray<{ after: string; track: ListenTrack }>) {
  if (!state.track || !items.length) return;
  const queue = [...state.queue];
  let added = 0;
  for (const { after, track } of items) {
    if (queue.some((t) => t.ref === track.ref)) continue;
    const i = queue.findIndex((t) => !t.rec && idOf(t) === after);
    if (i < 0 || i < state.index) continue;
    queue.splice(i + 1, 0, track);
    added++;
  }
  if (!added) return;
  set({ queue });
  if (state.phase === 'ended' && hasNext(state)) start(state.queue, state.index + 1, true);
  else if (state.phase !== 'error') prefetchNext();
}

/** «+»: the recommendation became the list's own song (no ✨ any more). */
export function markOwn(ref: string) {
  if (!state.queue.some((t) => t.ref === ref && t.rec)) return;
  const queue = state.queue.map((t) => (t.ref === ref && t.rec ? { ...t, rec: null } : t));
  set({ queue, track: queue[state.index] ?? state.track });
}

/** off → all → one → off. */
export function cycleRepeat() {
  setModes({ repeat: state.repeat === 'off' ? 'all' : state.repeat === 'all' ? 'one' : 'off' });
  prefetchNext();
}

/** «Играть следующим»: right after the current track (when nothing plays, they start now). */
export function playNext(tracks: readonly ListenTrack[]) {
  if (!tracks.length) return;
  if (!state.track) return listen(tracks[0], tracks);
  const added = tracks.map((t) => ({ ...t })); // own objects: the same track may be in the queue twice
  set({ queue: [...state.queue.slice(0, state.index + 1), ...added, ...state.queue.slice(state.index + 1)] });
  prefetchNext();
}

/** «Добавить в очередь»: after the last track (when nothing plays, they start now). */
export function addToQueue(tracks: readonly ListenTrack[]) {
  if (!tracks.length) return;
  if (!state.track) return listen(tracks[0], tracks);
  const ended = state.phase === 'ended';
  set({ queue: [...state.queue, ...tracks.map((t) => ({ ...t }))] });
  if (ended) start(state.queue, state.index + 1, true);
  else prefetchNext();
}

/** Takes a track that is not the current one out of the queue. */
export function removeFromQueue(index: number) {
  if (index === state.index || index < 0 || index >= state.queue.length) return;
  set({ queue: state.queue.filter((_, i) => i !== index), index: index < state.index ? state.index - 1 : state.index });
  prefetchNext();
}

/** Moves a track of the queue to another place (the current one too). */
export function moveInQueue(from: number, to: number) {
  const q = [...state.queue];
  if (from === to || from < 0 || to < 0 || from >= q.length || to >= q.length) return;
  const currentIndex = state.index === from ? to
    : state.index + (from < state.index ? -1 : 0) + (to <= state.index - (from < state.index ? 1 : 0) ? 1 : 0);
  const [t] = q.splice(from, 1);
  q.splice(to, 0, t);
  set({ queue: q, index: currentIndex });
  prefetchNext();
}

/** A tap on a track of the queue: it plays now, the queue stays as it is. */
export function playQueueAt(index: number) {
  if (index === state.index) return toggle();
  if (index >= 0 && index < state.queue.length) start(state.queue, index);
}

/** More tracks of a list were loaded (search results): if it is the list playing, it plays on through them too. */
export function extendQueue(list: readonly ListenTrack[]) {
  const q = state.queue;
  // the playing queue is the list as it was: the same refs in the same order
  if (!state.track || list.length <= q.length || q.some((t, i) => t.ref !== list[i].ref)) return;
  set({ queue: [...q, ...list.slice(q.length)] });
  if (state.phase !== 'error' && state.phase !== 'ended') prefetchNext();
}

/** The tracks after the current one are replaced (the Hub picked «Моя волна» anew, `lib/wave.ts`). A queue that ran
 * out meanwhile plays on. */
export function replaceUpcoming(list: readonly ListenTrack[]) {
  if (!state.track) return;
  const ended = state.phase === 'ended';
  set({ queue: [...state.queue.slice(0, state.index + 1), ...list] });
  if (ended && hasNext(state)) start(state.queue, state.index + 1, true);
  else if (state.phase !== 'error') prefetchNext();
}

export const hasNext = (s: ListenState) => s.index + 1 < s.queue.length;
export const hasPrev = (s: ListenState) => s.index > 0;

/** Plays the next track of the queue; false at its end. */
export function next(): boolean {
  if (hasNext(state)) start(state.queue, state.index + 1);
  else if (state.repeat === 'all' && state.track && !onWave()) start(state.queue, 0);
  else return false;
  return true;
}

/** ⏮: back to the start of the track, or (near its start) the previous track. */
export function prev() {
  if (hasPrev(state) && (state.position < RESTART_AFTER_S || state.phase === 'error')) start(state.queue, state.index - 1);
  else if (state.track) {
    seek(0);
    if (state.phase === 'ended' || state.phase === 'paused') resume();
  }
}

export function toggle() {
  if (state.phase === 'playing' || state.phase === 'loading' || state.phase === 'resolving') pause();
  else resume();
}

function resume() {
  if (!applyingShared && !localPlaybackAllowed()) return;
  const a = audio;
  const track = state.track;
  if (!a || !track) return;
  if (state.phase === 'error') return start(state.queue, state.index);
  if (state.phase === 'ended') return start(state.queue, state.index);
  set({ phase: state.info || state.position ? 'loading' : 'resolving' });
  play(a);
}

/** Only canonical identifiers and display metadata cross the device boundary. */
function sharedSnapshot(): PlaybackSnapshot | null {
  if (!state.track) return null;
  if (state.queue.length > 1000) throw new Error('Общая очередь ограничена 1000 треками.');
  if (state.info?.kind === 'preview') throw new Error('Превью нельзя передать как полный трек.');
  const queue = state.queue.map((track) => {
    const nd = track.nd_song_id || (track.ref.startsWith('nd:') ? track.ref.slice(3) : null);
    if (nd && !/^[A-Za-z0-9_.-]+$/.test(nd)) throw new Error('В очереди есть локальный трек без общего идентификатора.');
    const ref = track.ref || (nd ? `nd:${nd}` : '');
    if (!/^(?:(?:dz|hm|mb|nd):[A-Za-z0-9_.-]+|file:[A-Za-z0-9_-]+:[A-Za-z0-9_.:-]+)$/.test(ref)) {
      throw new Error('Локальные файлы и ссылки нельзя передавать. Выберите треки из библиотеки или каталога.');
    }
    return { ref, nd_song_id: nd, title: track.title, artist: track.artist, album: track.album ?? '',
      duration_s: Number.isFinite(track.duration_s) ? Math.max(0, track.duration_s!) : 0 };
  });
  return {
    queue, index: state.index, occurrence,
    position: Math.max(0, pendingSeek || (audio?.currentTime ?? state.position)),
    playing: !!audio && !audio.paused && !audio.ended && audio.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA,
    volume: audio?.volume ?? state.volume, repeat: state.repeat, shuffle: state.shuffle,
  };
}

/** Readiness is actual decoded audio, not a successful metadata HTTP response. */
function sharedMediaReady(signal: AbortSignal, ready: () => boolean) {
  return new Promise<void>((resolve, reject) => {
    const finish = (error?: Error) => {
      clearInterval(timer);
      clearTimeout(timeout);
      signal.removeEventListener('abort', aborted);
      if (error) reject(error);
      else resolve();
    };
    const aborted = () => finish(new Error('Передача отменена или соединение потеряно'));
    const check = () => {
      if (signal.aborted) return aborted();
      if (audio?.error || state.phase === 'error') return finish(new Error(state.error || 'Не удалось открыть аудио'));
      if (state.info?.kind === 'preview') return finish(new Error('Доступно только превью, передача полного трека невозможна'));
      try { if (ready()) finish(); }
      catch (e) { finish(e instanceof Error ? e : new Error('Не удалось начать воспроизведение')); }
    };
    const timer = setInterval(check, 100);
    const timeout = setTimeout(() => finish(new Error('Истекло время подготовки аудио')), 20_000);
    signal.addEventListener('abort', aborted, { once: true });
    check();
  });
}

const sharedAdapter = {
  snapshot: sharedSnapshot,
  pause() {
    audio?.pause();
    pause();
  },
  async unlock() {
    const a = el();
    if (canPlayAll()) throw new Error('В приложении используйте его плеер; для передачи сюда откройте обычный браузер.');
    if (!a.getAttribute('src')) throw new Error('Сначала передайте трек сюда. Если браузер запретит звук, разрешите его этой кнопкой и повторите передачу.');
    if (!a.paused) return;
    const muted = a.muted;
    a.muted = true;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 8000);
    const abort = () => a.pause();
    ctl.signal.addEventListener('abort', abort, { once: true });
    try {
      let started = false;
      let failure: unknown;
      void a.play().then(() => { started = true; }, (e: unknown) => { failure = e; });
      await sharedMediaReady(ctl.signal, () => {
        if (failure) throw failure;
        return started && !a.paused;
      });
    } finally {
      clearTimeout(timer);
      a.pause();
      a.muted = muted;
      set({ phase: 'paused' });
    }
  },
  async execute(command: PlaybackCommand, signal: AbortSignal): Promise<PlaybackSnapshot> {
    if (canPlayAll()) throw new Error('Передача в WebView недоступна. Откройте HomeMusic в обычном браузере.');
    if (signal.aborted || Date.now() / 1000 >= command.expires_at) throw new Error('Срок команды истёк');
    const a = el();
    const abort = () => { a.pause(); resolving?.abort(); };
    signal.addEventListener('abort', abort, { once: true });
    let shouldPlay = false;
    let mustLoad = false;
    try {
      applyingShared = true;
      try {
        if (command.kind === 'prepare' || command.kind === 'start') {
          const snapshot = command.state;
          if (!snapshot?.queue.length || !snapshot.queue[snapshot.index]) throw new Error('Нет очереди для передачи');
          smartHooks?.end();
          smartChosen = false;
          unshuffled = null;
          listContext = 'other';
          listLaunch = newLaunch(listContext);
          set({ smart: false });
          setModes({ shuffle: snapshot.shuffle, repeat: snapshot.repeat });
          setVolume(snapshot.volume);
          // The source's final stop snapshot wins, including its exact occurrence and queue.
          const current = command.kind === 'start' ? sharedSnapshot() : null;
          const sameQueue = current && current.index === snapshot.index && current.occurrence === snapshot.occurrence
            && JSON.stringify(current.queue) === JSON.stringify(snapshot.queue);
          a.pause();
          if (command.kind === 'prepare' || !sameQueue) {
            start(snapshot.queue.map((track) => ({ ...track, ref: track.ref || `nd:${track.nd_song_id}` })), snapshot.index, false, snapshot.position, false);
          } else seek(snapshot.position);
          occurrence = snapshot.occurrence;
          mustLoad = true;
          shouldPlay = command.kind === 'start' && snapshot.playing;
        } else if (command.kind === 'stop' || command.kind === 'pause') {
          a.pause();
          pause();
          if (state.track) set({ position: a.currentTime, phase: 'paused' });
        } else if (command.kind === 'volume') {
          if (!Number.isFinite(command.value)) throw new Error('Не задана громкость');
          setVolume(command.value!);
        } else if (command.kind === 'seek') {
          if (!Number.isFinite(command.value)) throw new Error('Не задана позиция');
          seek(Math.max(0, command.value!));
          mustLoad = true;
        } else {
          if (!state.track) throw new Error('Очередь пуста');
          let index = state.index;
          if (command.kind === 'next') {
            if (hasNext(state)) index++;
            else if (state.repeat === 'all') index = 0;
            else throw new Error('Достигнут конец очереди');
          } else if (command.kind === 'prev' && hasPrev(state) && a.currentTime < RESTART_AFTER_S) index--;
          if (command.kind !== 'play' || state.phase === 'error' || state.phase === 'ended') {
            start(state.queue, index, false, 0, false);
          }
          mustLoad = true;
          shouldPlay = true;
        }
      } finally {
        applyingShared = false;
      }
      if (mustLoad) {
        const expectedOccurrence = occurrence;
        await sharedMediaReady(signal, () => {
          if (occurrence !== expectedOccurrence) throw new Error('Очередь изменена во время передачи');
          return !!state.info && pendingSeek === 0 && a.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA && !a.seeking;
        });
      }
      if ((command.kind === 'prepare' || command.kind === 'start') && command.state
        && Math.abs(a.currentTime - command.state.position) > 0.5) {
        throw new Error('Источник не позволяет перейти к точной позиции передаваемого трека');
      }
      if (shouldPlay) {
        if (signal.aborted || Date.now() / 1000 >= command.expires_at) throw new Error('Срок команды истёк');
        let started = false;
        let failure: unknown;
        void a.play().then(() => { started = true; }, (e: unknown) => { failure = e; });
        await sharedMediaReady(signal, () => {
          if (failure) throw new Error((failure as Error).name === 'NotAllowedError'
            ? 'Браузер запретил звук. Нажмите «Разрешить звук», затем повторите передачу или команду.'
            : 'Не удалось начать воспроизведение');
          return started && !a.paused && a.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA;
        });
      }
      const snapshot = sharedSnapshot();
      if (!snapshot) throw new Error('Очередь пуста');
      return snapshot;
    } catch (e) {
      // Abort already paused synchronously; an old account's rejected promise must not pause its successor.
      if (!signal.aborted) { a.pause(); pause(); }
      throw e;
    } finally {
      signal.removeEventListener('abort', abort);
    }
  },
};

export function pause() {
  if (!audio || !state.track || state.phase === 'error' || state.phase === 'ended') return;
  audio.pause();
  set({ phase: 'paused' });
}

export function setVolume(volume: number) {
  if (!Number.isFinite(volume)) return;
  const value = Math.max(0, Math.min(1, volume));
  if (audio) audio.volume = value;
  set({ volume: value });
  try { localStorage.setItem(VOLUME_KEY, String(value)); } catch { /* storage is optional */ }
}

export function seek(seconds: number) {
  if (!audio || !Number.isFinite(seconds)) return;
  if (audio.readyState === HTMLMediaElement.HAVE_NOTHING) pendingSeek = Math.max(0, seconds);
  else { pendingSeek = 0; audio.currentTime = seconds; }
  set({ position: seconds });
  persistListening(true);
}

/** Stops and hides the player. */
export function stopListening() {
  reportEnd();
  resolving?.abort();
  resolving = null;
  prefetching?.ctl.abort();
  prefetching = null;
  pendingSeek = 0;
  if (audio) {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
  }
  unshuffled = null;
  smartHooks?.end();
  set({ track: null, queue: [], index: 0, info: null, error: null, phase: 'paused', position: 0, duration: 0, smart: false });
  if ('mediaSession' in navigator) navigator.mediaSession.metadata = null;
}

/** Queues one catalog track (the Hub downloads the file that was listened to, if any). */
export async function downloadRef(ref: string): Promise<boolean> {
  const r = await startDownload({ track_refs: [ref] });
  if (r) markQueued(ref);
  return !!r;
}

/** A download of this catalog track was queued elsewhere: rows show «качается». */
export function markQueued(ref: string) {
  set({ downloads: new Map(state.downloads).set(ref, 'queued') });
}

/** The app's player queued a download of a catalog track: «Скачать в библиотеку» — rows show «качается»; its ♥
 * (`star`) — only the ♥ shows it (`lib/stars.ts`). */
export const HUB_DOWNLOAD_EVENT = 'hm:hub-download';

window.addEventListener(HUB_DOWNLOAD_EVENT, (e) => {
  const d = (e as CustomEvent<{ ref?: string; star?: boolean }>).detail;
  if (d?.ref && !d.star) markQueued(d.ref);
});

hubEvents.on('download', (d) => {
  const ref = d.track_ref;
  // a download queued only for ♥ is never «качается», but once imported the track is in the library
  if (ref && d.star && d.status === 'done' && state.downloads.get(ref) !== 'done') {
    set({ downloads: new Map(state.downloads).set(ref, 'done') });
    return;
  }
  if (!ref || state.downloads.get(ref) !== 'queued') return;
  const next = new Map(state.downloads);
  if (d.status === 'done') next.set(ref, 'done');
  else if (d.status === 'failed' || d.status === 'cancelled' || d.status === 'needs_review') next.delete(ref);
  else return;
  set({ downloads: next });
});

/** Lock screen / headset controls in a phone browser (the app's WebView ignores them). */
function mediaSession(track: ListenTrack) {
  if (!('mediaSession' in navigator) || typeof MediaMetadata === 'undefined') return;
  const ms = navigator.mediaSession;
  ms.metadata = new MediaMetadata({
    title: track.title,
    artist: track.artist,
    artwork: track.cover_url ? [{ src: track.cover_url, sizes: '500x500' }] : [],
  });
  try {
    ms.setActionHandler('play', resume);
    ms.setActionHandler('pause', pause);
    ms.setActionHandler('seekto', (d) => d.seekTime !== undefined && seek(d.seekTime));
    ms.setActionHandler('nexttrack', hasNext(state) || state.repeat === 'all' ? () => void next() : null);
    ms.setActionHandler('previoustrack', prev);
  } catch {
    /* an action the browser doesn't know */
  }
}
