import { api } from '../api/client';
import type { LaunchOrigin, TrackOut, WaveOut } from '../api/types';
import { newLaunch, routeContext } from './plays';

/** Playback through the HomeMusic Android app: its WebView exposes `HomeMusicAndroid.play(idsJson, index)`,
 * which queues Navidrome songs in the app's own player. A plain browser has no player here, so the play
 * buttons are not drawn there. Asked at render time, not at import, so a bridge injected later still counts.
 * Since app 1.2.0 (`playQueue`) catalog tracks that are not downloaded yet play in the app's player too, in one
 * queue with the library songs of the same list: the app has one player. Older apps and a browser play them in
 * the page itself (`lib/listen.ts`); only one of the two players plays at a time. */
type Bridge = {
  play?: (songIdsJson: string, index: number) => void;
  playQueue?: (itemsJson: string, index: number) => void;
  playWave?: (waveJson: string) => void;
  pauseNative?: () => void;
  webPlayer?: (visible: boolean) => void;
};

/** The app's player started (sent by the app, and by `playSongs` right away): page listening stops. */
export const NATIVE_PLAYING_EVENT = 'hm:native-playing';

function bridge(): Bridge | undefined {
  return (window as unknown as { HomeMusicAndroid?: Bridge }).HomeMusicAndroid;
}

export function canPlay(): boolean {
  return typeof bridge()?.play === 'function';
}

/** The app's player plays catalog tracks too (app 1.2.0+): the page plays nothing itself. */
export function canPlayAll(): boolean {
  return typeof bridge()?.playQueue === 'function';
}

/** The app's player plays «Моя волна» and asks the Hub for its next tracks itself (app 1.6.0+, `playWave`). */
export function canPlayWave(): boolean {
  return canPlayAll() && typeof bridge()?.playWave === 'function';
}

/** Hands a started wave (the answer of `POST /api/wave/start`) to the app's player. Needs `canPlayWave()`. */
export function playWave(wave: WaveOut) {
  bridge()?.playWave?.(JSON.stringify({ ...wave, initial_play_trigger: 'auto' }));
}

/** A track the Hub streams by `ref` (`/api/listen/stream`): a catalog track, or a provider's file ("file:…"). */
export interface HubItem {
  ref: string;
  title: string;
  artist: string;
  album?: string | null;
  cover_url?: string | null;
  duration_s?: number | null;
}

/** An entry of `playQueue`: a library song by its Navidrome id, or a track the Hub streams. `context` (app 1.5.0+,
 * older apps ignore it) — where the list was played from, for the app's listening events (MIXES.md 3.2);
 * `launch_id` / `launch_origin` — this launch of the list, the same on every entry (INSIGHTS.md 6.5; the app reads
 * them from INSIGHTS stage 5 on, older apps ignore them). `initial_play_trigger` is a one-shot instruction
 * only for opening this queue at its initial index; never reuse it when revisiting a queue entry. */
type QueueItem = (
  | { song_id: string }
  | { ref: string; title: string; artist: string; album: string | null; cover_url: string | null; duration_s: number | null }
) & { context?: string; launch_id?: string; launch_origin?: LaunchOrigin | null };

const absolute = (url: string | null | undefined) => (url ? new URL(url, window.location.href).href : null);

const hubItem = (t: HubItem): QueueItem => ({
  ref: t.ref,
  title: t.title,
  artist: t.artist,
  album: t.album ?? null,
  cover_url: absolute(t.cover_url),
  duration_s: t.duration_s ?? null,
});

/** Plays `list` (an album, search results…) in the app's player from `from` on: downloaded tracks from the
 * library, the rest streamed by the Hub (the file a download would take). Needs `canPlayAll()`. `context` — where
 * it was played from (`mix:<id>`…); by default the screen (`routeContext`). `origin` — where it was launched (by
 * default by `context`). */
export function playTracks(list: readonly TrackOut[], from?: TrackOut, context?: string, origin?: LaunchOrigin) {
  const items: QueueItem[] = list.map((t) =>
    t.owned && t.nd_song_id
      ? { song_id: t.nd_song_id }
      : hubItem({ ...t, artist: t.artists?.length ? t.artists.join(', ') : t.artist, album: t.album_title }),
  );
  playQueue(items, from ? list.indexOf(from) : null, context, origin);
}

/** Plays provider files the Hub streams (the Files tab of the search) in the app's player. Needs `canPlayAll()`. */
export function playHubItems(list: readonly HubItem[], index: number, context?: string) {
  playQueue(list.map(hubItem), index, context);
}

/** Plays a list of library songs and tracks the Hub streams (an import's playlist) in the app's player. Needs
 * `canPlayAll()`. */
export function playMixed(list: ReadonlyArray<{ song_id: string } | HubItem>, index: number | null, context?: string) {
  playQueue(
    list.map((t) => ('song_id' in t ? { song_id: t.song_id } : hubItem(t))),
    index,
    context,
  );
}

function playQueue(items: QueueItem[], selected: number | null, context = routeContext(), origin?: LaunchOrigin) {
  const b = bridge();
  if (!b?.playQueue || items.length === 0) return;
  window.dispatchEvent(new Event(NATIVE_PLAYING_EVENT));
  const launch = newLaunch(context, origin);
  const choseTrack = selected !== null && selected >= 0 && selected < items.length;
  const index = choseTrack ? selected : 0;
  b.playQueue(JSON.stringify(items.map((it, i) => ({ ...it, context, launch_id: launch.id, launch_origin: launch.origin,
    ...(i === index ? { initial_play_trigger: choseTrack ? 'manual' : 'auto' } : {}) }))), index);
  // The Hub starts picking the files of the first catalog tracks now (it keeps a pick for 30 min and the player's
  // request waits for the same pick), so the first one and the one after it start sooner.
  for (const it of items.slice(index)) {
    if (!('ref' in it)) continue;
    api.listen(it.ref).catch(() => {
      /* the player shows the error when it gets there */
    });
    if (it !== items[index]) break;
  }
}

/** Pauses the app's player because the page starts playing (no-op in a browser and in app versions before 1.1.1). */
export function pauseNative() {
  const b = bridge();
  if (typeof b?.pauseNative === 'function') b.pauseNative();
}

/** The page's mini player appeared or went away: the app hides its own player meanwhile (1.1.2+). */
export function webPlayerShown(visible: boolean) {
  const b = bridge();
  if (typeof b?.webPlayer === 'function') b.webPlayer(visible);
}

/** A selected song by index, or null for the entire list. Legacy `play` takes only unannotated song IDs. */
export function playSongs(ids: string[], selected: number | null) {
  const b = bridge();
  if (b?.playQueue) return playQueue(ids.map((song_id) => ({ song_id })), selected);
  if (!b?.play || ids.length === 0) return;
  window.dispatchEvent(new Event(NATIVE_PLAYING_EVENT));
  b.play(JSON.stringify(ids), selected !== null && selected >= 0 && selected < ids.length ? selected : 0);
}

/** Plays the tracks of `list` that have a Navidrome id, starting at `from` (the first one if `from` has none). */
export function playList<T>(list: T[], idOf: (t: T) => string | null | undefined, from?: T) {
  const playable = list.filter((t) => !!idOf(t));
  const start = from === undefined ? null : playable.indexOf(from);
  playSongs(
    playable.map((t) => idOf(t) as string),
    start,
  );
}
