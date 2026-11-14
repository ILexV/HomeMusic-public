import { useEffect, useRef, useState } from 'react';
import type { AlbumOut, CatalogArtist, TrackOut } from '../api/types';
import { fmtDuration, recordTypeLabel, yearOf } from '../lib/format';
import { downloadRef, listen, listenAll, shuffled, touchTrack, useListen, watchOnScreen, type ListenTrack } from '../lib/listen';
import { canPlay, canPlayAll, playSongs, playTracks } from '../lib/player';
import { STANDALONE } from '../lib/embedded';
import { Link, enc } from '../lib/router';
import { Icon } from './Icon';
import { LikeButton } from './LikeButton';
import { TrackMenuButton, type MenuExtra } from './TrackMenu';
import { Cover, ExplicitBadge, IconButton, OwnedBadge, Spinner, cx } from './ui';

export function TrackStatus({
  owned,
  downloading,
  onDownload,
  onPlay,
  busy,
}: {
  owned: boolean;
  downloading: boolean;
  onDownload: () => void;
  /** Owned and playable in the app's player: a play button instead of the «есть» mark. */
  onPlay?: () => void;
  busy?: boolean;
}) {
  if (owned && onPlay) return <IconButton icon="play" label="Слушать" onClick={onPlay} className="!text-accent" />;
  if (owned)
    return (
      <span className="flex h-11 shrink-0 items-center gap-1 px-2 text-xs font-medium text-accent" title="Есть в библиотеке">
        <Icon name="check" size={16} stroke={3} />
        есть
      </span>
    );
  if (downloading)
    return (
      <span className="flex h-11 shrink-0 items-center gap-1.5 px-2 text-xs text-info" title="Скачивается">
        <Spinner size={14} />
        качается
      </span>
    );
  return <IconButton icon="download" label="Скачать трек" onClick={onDownload} busy={busy} className="text-muted hover:text-fg" />;
}

/** What the page player needs to know about a catalog track. */
export function listenTrack(t: TrackOut & { explanation?: string[] }): ListenTrack {
  return {
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
  };
}

/** «Слушать» / «Перемешать» of a whole list (an album, a playlist, «Понравившиеся»): in the app's player when it
 * plays catalog tracks (1.2.0+), otherwise in the page's own one. */
export function playAll(list: readonly TrackOut[], opts: { shuffle?: boolean; context?: string } = {}) {
  if (!list.length) return;
  if (canPlayAll()) {
    const q = opts.shuffle ? shuffled(list) : list;
    playTracks(q, q[0], opts.context);
  } else listenAll(list.map(listenTrack), opts);
}

/** ▷ for a track that is not downloaded yet (or a provider's file): listens in the page (⏸ while it plays). */
export function ListenButton({ track, onListen }: { track: { ref: string; title: string }; onListen: () => void }) {
  const phase = useListen((s) => (s.track?.ref === track.ref ? s.phase : null));
  const waiting = phase === 'resolving' || phase === 'loading';
  const on = phase === 'playing' || waiting;
  return (
    <IconButton
      icon={on ? 'pause' : 'play'}
      label={on ? 'Пауза' : `Послушать «${track.title}»`}
      busy={waiting}
      onClick={onListen}
      className={phase ? '!text-accent' : 'text-muted hover:text-fg'}
    />
  );
}

/** A catalog track with ♥, ownership status and a download button. Owned tracks play in the app's player (in a
 * browser: in the page); the rest can be listened to before downloading (a tap on the row or ▷), and the player
 * goes on through `queue` (the album or the list) after it. With app 1.2.0+ every track of the list plays in the
 * app's player; older apps and a browser play the ones that are not downloaded in the page. */
export function TrackRow({
  track,
  number,
  showCover = true,
  showAlbum = false,
  link = true,
  onPlay,
  queue,
  menuExtra,
}: {
  track: TrackOut & { nd_album_id?: string | null; nd_artist_id?: string | null };
  number?: number;
  showCover?: boolean;
  showAlbum?: boolean;
  link?: boolean;
  /** Plays with context in the app's player (the rest of the album or list); by default the track alone. */
  onPlay?: () => void;
  /** The list this row is in: the page player plays on through it. */
  queue?: readonly TrackOut[];
  /** More actions of the row's ⋯ menu (a playlist's «Убрать из плейлиста»). */
  menuExtra?: MenuExtra[];
}) {
  const [busy, setBusy] = useState(false);
  const dl = useListen((s) => s.downloads.get(track.ref));
  const phase = useListen((s) => s.track?.ref === track.ref ? s.phase : null);
  const current = phase !== null;
  const waiting = phase === 'resolving' || phase === 'loading';
  const playing = phase === 'playing' || waiting;
  const rowRef = useRef<HTMLDivElement>(null);
  // on screen: the Hub picks the file in the background, so a tap starts at once
  useEffect(() => (rowRef.current && !track.owned ? watchOnScreen(rowRef.current, track.ref) : undefined), [track.ref, track.owned]);
  const download = async () => {
    setBusy(true);
    await downloadRef(track.ref);
    setBusy(false);
  };
  const all = canPlayAll();
  const listenHere = all
    ? () => playTracks(queue?.includes(track) ? queue : [track], track)
    : () => listen(listenTrack(track), queue?.map(listenTrack));
  const songId = track.owned && canPlay() ? track.nd_song_id : null;
  // owned: the app's player; in a browser there is none, so the page plays the library file
  const play = all ? listenHere : songId ? (onPlay ?? (() => playSongs([songId]))) : track.owned && !canPlay() ? listenHere : undefined;
  const rowPlays = STANDALONE && (!track.owned || (!(link && track.album_ref) && !!play));
  const playLabel = playing ? `Пауза «${track.title}»` : `Слушать «${track.title}»`;
  const sub = [track.artists?.length ? track.artists.join(', ') : track.artist, showAlbum ? track.album_title : null].filter(Boolean).join(' · ');
  const body = (
    <>
      {number !== undefined || showCover ? (
        <span className={cx('relative flex h-12 shrink-0 items-center justify-center', number !== undefined ? 'w-7' : 'w-12')}>
          {number !== undefined ? (
            <span className={cx('text-sm tabular-nums text-dim', rowPlays && 'group-hover/track:opacity-0 group-focus-within/track:opacity-0', rowPlays && current && 'opacity-0')}>{number}</span>
          ) : <Cover src={track.cover_small_url || track.cover_url} size={48} alt="" />}
          {rowPlays && (
            <span aria-hidden="true" className={cx(
              'pointer-events-none absolute inset-0 flex items-center justify-center rounded-md transition-opacity group-hover/track:opacity-100 group-focus-within/track:opacity-100',
              number !== undefined ? 'text-fg' : 'bg-black/60 text-white',
              current ? 'opacity-100' : 'opacity-0',
            )}>
              {waiting ? <Spinner size={22} /> : <Icon name={playing ? 'pause' : 'play'} size={22} filled />}
            </span>
          )}
        </span>
      ) : null}
      <div className="min-w-0 flex-1">
        <div className={cx('truncate text-[15px]', track.owned || current ? 'text-accent' : 'text-fg')}>{track.title}</div>
        <div className="flex items-center gap-1.5 text-[13px] text-muted">
          {track.explicit && <ExplicitBadge />}
          <span className="truncate">{sub}</span>
        </div>
      </div>
      <span className="shrink-0 text-xs tabular-nums text-dim">{fmtDuration(track.duration_s)}</span>
    </>
  );
  const rowClass = cx('flex min-w-0 flex-1 items-center gap-3 py-2 text-left', rowPlays && 'cursor-pointer');
  return (
    <div ref={rowRef} className={cx('flex min-h-16 items-center gap-3 pl-4 pr-1', rowPlays && 'group/track transition-colors hover:bg-fg/5 focus-within:bg-fg/5')}>
      {!track.owned ? (
        <button type="button" onClick={listenHere} onPointerDown={() => touchTrack(track.ref)} className={rowClass} aria-label={rowPlays ? playLabel : `Послушать «${track.title}»`}>
          {body}
        </button>
      ) : link && track.album_ref ? (
        <Link to={`/album/${enc(track.album_ref)}`} className={rowClass}>
          {body}
        </Link>
      ) : play ? (
        <button type="button" onClick={play} className={rowClass} aria-label={rowPlays ? playLabel : `Слушать «${track.title}»`}>
          {body}
        </button>
      ) : (
        <div className={rowClass}>{body}</div>
      )}
      <div className="-ml-2 flex shrink-0 items-center">
        <LikeButton songId={track.owned ? track.nd_song_id : null} refId={track.ref} pending={track.star_pending} title={track.title} data={{ ...track }} className="w-10" />
        {track.owned ? (
          // a plain browser: the row itself plays it, a second ▷ would only take the title's room
          STANDALONE && !(link && track.album_ref) && play ? null : <TrackStatus owned downloading={false} onDownload={download} onPlay={play} />
        ) : (
          <>
            <ListenButton track={track} onListen={listenHere} />
            <TrackStatus owned={dl === 'done'} downloading={track.downloading || dl === 'queued'} onDownload={download} busy={busy} />
          </>
        )}
        <TrackMenuButton
          track={{
            title: track.title,
            artist: track.artists?.length ? track.artists.join(', ') : track.artist,
            ref: track.ref,
            nd_song_id: track.nd_song_id,
            artist_ref: track.artist_ref,
            album_ref: track.album_ref,
            nd_album_id: track.nd_album_id,
            nd_artist_id: track.nd_artist_id,
            listen: listenTrack(track),
          }}
          extra={menuExtra}
          className="-ml-1 w-8"
        />
      </div>
    </div>
  );
}

export function ArtistBubble({ artist }: { artist: CatalogArtist }) {
  return (
    <Link to={`/artist/${enc(artist.ref)}`} className="flex w-28 shrink-0 flex-col items-center gap-2 text-center">
      <Cover src={artist.picture_small_url || artist.picture_url} size={104} round icon="user" alt={artist.name} />
      <span className="line-clamp-2 text-sm leading-tight">{artist.name}</span>
    </Link>
  );
}

/** Release card for grids and horizontal rows; optional download button. */
export function ReleaseCard({ album, showArtist = false, onDownload, busy }: { album: AlbumOut; showArtist?: boolean; onDownload?: () => void; busy?: boolean }) {
  const year = yearOf(album);
  return (
    <div className="relative min-w-0">
      <Link to={`/album/${enc(album.ref)}`} className="block">
        <Cover src={album.cover_small_url || album.cover_url} alt={album.title} className="aspect-square w-full" />
        <div className="mt-2 line-clamp-2 text-sm font-medium leading-tight">
          {album.explicit && (
            <span className="mr-1 inline-block align-[-2px]">
              <ExplicitBadge />
            </span>
          )}
          {album.title}
        </div>
        <div className="mt-0.5 truncate text-xs text-muted">
          {[showArtist ? album.artist : null, year, recordTypeLabel(album.record_type)].filter(Boolean).join(' · ')}
        </div>
      </Link>
      <div className="mt-1.5 flex items-center justify-between gap-1">
        <OwnedBadge status={album.owned_status} count={album.owned_count} total={album.nb_tracks} />
        {onDownload && album.owned_status !== 'full' && (
          <IconButton icon="download" label={`Скачать «${album.title}»`} onClick={onDownload} busy={busy} className="-my-2 -mr-2 ml-auto text-muted" size={20} />
        )}
      </div>
    </div>
  );
}
