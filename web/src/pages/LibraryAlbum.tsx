import { useEffect, useRef, useState } from 'react';
import { api, libraryCoverUrl } from '../api/client';
import type { LibAlbumOut, LibTrackOut, ReleaseType } from '../api/types';
import { Icon } from '../components/Icon';
import { AlbumPicker } from '../components/pickers';
import { Button, Checkbox, Cover, ErrorState, ExplicitBadge, Field, IconButton, Loading, PageHeader, SectionTitle, Sheet, cx } from '../components/ui';
import { fmtDuration, fmtLength, providerLabel, toIntOrNull, tracksWord, warningText } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { TrackRow } from '../components/catalog';
import { LikeButton } from '../components/LikeButton';
import { TrackMenuButton } from '../components/TrackMenu';
import { listen, listenAll, type ListenTrack } from '../lib/listen';
import { canPlay, playList } from '../lib/player';
import { navigate } from '../lib/router';
import { attempt, confirmDialog, toast } from '../lib/ui';
import { ReleaseTypeSelect } from './Library';

const RELEASE_TYPES: ReleaseType[] = ['album', 'single', 'ep', 'compilation'];

export function LibraryAlbumPage({ id }: { id: number }) {
  const res = useAsync((s) => api.libAlbum(id, s), [id]);
  const [coverRev, setCoverRev] = useState(() => Date.now());
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [picker, setPicker] = useState<'move' | 'merge' | null>(null);
  const [pickBusy, setPickBusy] = useState(false);
  const [editTrack, setEditTrack] = useState<LibTrackOut | null>(null);

  if (res.loading)
    return (
      <>
        <PageHeader title="Альбом" back="/library" />
        <Loading />
      </>
    );
  if (res.error || !res.data)
    return (
      <>
        <PageHeader title="Альбом" back="/library" />
        <ErrorState text={res.error ?? 'Альбом не найден'} onRetry={() => res.reload()} />
      </>
    );

  const album = res.data;
  const tracks = [...(album.tracks ?? [])].sort((a, b) => (a.disc_no ?? 1) - (b.disc_no ?? 1) || (a.track_no ?? 0) - (b.track_no ?? 0));
  const total = tracks.reduce((s, t) => s + (t.duration_s ?? 0), 0);
  const allSelected = tracks.length > 0 && selected.size === tracks.length;
  // Tracks Navidrome has scanned play in album order: in the app's player, in a browser in the page's own one.
  const playable = tracks.filter((t) => t.nd_song_id);
  const asListen = (t: LibTrackOut): ListenTrack => ({
    ref: `nd:${t.nd_song_id}`, title: t.title, artist: t.artist, album: t.album ?? undefined, duration_s: t.duration_s,
    cover_url: album.has_cover ? libraryCoverUrl(album.id, 600, coverRev) : null,
    cover_small_url: album.has_cover ? libraryCoverUrl(album.id, 120, coverRev) : null,
  });
  const playFrom = (from?: LibTrackOut) => {
    if (canPlay()) return playList(playable, (x) => x.nd_song_id, from);
    const list = playable.map(asListen);
    if (from) listen(list[playable.indexOf(from)], list, 'album');
    else listenAll(list, { context: 'album' });
  };

  const toggle = (tid: number, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(tid);
      else next.delete(tid);
      return next;
    });

  const onPick = async (target: LibAlbumOut) => {
    if (picker === 'move') {
      const ids = [...selected];
      setPickBusy(true);
      const r = await attempt(() => api.moveTracks({ track_ids: ids, album_id: target.id }), `Перенесено в «${target.title}»: ${tracksWord(ids.length)}`);
      setPickBusy(false);
      if (r) {
        setPicker(null);
        setSelected(new Set());
        res.reload(true);
      }
    } else if (picker === 'merge') {
      const choice = await confirmDialog({
        title: 'Объединить альбомы?',
        body: `Все треки из «${album.title}» перейдут в «${target.title}» (его название, обложка и теги сохранятся). Альбом «${album.title}» исчезнет.`,
        okLabel: 'Объединить',
      });
      if (!choice) return;
      setPickBusy(true);
      const r = await attempt(() => api.mergeAlbums({ source_ids: [album.id], target_id: target.id }), 'Альбомы объединены');
      setPickBusy(false);
      if (r) {
        setPicker(null);
        navigate(`/library/album/${r.id}`, { replace: true });
      }
    }
  };

  const deleteTrack = async (t: LibTrackOut) => {
    const ok = await confirmDialog({
      title: 'Удалить трек?',
      body: `«${t.title}» — ${t.artist}\nФайл переместится в корзину и будет удалён через 30 дней.`,
      okLabel: 'Удалить',
      danger: true,
    });
    if (!ok) return;
    const r = await attempt(() => api.deleteTrack(t.id), 'Трек удалён');
    if (r) {
      res.reload(true);
      toggle(t.id, false);
    }
  };

  return (
    <div className={selected.size ? 'pb-20' : ''}>
      <PageHeader
        title={album.title}
        sub={album.album_artist}
        back="/library"
        right={
          <>
            <LikeButton targetType="album" ndId={album.nd_album_id}
              refId={album.deezer_album_id ? `dz:${album.deezer_album_id}` : null}
              title={album.title} data={{ name: album.title, artist: album.album_artist }} />
            <IconButton icon="merge" label="Объединить с другим альбомом" onClick={() => setPicker('merge')} />
          </>
        }
      />

      <AlbumForm
        album={album}
        coverSrc={album.has_cover ? libraryCoverUrl(album.id, 600, coverRev) : null}
        onSaved={() => res.reload(true)}
        onCover={(a) => {
          res.setData((prev) => ({ ...a, tracks: a.tracks ?? prev?.tracks, other_tracks: prev?.other_tracks }));
          setCoverRev(Date.now());
        }}
      />

      <SectionTitle
        right={
          tracks.length > 0 && (
            <Button small variant="ghost" onClick={() => setSelected(allSelected ? new Set() : new Set(tracks.map((t) => t.id)))}>
              {allSelected ? 'Снять выбор' : 'Выбрать все'}
            </Button>
          )
        }
      >
        Треки <span className="text-base font-normal text-dim">{tracks.length ? `${tracks.length} · ${fmtLength(total)}` : ''}</span>
      </SectionTitle>

      {playable.length > 0 && (
        <div className="px-4 pb-2">
          <Button variant="primary" icon="play" block onClick={() => playFrom()}>
            Слушать
          </Button>
        </div>
      )}

      {tracks.length === 0 ? (
        <p className="px-4 py-6 text-center text-muted">В альбоме нет треков. Перенесите их сюда из других альбомов или загрузите файлы.</p>
      ) : (
        <ul>
          {tracks.map((t) => (
            <li key={t.id} className={cx('flex items-center pr-1', selected.has(t.id) && 'bg-accent/5')}>
              <Checkbox checked={selected.has(t.id)} onChange={(v) => toggle(t.id, v)} label={`Выбрать «${t.title}»`} />
              <button type="button" className="flex min-w-0 flex-1 items-center gap-2 py-2 text-left" onClick={() => setEditTrack(t)} aria-label={`Изменить теги «${t.title}»`}>
                <span className="w-6 shrink-0 text-center text-sm tabular-nums text-dim">{t.track_no ?? '–'}</span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <span className="truncate text-[15px]">{t.title}</span>
                    {t.locked && (
                      <span className="shrink-0 text-dim" title="Ранняя (оригинальная) копия: повторная загрузка её не заменит" aria-label="Оригинальная копия">
                        <Icon name="lock" size={13} />
                      </span>
                    )}
                  </span>
                  <span className="flex items-center gap-1.5 text-[13px] text-muted">
                    {t.explicit && <ExplicitBadge />}
                    <span className="truncate">
                      {[t.artist !== album.album_artist ? t.artist : null, t.codec?.toUpperCase(), t.bitrate_kbps ? `${t.bitrate_kbps} kbps` : null, providerLabel(t.source)]
                        .filter(Boolean)
                        .join(' · ')}
                    </span>
                  </span>
                  {(t.warnings ?? []).map((w, i) => (
                    <span key={i} className="flex items-center gap-1 text-xs text-warn">
                      <Icon name="alert" size={12} /> {warningText(w)}
                    </span>
                  ))}
                </span>
                <span className="shrink-0 text-xs tabular-nums text-dim">{fmtDuration(t.duration_s)}</span>
              </button>
              <LikeButton songId={t.nd_song_id} refId={t.ref} title={t.title} className="w-10" />
              {playable.includes(t) && (
                <IconButton icon="play" label={`Слушать «${t.title}»`} className="!text-accent" size={20} onClick={() => playFrom(t)} />
              )}
              <IconButton icon="trash" label={`Удалить «${t.title}»`} className="text-dim hover:text-danger" size={20} onClick={() => deleteTrack(t)} />
              {t.nd_song_id && <TrackMenuButton track={{ title: t.title, artist: t.artist, nd_song_id: t.nd_song_id }} className="-ml-2 w-8" />}
            </li>
          ))}
        </ul>
      )}

      {!!album.other_tracks?.length && (
        <section aria-label="Остальные треки релиза">
          <SectionTitle>Остальные треки релиза · {album.other_tracks.length}</SectionTitle>
          {album.other_tracks.map((t) => (
            <TrackRow key={t.ref} track={t} number={t.track_no ?? undefined} albumDetail link={false} />
          ))}
        </section>
      )}

      {selected.size > 0 && (
        <div className="fixed inset-x-0 bottom-[calc(var(--tabbar-h)+var(--player-h,0px)+env(safe-area-inset-bottom))] z-30 mx-auto flex max-w-2xl items-center gap-2 border-t border-line bg-surface px-4 py-2.5">
          <IconButton icon="x" label="Снять выбор" onClick={() => setSelected(new Set())} className="-ml-2" />
          <span className="flex-1 whitespace-nowrap text-sm">Выбрано {selected.size}</span>
          <Button small variant="primary" icon="move" className="whitespace-nowrap" onClick={() => setPicker('move')}>
            Перенести в альбом…
          </Button>
        </div>
      )}

      <AlbumPicker
        open={picker !== null}
        onClose={() => setPicker(null)}
        title={picker === 'merge' ? 'Объединить с…' : `Перенести ${tracksWord(selected.size)} в…`}
        excludeIds={[album.id]}
        allowCreate={picker === 'move'}
        busy={pickBusy}
        onPick={onPick}
      />

      <TrackEditSheet
        track={editTrack}
        onClose={() => setEditTrack(null)}
        onSaved={(t) => {
          setEditTrack(null);
          res.setData((prev) => (prev ? { ...prev, tracks: (prev.tracks ?? []).map((x) => (x.id === t.id ? t : x)) } : prev));
        }}
      />
    </div>
  );
}

function AlbumForm({
  album,
  coverSrc,
  onSaved,
  onCover,
}: {
  album: LibAlbumOut;
  coverSrc: string | null;
  onSaved: (a: LibAlbumOut) => void;
  onCover: (a: LibAlbumOut) => void;
}) {
  const [title, setTitle] = useState(album.title);
  const [artist, setArtist] = useState(album.album_artist);
  const [year, setYear] = useState(album.year ? String(album.year) : '');
  const [genre, setGenre] = useState(album.genre ?? '');
  const [type, setType] = useState<ReleaseType>(RELEASE_TYPES.includes(album.release_type as ReleaseType) ? (album.release_type as ReleaseType) : 'album');
  const [busy, setBusy] = useState(false);
  const [coverBusy, setCoverBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setTitle(album.title);
    setArtist(album.album_artist);
    setYear(album.year ? String(album.year) : '');
    setGenre(album.genre ?? '');
  }, [album.id, album.title, album.album_artist, album.year, album.genre]);

  const dirty =
    title.trim() !== album.title ||
    artist.trim() !== album.album_artist ||
    toIntOrNull(year) !== (album.year ?? null) ||
    (genre.trim() || null) !== (album.genre || null) ||
    type !== (album.release_type ?? 'album');

  const save = async () => {
    setBusy(true);
    const r = await attempt(
      () => api.patchAlbum(album.id, { title: title.trim(), album_artist: artist.trim(), year: toIntOrNull(year), genre: genre.trim() || null, release_type: type }),
      'Сохранено',
    );
    setBusy(false);
    if (r) onSaved(r);
  };

  const pickCover = async (file: File | undefined) => {
    if (!file) return;
    if (!file.type.startsWith('image/')) {
      toast('Нужна картинка (JPEG или PNG)', 'error');
      return;
    }
    setCoverBusy(true);
    const r = await attempt(() => api.setAlbumCover(album.id, file), 'Обложка обновлена');
    setCoverBusy(false);
    if (r) onCover(r);
  };

  return (
    <div className="px-4">
      <div className="flex gap-4">
        <div className="relative w-32 shrink-0">
          <Cover src={coverSrc} alt="Обложка" className="aspect-square w-full" icon="disc" />
          <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={(e) => void pickCover(e.target.files?.[0]).finally(() => (e.target.value = ''))} />
          <Button small className="mt-2 w-full" icon="image" busy={coverBusy} onClick={() => fileRef.current?.click()}>
            Обложка
          </Button>
        </div>
        <div className="min-w-0 flex-1 space-y-3">
          <Field label="Название">
            <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field label="Исполнитель альбома">
            <input className="input" value={artist} onChange={(e) => setArtist(e.target.value)} />
          </Field>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-3">
        <Field label="Год">
          <input className="input" inputMode="numeric" value={year} onChange={(e) => setYear(e.target.value.replace(/\D/g, '').slice(0, 4))} />
        </Field>
        <Field label="Жанр">
          <input className="input" value={genre} onChange={(e) => setGenre(e.target.value)} />
        </Field>
        <Field label="Тип">
          <ReleaseTypeSelect value={type} onChange={setType} />
        </Field>
      </div>
      {dirty && (
        <div className="mt-3 flex justify-end">
          <Button variant="primary" small icon="check" busy={busy} disabled={!title.trim() || !artist.trim()} onClick={save}>
            Сохранить
          </Button>
        </div>
      )}
    </div>
  );
}

function TrackEditSheet({ track, onClose, onSaved }: { track: LibTrackOut | null; onClose: () => void; onSaved: (t: LibTrackOut) => void }) {
  const [title, setTitle] = useState('');
  const [artist, setArtist] = useState('');
  const [trackNo, setTrackNo] = useState('');
  const [discNo, setDiscNo] = useState('');
  const [genre, setGenre] = useState('');
  const [explicit, setExplicit] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!track) return;
    setTitle(track.title);
    setArtist(track.artist);
    setTrackNo(track.track_no ? String(track.track_no) : '');
    setDiscNo(track.disc_no ? String(track.disc_no) : '');
    setGenre('');
    setExplicit(!!track.explicit);
  }, [track]);

  const save = async () => {
    if (!track) return;
    setBusy(true);
    const r = await attempt(
      () =>
        api.patchTrack(track.id, {
          title: title.trim(),
          artist: artist.trim(),
          track_no: toIntOrNull(trackNo),
          disc_no: toIntOrNull(discNo),
          genre: genre.trim() || undefined,
          explicit,
        }),
      'Теги сохранены',
    );
    setBusy(false);
    if (r) onSaved(r);
  };

  return (
    <Sheet
      open={!!track}
      onClose={onClose}
      title="Теги трека"
      footer={
        <Button variant="primary" block busy={busy} disabled={!title.trim() || !artist.trim()} onClick={save}>
          Сохранить
        </Button>
      }
    >
      <div className="space-y-4">
        <Field label="Название">
          <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field label="Исполнитель">
          <input className="input" value={artist} onChange={(e) => setArtist(e.target.value)} />
        </Field>
        <div className="grid grid-cols-3 gap-3">
          <Field label="№ трека">
            <input className="input" inputMode="numeric" value={trackNo} onChange={(e) => setTrackNo(e.target.value.replace(/\D/g, ''))} />
          </Field>
          <Field label="Диск">
            <input className="input" inputMode="numeric" value={discNo} onChange={(e) => setDiscNo(e.target.value.replace(/\D/g, ''))} />
          </Field>
          <Field label="Жанр">
            <input className="input" value={genre} onChange={(e) => setGenre(e.target.value)} placeholder="без изм." />
          </Field>
        </div>
        <label className="flex min-h-11 items-center gap-3">
          <input type="checkbox" className="h-5 w-5 accent-[#1ed760]" checked={explicit} onChange={(e) => setExplicit(e.target.checked)} />
          <span>Explicit (18+)</span>
        </label>
        {track && <p className="break-all text-xs text-dim">{track.path}</p>}
      </div>
    </Sheet>
  );
}
