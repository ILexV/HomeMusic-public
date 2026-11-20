import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { api, errorMessage, libraryCoverUrl, sessionGeneration, uploadCoverUrl } from '../api/client';
import type { LibAlbumOut, ReleaseType, UploadOut, UploadTrackIn } from '../api/types';
import { Icon } from '../components/Icon';
import { AlbumPicker, PlaylistPicker, type PlaylistPick } from '../components/pickers';
import { Button, Checkbox, Cover, ErrorState, Field, IconButton, Loading, Notice, PageHeader, ProgressBar, Radio, SectionTitle, cx } from '../components/ui';
import { fmtDuration, fmtSize, stripExt, toIntOrNull, tracksWord } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { navigate } from '../lib/router';
import { attempt, confirmDialog, toast } from '../lib/ui';
import { ReleaseTypeSelect } from './Library';

export const AUDIO_ACCEPT = 'audio/*,.mp3,.m4a,.flac,.ogg,.opus,.wav';
const MAX_SIZE = 500 * 1024 * 1024;
const AUDIO_EXT = /\.(mp3|m4a|aac|flac|ogg|oga|opus|wav)$/i;

export const isAudioFile = (f: File) => f.type.startsWith('audio/') || AUDIO_EXT.test(f.name);

// Files handed over by the Share page (Android "Share" → HomeMusic); consumed on mount.
let handoff: File[] = [];
export function queueFilesForUpload(files: File[]) {
  handoff = files;
}

interface Transfer {
  key: string;
  file: File;
  progress: number;
  error?: string;
}

interface RowState {
  include: boolean;
  title: string;
  artist: string;
  trackNo: string;
  discNo: string;
}

type CoverChoice = { kind: 'none' } | { kind: 'upload'; id: number } | { kind: 'file'; file: File; url: string };

const str = (v: unknown) => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '');

function rowFromUpload(u: UploadOut): RowState {
  return {
    include: true,
    title: str(u.tags.title) || stripExt(u.orig_name),
    artist: str(u.tags.artist) || str(u.tags.album_artist),
    trackNo: str(u.tags.track_no),
    discNo: str(u.tags.disc_no),
  };
}

export function UploadPage() {
  const pendingRes = useAsync((s) => api.uploads('uploaded', s), []);
  const [uploads, setUploads] = useState<UploadOut[]>([]);
  const [rows, setRows] = useState<Record<number, RowState>>({});
  const [transfers, setTransfers] = useState<Transfer[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);
  const coverRef = useRef<HTMLInputElement>(null);

  const [mode, setMode] = useState<'new' | 'existing'>('new');
  const [albumTitle, setAlbumTitle] = useState('');
  const [albumArtist, setAlbumArtist] = useState('');
  const [year, setYear] = useState('');
  const [genre, setGenre] = useState('');
  const [type, setType] = useState<ReleaseType>('album');
  const [prefilled, setPrefilled] = useState(false);
  const [existing, setExisting] = useState<LibAlbumOut | null>(null);
  const [albumPicker, setAlbumPicker] = useState(false);
  const [cover, setCover] = useState<CoverChoice>({ kind: 'none' });
  const [coverTouched, setCoverTouched] = useState(false);
  const [playlist, setPlaylist] = useState<{ id: string; name: string } | null>(null);
  const [plPicker, setPlPicker] = useState(false);
  const [plBusy, setPlBusy] = useState(false);
  const [committing, setCommitting] = useState(false);

  const addUploads = useCallback((list: UploadOut[]) => {
    setUploads((prev) => {
      const seen = new Set(prev.map((u) => u.id));
      return [...prev, ...list.filter((u) => !seen.has(u.id))];
    });
    setRows((prev) => {
      const next = { ...prev };
      for (const u of list) if (!next[u.id]) next[u.id] = rowFromUpload(u);
      return next;
    });
  }, []);

  useEffect(() => {
    if (pendingRes.data) addUploads(pendingRes.data);
  }, [pendingRes.data, addUploads]);

  // Pre-fill the new album form and the cover from the first file's tags (once).
  useEffect(() => {
    if (prefilled || uploads.length === 0) return;
    const t = uploads[0].tags;
    setAlbumTitle((v) => v || str(t.album));
    setAlbumArtist((v) => v || str(t.album_artist) || str(t.artist));
    setYear((v) => v || str(t.year).slice(0, 4));
    setGenre((v) => v || str(t.genre));
    setPrefilled(true);
  }, [uploads, prefilled]);

  useEffect(() => {
    if (coverTouched || cover.kind !== 'none') return;
    const withCover = uploads.find((u) => u.tags.has_cover);
    if (withCover) setCover({ kind: 'upload', id: withCover.id });
  }, [uploads, cover.kind, coverTouched]);

  useEffect(() => () => void (cover.kind === 'file' && URL.revokeObjectURL(cover.url)), [cover]);

  const startUpload = useCallback(
    async (files: File[]) => {
      const ok: File[] = [];
      for (const f of files) {
        if (f.size > MAX_SIZE) toast(`«${f.name}» больше 500 МБ — пропущен`, 'error');
        else if (!isAudioFile(f)) toast(`«${f.name}» — не аудиофайл`, 'error');
        else ok.push(f);
      }
      const batch: Transfer[] = ok.map((file, i) => ({ key: `${Date.now()}-${i}-${file.name}`, file, progress: 0 }));
      setTransfers((prev) => [...prev, ...batch]);
      // Sequential: one slow mobile uplink is better used by one file at a time.
      const generation = sessionGeneration;
      for (const t of batch) {
        if (generation !== sessionGeneration) break;
        try {
          const res = await api.uploadFiles([t.file], (p) => setTransfers((prev) => prev.map((x) => (x.key === t.key ? { ...x, progress: p } : x))));
          addUploads(res);
          setTransfers((prev) => prev.filter((x) => x.key !== t.key));
        } catch (e) {
          setTransfers((prev) => prev.map((x) => (x.key === t.key ? { ...x, error: errorMessage(e) } : x)));
        }
      }
    },
    [addUploads],
  );

  useEffect(() => {
    if (handoff.length) {
      const files = handoff;
      handoff = [];
      void startUpload(files);
    }
  }, [startUpload]);

  const removeUpload = async (u: UploadOut) => {
    const ok = await confirmDialog({ title: 'Удалить файл?', body: u.orig_name, okLabel: 'Удалить', danger: true });
    if (!ok) return;
    if (await attempt(() => api.deleteUpload(u.id))) {
      setUploads((prev) => prev.filter((x) => x.id !== u.id));
      if (cover.kind === 'upload' && cover.id === u.id) setCover({ kind: 'none' });
    }
  };

  const setRow = (id: number, patch: Partial<RowState>) => setRows((prev) => ({ ...prev, [id]: { ...prev[id], ...patch } }));

  const included = uploads.filter((u) => rows[u.id]?.include);
  const invalid = included.filter((u) => !rows[u.id].title.trim() || !rows[u.id].artist.trim());
  const albumOk = mode === 'existing' ? !!existing : !!albumTitle.trim() && !!albumArtist.trim();
  const uploadingNow = transfers.some((t) => !t.error);
  const canCommit = included.length > 0 && invalid.length === 0 && albumOk && !uploadingNow;

  const pickPlaylist = async (p: PlaylistPick) => {
    if (p.kind === 'existing') {
      setPlaylist({ id: p.playlist.id, name: p.playlist.name });
      setPlPicker(false);
      return;
    }
    setPlBusy(true);
    const created = await attempt(() => api.createPlaylist(p.name));
    setPlBusy(false);
    if (created) {
      setPlaylist({ id: created.id, name: created.name });
      setPlPicker(false);
    }
  };

  const commit = async () => {
    const tracks: UploadTrackIn[] = included.map((u) => {
      const r = rows[u.id];
      return {
        upload_id: u.id,
        title: r.title.trim(),
        artist: r.artist.trim(),
        track_no: toIntOrNull(r.trackNo),
        disc_no: toIntOrNull(r.discNo),
        genre: mode === 'new' ? genre.trim() || null : null,
      };
    });
    setCommitting(true);
    const res = await attempt(() =>
      api.commitUploads(
        {
          album:
            mode === 'existing'
              ? { mode: 'existing', album_id: existing!.id }
              : { mode: 'new', title: albumTitle.trim(), album_artist: albumArtist.trim(), year: toIntOrNull(year), genre: genre.trim() || null, release_type: type },
          tracks,
          cover_from_upload_id: cover.kind === 'upload' ? cover.id : null,
          playlist_id: playlist?.id ?? null,
        },
        cover.kind === 'file' ? cover.file : null,
      ),
    );
    setCommitting(false);
    if (!res) return;
    toast(`Импортировано: ${tracksWord(res.imported)}`, 'success');
    navigate(`/library/album/${res.album_id}`, { replace: true });
  };

  const coversAvailable = uploads.filter((u) => u.tags.has_cover);

  return (
    <div className="pb-28">
      <PageHeader title="Загрузить файлы" back="/more" />

      <div className="px-4">
        <input
          ref={fileRef}
          type="file"
          multiple
          accept={AUDIO_ACCEPT}
          className="hidden"
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            e.target.value = '';
            if (files.length) void startUpload(files);
          }}
        />
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="flex w-full flex-col items-center gap-2 rounded-2xl border-2 border-dashed border-line bg-surface px-4 py-7 text-center transition hover:border-accent"
        >
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-accent text-on-accent">
            <Icon name="upload" />
          </span>
          <span className="font-semibold">Выбрать аудиофайлы</span>
          <span className="text-xs text-muted">MP3, M4A, FLAC, OGG/Opus, WAV · до 500 МБ на файл</span>
        </button>
      </div>

      {transfers.length > 0 && (
        <ul className="mt-3 space-y-2 px-4" aria-label="Передача файлов">
          {transfers.map((t) => (
            <li key={t.key} className="card p-3">
              <div className="flex items-center gap-2 text-sm">
                <span className="min-w-0 flex-1 truncate">{t.file.name}</span>
                <span className="shrink-0 text-xs text-muted">{t.error ? '' : `${Math.round(t.progress * 100)}% · `}{fmtSize(t.file.size)}</span>
                {t.error && <IconButton icon="x" label="Убрать" onClick={() => setTransfers((prev) => prev.filter((x) => x.key !== t.key))} />}
              </div>
              {t.error ? <div className="mt-1 text-xs text-danger">{t.error}</div> : <ProgressBar value={t.progress} className="mt-2" />}
            </li>
          ))}
        </ul>
      )}

      {pendingRes.loading && !uploads.length ? (
        <Loading text="Проверяю незавершённые загрузки…" />
      ) : pendingRes.error && !uploads.length ? (
        <ErrorState text={pendingRes.error} onRetry={() => pendingRes.reload()} />
      ) : uploads.length === 0 ? (
        <p className="px-6 py-8 text-center text-sm text-muted">Загруженные файлы появятся здесь — затем выберите альбом и нажмите «Импортировать».</p>
      ) : (
        <>
          <SectionTitle>
            Файлы <span className="text-base font-normal text-dim">{included.length}/{uploads.length}</span>
          </SectionTitle>
          <ul className="space-y-2 px-4">
            {uploads.map((u) => {
              const r = rows[u.id];
              if (!r) return null;
              const bad = r.include && (!r.title.trim() || !r.artist.trim());
              return (
                <li key={u.id} className={cx('card p-3', !r.include && 'opacity-50', bad && 'ring-1 ring-danger/60')}>
                  <div className="-ml-2 -mt-2 flex items-center gap-1">
                    <Checkbox checked={r.include} onChange={(v) => setRow(u.id, { include: v })} label={`Импортировать ${u.orig_name}`} />
                    <span className="min-w-0 flex-1 truncate text-xs text-muted">
                      {u.orig_name} · {fmtSize(u.size)}
                      {u.tags.duration_s ? ` · ${fmtDuration(u.tags.duration_s)}` : ''}
                    </span>
                    <IconButton icon="trash" label={`Удалить ${u.orig_name}`} size={18} className="-mr-2 text-dim" onClick={() => removeUpload(u)} />
                  </div>
                  {r.include && (
                    <div className="grid grid-cols-[1fr_4.5rem] gap-2">
                      <Field label="Название">
                        <input className="input" value={r.title} onChange={(e) => setRow(u.id, { title: e.target.value })} />
                      </Field>
                      <Field label="№">
                        <input className="input" inputMode="numeric" value={r.trackNo} onChange={(e) => setRow(u.id, { trackNo: e.target.value.replace(/\D/g, '') })} />
                      </Field>
                      <Field label="Исполнитель">
                        <input className="input" value={r.artist} onChange={(e) => setRow(u.id, { artist: e.target.value })} />
                      </Field>
                      <Field label="Диск">
                        <input className="input" inputMode="numeric" value={r.discNo} onChange={(e) => setRow(u.id, { discNo: e.target.value.replace(/\D/g, '') })} />
                      </Field>
                    </div>
                  )}
                  {u.error && <div className="mt-2 text-xs text-danger">{u.error}</div>}
                </li>
              );
            })}
          </ul>

          <SectionTitle>Альбом</SectionTitle>
          <div className="space-y-2 px-4">
            <button type="button" className="flex min-h-11 w-full items-center gap-3 text-left" onClick={() => setMode('new')} aria-pressed={mode === 'new'}>
              <Radio checked={mode === 'new'} />
              Новый альбом
            </button>
            {mode === 'new' && (
              <div className="card space-y-3 p-3">
                <Field label="Название альбома">
                  <input className="input" value={albumTitle} onChange={(e) => setAlbumTitle(e.target.value)} />
                </Field>
                <Field label="Исполнитель альбома">
                  <input className="input" value={albumArtist} onChange={(e) => setAlbumArtist(e.target.value)} />
                </Field>
                <div className="grid grid-cols-3 gap-2">
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
              </div>
            )}
            <button type="button" className="flex min-h-11 w-full items-center gap-3 text-left" onClick={() => setMode('existing')} aria-pressed={mode === 'existing'}>
              <Radio checked={mode === 'existing'} />В существующий альбом
            </button>
            {mode === 'existing' && (
              <div className="card flex items-center gap-3 p-3">
                {existing ? (
                  <>
                    <Cover src={existing.has_cover ? libraryCoverUrl(existing.id, 100) : null} size={48} icon="disc" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate">{existing.title}</div>
                      <div className="truncate text-xs text-muted">{[existing.album_artist, existing.year, tracksWord(existing.track_count)].filter(Boolean).join(' · ')}</div>
                    </div>
                    <Button small onClick={() => setAlbumPicker(true)}>
                      Изменить
                    </Button>
                  </>
                ) : (
                  <Button small icon="search" onClick={() => setAlbumPicker(true)}>
                    Выбрать альбом
                  </Button>
                )}
              </div>
            )}
          </div>

          <SectionTitle>Обложка</SectionTitle>
          <div className="no-scrollbar flex gap-3 overflow-x-auto px-4 pb-1">
            <CoverOption selected={cover.kind === 'none'} label={mode === 'existing' ? 'Как у альбома' : 'Без обложки'} onClick={() => { setCoverTouched(true); setCover({ kind: 'none' }); }}>
              <Icon name="x" className="text-dim" />
            </CoverOption>
            {coversAvailable.map((u) => (
              <CoverOption key={u.id} selected={cover.kind === 'upload' && cover.id === u.id} label={`Из «${stripExt(u.orig_name)}»`} onClick={() => { setCoverTouched(true); setCover({ kind: 'upload', id: u.id }); }}>
                <img src={uploadCoverUrl(u.id)} alt="" className="h-full w-full object-cover" />
              </CoverOption>
            ))}
            {cover.kind === 'file' && (
              <CoverOption selected label="Своя картинка" onClick={() => coverRef.current?.click()}>
                <img src={cover.url} alt="" className="h-full w-full object-cover" />
              </CoverOption>
            )}
            <CoverOption selected={false} label="Выбрать картинку" onClick={() => coverRef.current?.click()}>
              <Icon name="image" className="text-muted" />
            </CoverOption>
            <input
              ref={coverRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                e.target.value = '';
                if (f) {
                  setCoverTouched(true);
                  setCover({ kind: 'file', file: f, url: URL.createObjectURL(f) });
                }
              }}
            />
          </div>

          <SectionTitle>Плейлист</SectionTitle>
          <div className="flex items-center gap-2 px-4">
            {playlist ? (
              <>
                <span className="min-w-0 flex-1 truncate">
                  <Icon name="playlist" size={18} className="mr-2 inline text-muted" />
                  {playlist.name}
                </span>
                <IconButton icon="x" label="Не добавлять в плейлист" onClick={() => setPlaylist(null)} />
              </>
            ) : (
              <Button small icon="playlist" onClick={() => setPlPicker(true)}>
                Добавить ещё и в плейлист
              </Button>
            )}
          </div>
        </>
      )}

      {uploads.length > 0 && (
        <div className="fixed inset-x-0 bottom-[calc(var(--tabbar-h)+var(--player-h,0px)+env(safe-area-inset-bottom))] z-30 mx-auto max-w-2xl border-t border-line bg-surface/95 px-4 py-3 backdrop-blur">
          {invalid.length > 0 && <div className="mb-2"><Notice kind="error">Заполните название и исполнителя у {tracksWord(invalid.length)}</Notice></div>}
          <Button variant="primary" block icon="check" busy={committing} disabled={!canCommit} onClick={commit}>
            Импортировать {included.length ? tracksWord(included.length) : ''}
          </Button>
        </div>
      )}

      <AlbumPicker
        open={albumPicker}
        onClose={() => setAlbumPicker(false)}
        title="Выберите альбом"
        allowCreate={false}
        onPick={(a) => {
          setExisting(a);
          setAlbumPicker(false);
        }}
      />
      <PlaylistPicker open={plPicker} onClose={() => setPlPicker(false)} onPick={pickPlaylist} busy={plBusy} title="Добавить в плейлист" />
    </div>
  );
}

function CoverOption({ selected, label, onClick, children }: { selected: boolean; label: string; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={selected} className="flex w-24 shrink-0 flex-col items-center gap-1.5 text-center">
      <span className={cx('flex aspect-square w-full items-center justify-center overflow-hidden rounded-lg bg-raised ring-2', selected ? 'ring-accent' : 'ring-transparent')}>{children}</span>
      <span className="line-clamp-2 text-xs leading-tight text-muted">{label}</span>
    </button>
  );
}
