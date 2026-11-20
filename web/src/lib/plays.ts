import { api, ApiError } from '../api/client';
import type { FavoriteTarget, LaunchOrigin, PlayEventIn, PlayKind } from '../api/types';
import { canSendPlayEvent, typedFavoritesSupported } from './capabilities';
import { matchPath } from './router';

/** Listening events for mixes and «Моя волна» (MIXES.md 3.2): what the page's own player plays, skips and plays to
 * the end, and ♥ / «Не нравится» set here. They gather for a few seconds and go to the Hub in one batch
 * (`POST /api/plays/events`); a batch that didn't get through waits in local storage and goes with the next one
 * (each event has its own id, so a batch sent twice is stored once). */

export type PlayTarget = Partial<FavoriteTarget>;

const KEY = 'hm-play-events';
const EVENT_KEY = 'hm-play-event:';
const SEND_DELAY_MS = 3000;
const BATCH_SIZE = 500;

type QueuedEvent = PlayEventIn & { _account?: number | null; _rejected?: string };
let queue: QueuedEvent[] = load();
let currentAccount: number | null = null;
let batchSize = BATCH_SIZE;
let failures = 0;

export const listeningAccount = () => currentAccount;
export function setListeningAccount(id: number | null) {
  currentAccount = id;
  if (id !== null) { clearTimeout(timer); timer = setTimeout(() => void flush(), SEND_DELAY_MS); }
}
let timer: ReturnType<typeof setTimeout> | undefined;
let sending = false;

function load(): QueuedEvent[] {
  const rows = new Map<string, QueuedEvent>();
  try {
    const legacy = JSON.parse(localStorage.getItem(KEY) ?? '[]') as unknown;
    if (Array.isArray(legacy)) {
      for (const e of legacy as QueuedEvent[]) if (e.event_id) {
        localStorage.setItem(EVENT_KEY + e.event_id, JSON.stringify(e));
        rows.set(e.event_id, e);
      }
      localStorage.removeItem(KEY);
    }
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key?.startsWith(EVENT_KEY)) continue;
      const e = JSON.parse(localStorage.getItem(key) ?? 'null') as QueuedEvent | null;
      if (e?.event_id) rows.set(e.event_id, e);
    }
  } catch (e) { console.error('Could not read listening outbox', e); }
  return [...rows.values()].sort((a, b) => a.ts.localeCompare(b.ts));
}

function save(events: QueuedEvent[]) {
  try {
    for (const event of events) localStorage.setItem(EVENT_KEY + event.event_id, JSON.stringify(event));
    return true;
  } catch (e) {
    console.error('Could not persist listening outbox', e);
    return false;
  }
}

function removeStored(ids: Set<string>) {
  for (const id of ids) {
    try { localStorage.removeItem(EVENT_KEY + id); } catch (e) { console.error('Could not acknowledge listening event', e); }
  }
}

export function newEventId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    // no randomUUID outside a secure context (http on the LAN)
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  }
}

/** Where a list was started from, by the screen it was on (a mix's page: `mix:<id>`). */
export function routeContext(): string {
  const path = window.location.hash.replace(/^#/, '').split('?')[0];
  const mix = /^\/mix\/(\d+)/.exec(path);
  if (mix) return `mix:${mix[1]}`;
  if (path.startsWith('/album/') || path.startsWith('/library/') || path.startsWith('/lib/album/')) return 'album';
  if (path.startsWith('/artist/') || path.startsWith('/lib/artist/')) return 'artist';
  if (path.startsWith('/search')) return 'search';
  const playlist = matchPath('/playlist/:id', path);
  if (playlist && playlist.id.length <= 64) return `playlist:${playlist.id}`;
  if (path.startsWith('/playlist')) return 'playlist';
  if (path.startsWith('/liked')) return 'liked';
  if (path.startsWith('/player')) return 'queue';
  return 'other';
}

/** One launched list (INSIGHTS.md 6.5): every event of its tracks carries the same `launch_id`; `origin` — where it
 * was launched. Analytics only: the taste profile never reads them. */
export interface Launch {
  id: string;
  origin: LaunchOrigin | null;
}

const SCREEN_ORIGINS = new Set(['album', 'artist', 'playlist', 'liked', 'search', 'queue', 'library']);

/** Where a list of `context` was launched, when the caller doesn't say: a tap on a mix's track (its «Слушать всё»
 * says `mix_page` itself), a wave's queue, the screen of the list. */
export function launchOrigin(context: string): LaunchOrigin | null {
  if (context.startsWith('mix:')) return 'mix_page_track';
  if (context.startsWith('wave:')) return 'wave';
  if (context.startsWith('playlist:')) return 'playlist';
  return SCREEN_ORIGINS.has(context) ? context : null;
}

/** A new launch of a list of `context`; a wave's is its session (`wave:<id>`). */
export function newLaunch(context: string, origin?: LaunchOrigin | null): Launch {
  return { id: context.startsWith('wave:') ? context : newEventId(), origin: origin ?? launchOrigin(context) };
}

const listeners = new Set<(e: PlayEventIn) => void>();

/** Every event as it is made (`lib/wave.ts` hands the ones of its tracks to the Hub's wave too). */
export function onPlayEvent(fn: (e: PlayEventIn) => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function playEvent(
  kind: PlayKind,
  target: PlayTarget,
  extra: Partial<Pick<PlayEventIn, 'position_s' | 'duration_s' | 'context' | 'event_id' | 'ts' |
    'reverts_event_id' | 'playback_id' | 'play_trigger' | 'device_id' | 'listened_ms' | 'track_metadata' |
    'launch_id' | 'launch_origin'>> & { account?: number | null } = {},
) {
  if (!target.ref && !target.nd_song_id && !target.nd_album_id && !target.nd_artist_id) return false;
  const round = (x?: number | null) => (x == null || !Number.isFinite(x) ? null : Math.round(x * 10) / 10);
  const event: QueuedEvent = {
    _account: extra.account ?? currentAccount,
    event_id: extra.event_id ?? newEventId(),
    ts: extra.ts ?? new Date().toISOString(),
    kind,
    target_type: target.target_type,
    nd_album_id: target.nd_album_id,
    nd_artist_id: target.nd_artist_id,
    target_data: target.target_data,
    reverts_event_id: extra.reverts_event_id,
    ref: target.ref ?? null,
    nd_song_id: target.nd_song_id ?? null,
    playback_id: extra.playback_id,
    play_trigger: extra.play_trigger,
    device_id: extra.device_id,
    listened_ms: extra.listened_ms,
    track_metadata: extra.track_metadata,
    position_s: round(extra.position_s),
    duration_s: round(extra.duration_s),
    context: extra.context ?? null,
    client: 'pwa',
    ...(extra.launch_id ? { launch_id: extra.launch_id, launch_origin: extra.launch_origin ?? null } : {}),
  };
  queue = [...queue, event];
  const persisted = save([event]);
  if (persisted && event.kind === 'progress' && event.playback_id && event.device_id && event.listened_ms != null) {
    const superseded = new Set(queue.filter((old) => old !== event && old.kind === 'progress' &&
      old._account === event._account && old.playback_id === event.playback_id && old.device_id === event.device_id &&
      old.ts.slice(0, 15) === event.ts.slice(0, 15) &&
      old.listened_ms != null && old.listened_ms <= event.listened_ms!).map((old) => old.event_id));
    removeStored(superseded);
    queue = queue.filter((old) => !superseded.has(old.event_id));
  }
  clearTimeout(timer);
  timer = setTimeout(() => void flush(), SEND_DELAY_MS);
  for (const fn of listeners) fn(event);
  return persisted;
}

async function flush() {
  if (sending || !queue.length) return;
  sending = true;
  try {
    await typedFavoritesSupported();
    const account = currentAccount;
    if (account === null) return;
    const batch = queue.filter((e) => e._account === account && !e._rejected && canSendPlayEvent(e)).slice(0, batchSize);
    if (!batch.length) return;
    const payload = batch.map(({ _account: _, _rejected: _reason, ...e }) => e);
    const response = await api.playEvents(payload, account);
    failures = 0;
    const rejected = new Map(response.rejected.map((r) => [r.index, r.detail]));
    const sent = new Set(batch.filter((_, i) => !rejected.has(i)).map((e) => e.event_id));
    const failuresById = new Map(batch.flatMap((e, i) => rejected.has(i) ? [[e.event_id, rejected.get(i)!] as const] : []));
    removeStored(sent);
    queue = queue.filter((e) => !sent.has(e.event_id)).map((e) => failuresById.has(e.event_id) ? { ...e, _rejected: failuresById.get(e.event_id) } : e);
    if (failuresById.size) console.error('Hub rejected listening events; retained in outbox', [...failuresById.values()]);
    save(queue.filter((e) => failuresById.has(e.event_id)));
    if (queue.some((e) => e._account === currentAccount && !e._rejected && canSendPlayEvent(e))) timer = setTimeout(() => void flush(), SEND_DELAY_MS);
  } catch (e) {
    if (e instanceof ApiError && e.status === 413) {
      if (batchSize > 1) { batchSize = Math.max(1, Math.floor(batchSize / 2)); timer = setTimeout(() => void flush(), SEND_DELAY_MS); }
      else {
        const oversized = queue.find((e) => e._account === currentAccount && !e._rejected && canSendPlayEvent(e));
        if (oversized) {
          oversized._rejected = 'Server rejected single event as too large (413)';
          save([oversized]);
          console.error('Listening event exceeds server limit; quarantined');
          timer = setTimeout(() => void flush(), SEND_DELAY_MS);
        }
      }
    } else {
      failures++;
      timer = setTimeout(() => void flush(), Math.min(30_000 * 2 ** Math.min(failures - 1, 6), 1_800_000));
    }
  } finally {
    sending = false;
  }
}

/** The page is going away (closed, app in the background): send what is waiting now. */
function flushOnLeave() {
  if (!queue.length || sending) return;
  clearTimeout(timer);
  const account = currentAccount;
  if (account === null) return;
  const batch = queue.filter((e) => e._account === account && !e._rejected && canSendPlayEvent(e)).slice(0, 20);
  if (!batch.length) return;
  sending = true;
  try {
    void fetch('/api/plays/events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ events: batch.map(({ _account: _, _rejected: _reason, ...e }) => e), expected_user_id: account }),
      credentials: 'include',
      keepalive: true,
    }).then(
      async (r) => {
        if (!r.ok) return;
        const response = await r.json() as { rejected: { index: number; detail: string }[] };
        const rejected = new Map(response.rejected.map((x) => [x.index, x.detail]));
        const sent = new Set(batch.filter((_, i) => !rejected.has(i)).map((e) => e.event_id));
        const failuresById = new Map(batch.flatMap((e, i) => rejected.has(i) ? [[e.event_id, rejected.get(i)!] as const] : []));
        removeStored(sent);
        queue = queue.filter((e) => !sent.has(e.event_id)).map((e) => failuresById.has(e.event_id) ? { ...e, _rejected: failuresById.get(e.event_id) } : e);
        save(queue.filter((e) => failuresById.has(e.event_id)));
      },
      () => {},
    ).finally(() => { sending = false; });
  } catch {
    sending = false;
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', flushOnLeave);
  window.addEventListener('online', () => { clearTimeout(timer); timer = setTimeout(() => void flush(), SEND_DELAY_MS); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushOnLeave();
  });
  if (queue.length) timer = setTimeout(() => void flush(), SEND_DELAY_MS);
}
