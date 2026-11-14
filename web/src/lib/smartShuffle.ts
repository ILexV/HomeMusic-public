import { useEffect, useSyncExternalStore } from 'react';
import { ApiError, api, errorMessage } from '../api/client';
import type { PlayEventIn, SmartOut, SmartRecOut } from '../api/types';
import { STANDALONE } from './embedded';
import {
  endSmart,
  getListenState,
  idOf,
  insertAfterOwn,
  markOwn,
  next,
  registerSmart,
  removeFromQueue,
  replaceUpcoming,
  useSmartChosen,
  type ListenTrack,
} from './listen';
import { onPlayEvent } from './plays';
import { toggleLike } from './stars';
import { toast, toastError } from './ui';

/** «Умное перемешивание» (MIXES.md 8): a playlist or «Понравившиеся» in the shuffle, with recommendations "in the mood
 * of the list" put between its songs — one after every 3 own songs (every 2 in a list of 15 or fewer). All the logic
 * is the Hub's (`POST /api/smart-shuffle/*`); this module keeps the session, hands the Hub the events and the place of
 * the player, and puts what it answers in the page player's queue (`lib/listen.ts`).
 *
 * The shuffle button goes off → shuffle → smart → off; smart is offered only for a list started from a playlist's or
 * «Понравившиеся» page (`useSmartPage`) and only with the setting «Умное перемешивание в режимах» on (More). The first
 * track starts at once in the plain shuffle; the Hub's order replaces what is after it a moment later. The playlist
 * itself is never changed. A recommendation is told by `ListenTrack.rec` (✨ and the line «Рекомендация на основе …»);
 * «+» puts it in the list (in «Понравившиеся» — ♥), «−» skips it and the Hub never recommends it in this list again. */

const SETTING_KEY = 'hm-smart-shuffle';

const subscribers = new Set<() => void>();
const emit = () => subscribers.forEach((fn) => fn());
const subscribe = (fn: () => void) => {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
};

/** «Умное перемешивание в режимах»: on by default (off — the button goes between shuffle and off only). */
export function smartEnabled(): boolean {
  try {
    return localStorage.getItem(SETTING_KEY) !== '0';
  } catch {
    return true;
  }
}

export function setSmartEnabled(on: boolean) {
  try {
    localStorage.setItem(SETTING_KEY, on ? '1' : '0');
  } catch {
    /* the choice is just not remembered */
  }
  emit();
}

export function useSmartEnabled(): boolean {
  return useSyncExternalStore(subscribe, smartEnabled);
}

// --- the screen's context ------------------------------------------------------------------------------------------

interface Page {
  context: string; // "liked" or "playlist:<Navidrome id>"
  title: string | null;
}

let page: Page | null = null;

/** The screen shows a playlist or «Понравившиеся»: a list started from it may be played in the smart shuffle. */
export function useSmartPage(context: string | null, title: string | null) {
  useEffect(() => {
    if (!context) return;
    page = { context, title };
    emit();
    return () => {
      if (page?.context === context) page = null;
      emit();
    };
  }, [context, title]);
}

/** The list at the top of the screen can be played in the smart shuffle (and the setting is on): the button of
 * «Перемешать» shows it. */
export function useSmartForPage(): boolean {
  const chosen = useSmartChosen();
  const enabled = useSmartEnabled();
  return useSyncExternalStore(subscribe, () => STANDALONE && enabled && chosen && page !== null);
}

// --- the session ---------------------------------------------------------------------------------------------------

interface Session {
  id: string;
  context: string;
  title: string | null;
  label: string;
  /** the Hub's recommendations of this session (refs) */
  recs: Set<string>;
  /** events of the recommendations not yet handed to the Hub */
  pending: PlayEventIn[];
  busy: boolean;
  again: boolean;
}

let session: Session | null = null;
/** The list being started: the screen's context at the moment it began (the list's screen can be left meanwhile). */
let listPage: Page | null = null;
/** Counts the starts, so that a late answer of an old one is ignored. */
let generation = 0;
const RETRY_MS = 4000;

export const labelOf = (context: string, title: string | null) =>
  context === 'liked' ? 'Рекомендация на основе любимых песен' : title ? `Рекомендация на основе «${title}»` : 'Рекомендация на основе плейлиста';

const asTrack = (r: SmartRecOut, s: Session): ListenTrack => ({
  ref: r.ref,
  nd_song_id: r.owned ? r.nd_song_id : null,
  rec: { label: s.label, session: s.id },
  explanation: r.explanation,
  title: r.title,
  artist: r.artists?.length ? r.artists.join(', ') : r.artist,
  cover_url: r.cover_url,
  cover_small_url: r.cover_small_url,
  album_ref: r.album_ref,
  artist_ref: r.artist_ref,
  artists: (r.artists?.length ? r.artists : [r.artist]).map((name) =>
    ({ name, ...(r.artists?.length && name === r.artist && r.artist_ref ? { ref: r.artist_ref } : {}) })),
  album: r.album_title ?? undefined,
  isrc: r.isrc ?? undefined,
  year: r.release_date ? Number(r.release_date.slice(0, 4)) || undefined : undefined,
  duration_s: r.duration_s,
});

/** The own song the player is at or passed last (the Hub fills the places ahead of it). */
function ownAt(): string | null {
  const st = getListenState();
  for (let i = Math.min(st.index, st.queue.length - 1); i >= 0; i--) if (!st.queue[i].rec) return idOf(st.queue[i]);
  return null;
}

function unavailable() {
  toast('Умное перемешивание сейчас недоступно: играет обычное перемешивание', 'info');
}

async function begin(list: readonly ListenTrack[], current: ListenTrack) {
  const ctx = listPage;
  if (!ctx) return endSmart();
  const mine = ++generation;
  session = null;
  try {
    const r = await api.smartStart({
      context: ctx.context, songs: list.map(idOf), current: idOf(current), title: ctx.title,
      origin: ctx.context === 'liked' ? 'liked_page' : 'playlist_page', client: 'pwa',
    });
    const st = getListenState();
    if (mine !== generation || !st.smart || !st.track) return;
    const s: Session = { id: r.session_id, context: ctx.context, title: ctx.title, label: labelOf(ctx.context, ctx.title),
      recs: new Set(), pending: [], busy: false, again: false };
    const byId = new Map(list.map((t) => [idOf(t), t]));
    // what has played or is playing stays; the Hub's order replaces what is after it
    const done = new Set(st.queue.slice(0, st.index + 1).map((t) => idOf(t)));
    const recsAfter = new Map<string, ListenTrack[]>();
    for (const rec of r.recs) {
      s.recs.add(rec.ref);
      recsAfter.set(rec.after, [...(recsAfter.get(rec.after) ?? []), asTrack(rec, s)]);
    }
    const upcoming: ListenTrack[] = [];
    for (const id of r.own) {
      const own = byId.get(id);
      if (own && !done.has(id)) upcoming.push(own);
      // a recommendation after a song that already played is not worth a place any more
      if (!done.has(id) || id === idOf(st.queue[st.index])) upcoming.push(...(recsAfter.get(id) ?? []));
    }
    session = s;
    replaceUpcoming(upcoming);
  } catch (e) {
    if (mine !== generation) return;
    endSmart();
    unavailable();
    console.warn('smart shuffle:', errorMessage(e));
  }
}

async function ask() {
  const s = session;
  if (!s) return;
  if (s.busy) {
    s.again = true;
    return;
  }
  s.busy = true;
  const played = s.pending.splice(0);
  try {
    const r: SmartOut = await api.smartNext(s.id, { played, at: ownAt() });
    if (session !== s) return;
    const fresh = r.recs.filter((x) => !s.recs.has(x.ref));
    for (const x of fresh) s.recs.add(x.ref);
    insertAfterOwn(fresh.map((x) => ({ after: x.after, track: asTrack(x, s) })));
    if (r.pending && !fresh.length) setTimeout(() => void ask(), RETRY_MS);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      session = null; // the session is over (a day without it): the queue plays out
    } else s.pending.unshift(...played); // offline: with the next call
  } finally {
    s.busy = false;
    if (s.again && session === s) {
      s.again = false;
      void ask();
    }
  }
}

onPlayEvent((e) => {
  const s = session;
  if (!s || e.kind === 'progress' || e.kind === 'interrupted' || e.kind === 'error') return;
  if ((e.target_type && e.target_type !== 'track') || e.reverts_event_id) return;
  const rec = !!e.ref && s.recs.has(e.ref);
  if (rec) s.pending.push(e);
  // the player moved on (a track started) or the user reacted to a recommendation: the Hub fills the places ahead
  if (e.kind === 'start' || (rec && (e.kind === 'finish' || e.kind === 'skip' || e.kind === 'like' || e.kind === 'dislike'))) void ask();
});

registerSmart({
  listStarted(context) {
    generation++;
    session = null;
    listPage = (context === 'playlist' || context === 'liked') && page && (page.context === 'liked') === (context === 'liked') ? page : null;
  },
  canStart() {
    if (!STANDALONE || !smartEnabled()) return false;
    // a list of a playlist's / «Понравившиеся» page, noted when it was started (or, for a restored one, the session)
    return listPage !== null && !getListenState().context.startsWith('wave:');
  },
  begin(list, current) {
    void begin(list, current);
  },
  end() {
    generation++;
    session = null;
  },
  restored() {
    // a reload: the same Hub session goes on from the recommendations in the queue
    const st = getListenState();
    const rec = st.queue.find((t) => t.rec)?.rec;
    if (!st.smart || !rec) return;
    const context = st.context === 'liked' ? 'liked' : 'playlist';
    session = { id: rec.session, context, title: null, label: rec.label, recs: new Set(st.queue.filter((t) => t.rec).map((t) => t.ref)),
      pending: [], busy: false, again: false };
    listPage = null;
  },
});

// --- «+» and «−» of a playing recommendation -----------------------------------------------------------------------

/** «+»: the recommendation joins the list (a playlist: put in it, downloaded first if the library lacks it; «Понравившиеся»:
 * ♥). It is the list's own song from now on (no ✨). */
export async function addRec(track: ListenTrack): Promise<void> {
  const s = session;
  const rec = track.rec;
  if (!rec) return;
  try {
    if (s?.context === 'liked' || (!s && getListenState().context === 'liked')) {
      await toggleLike({ targetType: 'track', songId: track.nd_song_id, ref: track.ref, data: { ...track } });
      await api.smartAdd(rec.session, { ref: track.ref, nd_song_id: track.nd_song_id });
      toast(`«${track.title}» в понравившихся`, 'success');
    } else {
      const r = await api.smartAdd(rec.session, { ref: track.ref, nd_song_id: track.nd_song_id });
      toast(r.status === 'downloading' ? `«${track.title}» скачивается и появится в плейлисте` : `«${track.title}» добавлен в плейлист`, 'success');
    }
    markOwn(track.ref);
    s?.recs.delete(track.ref);
  } catch (e) {
    toastError(e);
  }
}

/** «−»: skips the recommendation; the Hub does not recommend it in this list again (it stays everywhere else). */
export async function hideRec(track: ListenTrack): Promise<void> {
  const rec = track.rec;
  if (!rec) return;
  const st = getListenState();
  const at = st.queue.findIndex((t) => t.ref === track.ref);
  if (at === st.index) {
    if (!next()) toast('Трек скрыт из рекомендаций', 'info');
  }
  const gone = getListenState().queue.findIndex((t) => t.ref === track.ref);
  if (gone >= 0 && gone !== getListenState().index) removeFromQueue(gone);
  try {
    await api.smartHide(rec.session, { ref: track.ref, nd_song_id: track.nd_song_id });
    toast(`«${track.title}» больше не рекомендуется здесь`, 'info');
  } catch (e) {
    toastError(e);
  }
}
