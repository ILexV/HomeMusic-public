import { useMemo, useRef, useState } from 'react';
import { api } from '../api/client';
import type { TrackOut } from '../api/types';
import { TrackRow, playAll } from '../components/catalog';
import { LikeButton } from '../components/LikeButton';
import { PlaylistPicker, type PlaylistPick } from '../components/pickers';
import { Button, Cover, ErrorState, ExplicitBadge, IconButton, Loading, OwnedBadge } from '../components/ui';
import { startDownload } from '../lib/actions';
import { useHubEvent } from '../lib/events';
import { fmtLength, recordTypeLabel, tracksWord, yearOf } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { canPlay, canPlayAll, playList } from '../lib/player';
import { Link, enc, goBack } from '../lib/router';
import { attempt, toast } from '../lib/ui';

export function AlbumPage({ refId }: { refId: string }) {
  const [rev, setRev] = useState(0);
  const res = useAsync((s) => api.album(refId, s, rev > 0), [refId, rev]);
  const [dlBusy, setDlBusy] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [plBusy, setPlBusy] = useState(false);
  const [starBusy, setStarBusy] = useState(false);
  const [starred, setStarred] = useState(false);
  const reloadTimer = useRef<number | undefined>(undefined);

  // New imports may complete tracks of this album: refetch (debounced, bypassing the cache).
  useHubEvent('library', (e) => {
    if (e.kind !== 'imported') return;
    window.clearTimeout(reloadTimer.current);
    reloadTimer.current = window.setTimeout(() => setRev((r) => r + 1), 1500);
  });

  const tracks = useMemo(() => res.data?.tracks ?? [], [res.data]);
  const discs = useMemo(() => {
    const m = new Map<number, TrackOut[]>();
    for (const t of tracks) {
      const d = t.disc_no ?? 1;
      if (!m.has(d)) m.set(d, []);
      m.get(d)!.push(t);
    }
    return [...m.entries()].sort((a, b) => a[0] - b[0]);
  }, [tracks]);

  if (res.loading && !res.data)
    return (
      <>
        <Bar />
        <Loading />
      </>
    );
  if (res.error || !res.data)
    return (
      <>
        <Bar />
        <ErrorState text={res.error ?? 'Альбом не найден'} onRetry={() => res.reload()} />
      </>
    );

  const album = res.data;
  const year = yearOf(album);
  const total = tracks.reduce((s, t) => s + (t.duration_s ?? 0), 0);
  const missing = tracks.filter((t) => !t.owned);
  const owned = tracks.filter((t) => t.owned);
  // Older apps: owned tracks Navidrome has scanned play in the app's player, in album order.
  const playable = canPlay() && !canPlayAll() ? owned.filter((t) => t.nd_song_id) : [];
  const play = (from?: TrackOut) => playList(playable, (t) => t.nd_song_id, from);
  // The whole album (own files and the rest from sources): in the app's player (1.2.0+), in a browser in the page.
  const listenAll = tracks.length && (canPlayAll() || !canPlay()) ? () => playAll(tracks) : null;

  const markDownloading = (refs: Set<string>) =>
    res.setData((prev) => (prev ? { ...prev, tracks: (prev.tracks ?? []).map((t) => (refs.has(t.ref) ? { ...t, downloading: true } : t)) } : prev));

  const downloadAlbum = async () => {
    setDlBusy(true);
    const r = await startDownload({ album_ref: album.ref });
    setDlBusy(false);
    if (r) markDownloading(new Set(missing.map((t) => t.ref)));
  };

  const addToPlaylist = async (pick: PlaylistPick) => {
    setPlBusy(true);
    try {
      let playlistId = pick.kind === 'existing' ? pick.playlist.id : null;
      const name = pick.kind === 'existing' ? pick.playlist.name : pick.name;
      if (missing.length) {
        const r = await attempt(() =>
          api.download({ album_ref: album.ref, playlist_id: playlistId, new_playlist_name: playlistId ? null : name }),
        );
        if (!r) return;
        playlistId = playlistId ?? r.playlist_id ?? null;
        markDownloading(new Set(missing.map((t) => t.ref)));
      }
      if (!playlistId) {
        const p = await attempt(() => api.createPlaylist(name));
        if (!p) return;
        playlistId = p.id;
      }
      if (owned.length) {
        const song_ids = owned.map((t) => t.nd_song_id).filter((x): x is string => !!x);
        const library_track_ids = owned.filter((t) => !t.nd_song_id && t.library_track_id).map((t) => t.library_track_id as number);
        const pid = playlistId;
        const ok = await attempt(() => api.addToPlaylist(pid, { song_ids, library_track_ids }));
        if (!ok) return;
      }
      setPickerOpen(false);
      toast(
        missing.length ? `«${name}»: ${owned.length} добавлено, ${missing.length} появятся после скачивания` : `Добавлено в «${name}»: ${tracksWord(owned.length)}`,
        'success',
        missing.length ? { label: 'Загрузки', to: '/downloads' } : undefined,
      );
    } finally {
      setPlBusy(false);
    }
  };

  const star = async () => {
    setStarBusy(true);
    const ok = await attempt(() => api.star({ catalog_album_ref: album.ref }), 'Добавлено в коллекцию');
    setStarBusy(false);
    if (ok) setStarred(true);
  };

  return (
    <div>
      <div className="relative">
        <div className="absolute inset-0 overflow-hidden">
          {album.cover_url && <img src={album.cover_small_url || album.cover_url} alt="" className="h-full w-full scale-125 object-cover opacity-40 blur-3xl" referrerPolicy="no-referrer" />}
          <div className="absolute inset-0 bg-gradient-to-b from-transparent via-bg/50 to-bg" />
        </div>
        <Bar overlay />
        <div className="relative flex flex-col items-center px-6 pb-4 text-center">
          <Cover src={album.cover_url} alt={album.title} className="aspect-square w-[min(64vw,260px)] shadow-2xl" icon="disc" />
          <h1 className="mt-4 text-2xl font-extrabold leading-tight">{album.title}</h1>
          <LikeButton targetType="album" refId={album.ref} title={album.title} size={24}
            data={{ name: album.title, artist: album.artist, artist_ref: album.artist_ref }} />
          {album.artist_ref ? (
            <Link to={`/artist/${enc(album.artist_ref)}`} className="mt-1 font-semibold hover:underline">
              {album.artist}
            </Link>
          ) : (
            <div className="mt-1 font-semibold">{album.artist}</div>
          )}
          <div className="mt-1 flex flex-wrap items-center justify-center gap-x-1.5 text-sm text-muted">
            {album.explicit && <ExplicitBadge />}
            <span>{[recordTypeLabel(album.record_type), year, tracks.length ? tracksWord(tracks.length) : null, total ? fmtLength(total) : null].filter(Boolean).join(' · ')}</span>
          </div>
          <div className="mt-2">
            <OwnedBadge status={album.owned_status} count={album.owned_count} total={tracks.length || album.nb_tracks} />
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 px-4 pb-2">
        {playable.length > 0 && (
          <Button variant="primary" icon="play" className="col-span-2" onClick={() => play()}>
            {playable.length === tracks.length ? 'Слушать' : `Слушать ${playable.length} из ${tracks.length}`}
          </Button>
        )}
        {listenAll && (
          <Button variant="primary" icon="play" className="col-span-2" onClick={listenAll}>
            Слушать
          </Button>
        )}
        {!(playable.length > 0 && missing.length === 0) && (
          <Button variant={playable.length > 0 || listenAll ? 'secondary' : 'primary'} icon="download" className="col-span-2" busy={dlBusy} disabled={missing.length === 0} onClick={downloadAlbum}>
            {missing.length === 0 ? 'Всё скачано' : owned.length ? `Скачать недостающие ${missing.length} из ${tracks.length}` : 'Скачать альбом'}
          </Button>
        )}
        <Button icon="playlist" onClick={() => setPickerOpen(true)}>
          В плейлист
        </Button>
        <Button icon={starred ? 'check' : 'star'} busy={starBusy} onClick={star} className={starred ? '!text-accent' : ''}>
          {starred ? 'В коллекции' : 'В коллекцию'}
        </Button>
      </div>

      <section aria-label="Треки" className="pt-2">
        {discs.map(([disc, list]) => (
          <div key={disc}>
            {discs.length > 1 && <div className="px-4 pb-1 pt-3 text-sm font-semibold text-muted">Диск {disc}</div>}
            {list.map((t, i) => (
              <TrackRow key={t.ref} track={t} number={t.track_no ?? i + 1} albumDetail link={false} onPlay={() => play(t)} queue={tracks} />
            ))}
          </div>
        ))}
        {!tracks.length && <p className="px-4 py-6 text-center text-muted">Треклист недоступен</p>}
      </section>

      {album.release_date && <p className="px-4 pb-2 pt-4 text-xs text-dim">Дата выхода: {new Date(album.release_date).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' })}</p>}

      <PlaylistPicker open={pickerOpen} onClose={() => setPickerOpen(false)} onPick={addToPlaylist} busy={plBusy} defaultName={album.title} />
    </div>
  );
}

function Bar({ overlay }: { overlay?: boolean }) {
  return (
    <div className={`pt-safe ${overlay ? 'relative' : 'sticky top-0 z-20 bg-bg/90'} flex h-14 items-center px-2`}>
      <IconButton icon="back" label="Назад" onClick={() => goBack('/search')} className="bg-black/30 !text-white" />
    </div>
  );
}
