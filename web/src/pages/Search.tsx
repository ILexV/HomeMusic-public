import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api, errorMessage, isAbort, peekCache, withQuery } from '../api/client';
import type { Candidate, LibTrackOut, ProviderSearchOut, SearchOut, SearchTracksOut, TrackOut } from '../api/types';
import { ArtistBubble, ReleaseCard, TrackRow, listenTrack } from '../components/catalog';
import { TrackMenuButton } from '../components/TrackMenu';
import { CandidateRow, fileRef } from '../components/candidate';
import { Icon } from '../components/Icon';
import { LikeButton } from '../components/LikeButton';
import { WaveCard } from '../components/Wave';
import { Button, Empty, ErrorState, IconButton, Notice, SectionTitle, Segmented, SkeletonRows, Spinner, cx } from '../components/ui';
import { startDownload } from '../lib/actions';
import { useHubEvent } from '../lib/events';
import { fmtDuration } from '../lib/format';
import { useAsync, useDebounced, useRememberScrollX, type AsyncState } from '../lib/hooks';
import { extendQueue, markQueued, useListen } from '../lib/listen';
import { canPlay, canPlayAll, playList } from '../lib/player';
import { Link, navigate } from '../lib/router';
import { MixCard } from './Mix';

export type SearchTab = 'catalog' | 'files' | 'reuploads';

export const searchTab = (v: string | null): SearchTab => (v === 'files' || v === 'reuploads' ? v : 'catalog');

export function SearchPage({ initialQuery, initialTab }: { initialQuery: string; initialTab: SearchTab }) {
  const [q, setQ] = useState(initialQuery);
  const [tab, setTab] = useState<SearchTab>(initialTab);
  const dq = useDebounced(q.trim(), 400);
  const inputRef = useRef<HTMLInputElement>(null);
  const [historyError, setHistoryError] = useState<string>();

  // Keep the query in the URL so "back" from an artist/album restores the results.
  useEffect(() => {
    const target = withQuery('/search', { q: dq, tab: tab === 'catalog' ? undefined : tab });
    if (window.location.hash !== '#' + target) navigate(target, { replace: true });
  }, [dq, tab]);

  // Record the query even if its search results are already cached. Tabs share one history.
  useEffect(() => {
    if (dq.length < 2) return;
    let active = true;
    api.rememberSearch(dq).then(
      () => { if (active) setHistoryError(undefined); },
      (e) => { if (active) setHistoryError(`Не удалось сохранить запрос в истории. ${errorMessage(e)}`); },
    );
    return () => { active = false; };
  }, [dq]);

  // External navigation to /search?q=… (links, tab bar) replaces the query.
  useEffect(() => {
    setQ((cur) => (cur.trim() === initialQuery ? cur : initialQuery));
  }, [initialQuery]);
  useEffect(() => setTab(initialTab), [initialTab]);

  useEffect(() => {
    if (!initialQuery) inputRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div>
      <header className="pt-safe sticky top-0 z-20 bg-bg/95 px-4 pb-3 backdrop-blur">
        <h1 className="pb-3 pt-4 text-2xl font-bold">Поиск</h1>
        <div className="relative">
          <Icon name="search" size={20} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-dim" />
          <input
            ref={inputRef}
            type="search"
            maxLength={512}
            enterKeyHint="search"
            className="input h-12 rounded-full pl-11 pr-11"
            placeholder="Исполнитель, альбом или трек"
            aria-label="Поиск"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
          />
          {q && (
            <IconButton
              icon="x"
              label="Очистить"
              className="absolute right-0.5 top-1/2 -translate-y-1/2 text-muted"
              onClick={() => {
                setQ('');
                inputRef.current?.focus();
              }}
            />
          )}
        </div>
        <div className="mt-3">
          <Segmented<SearchTab>
            label="Где искать"
            value={tab}
            onChange={setTab}
            options={[
              { value: 'catalog', label: 'Каталог' },
              { value: 'files', label: 'Файлы' },
              { value: 'reuploads', label: 'Без цензуры' },
            ]}
          />
        </div>
      </header>

      {historyError && <div className="px-4 pb-3"><Notice>{historyError}</Notice></div>}

      {!dq ? (
        <>
          <MixCard />
          <WaveCard />
          <RecentSearches onPick={setQ} />
        </>
      ) : tab === 'catalog' ? (
        <CatalogResults q={dq} />
      ) : tab === 'files' ? (
        <ProviderResults q={dq} />
      ) : (
        <ReuploadResults q={dq} />
      )}
    </div>
  );
}

function RecentSearches({ onPick }: { onPick: (q: string) => void }) {
  const res = useAsync((s) => api.searchHistory(s), []);
  const [expanded, setExpanded] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState<string>();
  useHubEvent('search_history', () => res.reload(true));
  useHubEvent('reconnect', () => res.reload(true));
  useEffect(() => {
    const wake = () => { if (document.visibilityState === 'visible') res.reload(true); };
    document.addEventListener('visibilitychange', wake);
    window.addEventListener('hm:resume', wake);
    window.addEventListener('focus', wake);
    window.addEventListener('online', wake);
    return () => {
      document.removeEventListener('visibilitychange', wake);
      window.removeEventListener('hm:resume', wake);
      window.removeEventListener('focus', wake);
      window.removeEventListener('online', wake);
    };
  }, [res.reload]);

  const clear = async () => {
    setClearing(true);
    setClearError(undefined);
    try {
      await api.clearSearchHistory();
      res.reload(true);
    } catch (e) {
      setClearError(`Не удалось очистить историю. ${errorMessage(e)}`);
    } finally {
      setClearing(false);
    }
  };

  const recent = res.data?.queries ?? [];
  if (!res.data && res.loading) return <SkeletonRows n={3} />;
  if (res.error && !recent.length) return <ErrorState text={res.error} onRetry={() => res.reload()} />;
  if (!recent.length) {
    return <Empty icon="search" title="Что будем слушать?" text="Ищите по каталогу Deezer и hitmoz, по своей библиотеке или по файлам в источниках." />;
  }
  return (
    <div>
      <SectionTitle
        right={
          <Button
            small
            variant="ghost"
            disabled={clearing}
            onClick={() => void clear()}
          >
            Очистить
          </Button>
        }
      >
        Недавние запросы
      </SectionTitle>
      {(clearError || res.error) && <div className="px-4 pb-3"><Notice>{clearError || res.error}</Notice></div>}
      <ul id="recent-searches-list">
        {(expanded ? recent : recent.slice(0, 8)).map((r) => (
          <li key={r}>
            <button type="button" className="flex min-h-12 w-full items-center gap-3 px-4 text-left hover:bg-fg/5" onClick={() => onPick(r)}>
              <Icon name="search" size={18} className="text-dim" />
              <span className="truncate">{r}</span>
            </button>
          </li>
        ))}
      </ul>
      {recent.length > 8 && (
        <div className="px-4 py-2">
          <Button variant="ghost" aria-expanded={expanded} aria-controls="recent-searches-list" onClick={() => setExpanded((v) => !v)}>
            {expanded ? 'Свернуть' : 'Показать ещё'}
          </Button>
        </div>
      )}
    </div>
  );
}

/** Tracks of the list from its end at which the next page starts loading. */
const MORE_AT = 10;

interface MoreTracks {
  q: string;
  first: SearchOut | undefined;
  pages: TrackOut[][];
  next: number | null;
  loading: boolean;
  failed: boolean;
}

/** Pages loaded earlier for this search are still in the request cache: "back" to the results gets them (and the
 * scroll position) again at once. */
function restoreMore(q: string, first: SearchOut | undefined): MoreTracks {
  const pages: TrackOut[][] = [];
  let next = first?.tracks_next ?? null;
  for (let p: SearchTracksOut | undefined; next !== null && (p = peekCache(withQuery('/api/search/tracks', { q, offset: next }))); ) {
    pages.push(p.tracks);
    next = p.next_offset ?? null;
  }
  return { q, first, pages, next, loading: false, failed: false };
}

/** The search's tracks, more of them as the list is scrolled (GET /api/search/tracks): the next page is asked for
 * when the row `MORE_AT` from the end comes into view, until the catalog has no more. */
function useMoreTracks(q: string, first: SearchOut | undefined) {
  const [st, setSt] = useState(() => restoreMore(q, first));
  const ctl = useRef<AbortController | null>(null);
  let cur = st;
  if (st.q !== q || st.first !== first) {
    cur = restoreMore(q, first);
    setSt(cur);
  }
  // a new search: the page being loaded for the old one is not needed
  useEffect(() => () => ctl.current?.abort(), [q, first]);

  const tracks = useMemo(() => {
    const seen = new Set<string>();
    return [...(cur.first?.tracks ?? []), ...cur.pages.flat()].filter((t) => !seen.has(t.ref) && !!seen.add(t.ref));
  }, [cur.first, cur.pages]);

  const loadMore = () => {
    if (cur.loading || cur.next === null) return;
    const { q: forQ, first: forFirst, next } = cur;
    const c = new AbortController();
    ctl.current?.abort();
    ctl.current = c;
    const same = (s: MoreTracks) => s.q === forQ && s.first === forFirst;
    setSt((s) => (same(s) ? { ...s, loading: true, failed: false } : s));
    api.searchTracks(forQ, next, c.signal).then(
      (p) => setSt((s) => (same(s) && !c.signal.aborted ? { ...s, pages: [...s.pages, p.tracks], next: p.next_offset ?? null, loading: false } : s)),
      (e) => {
        if (c.signal.aborted || isAbort(e)) return;
        setSt((s) => (same(s) ? { ...s, loading: false, failed: true } : s));
      },
    );
  };

  // the row MORE_AT from the end (the last one in a short list) is watched; when the list grows, the next one is
  const trigger = cur.next !== null && !cur.loading && !cur.failed ? Math.max(0, tracks.length - MORE_AT) : -1;
  const sentinel = useRef<HTMLDivElement>(null);
  const loadRef = useRef(loadMore);
  loadRef.current = loadMore;
  useEffect(() => {
    const el = sentinel.current;
    if (trigger < 0 || !el || typeof IntersectionObserver === 'undefined') return;
    // a row above the screen counts too: a fast fling may pass it between two frames
    const o = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting || e.boundingClientRect.bottom < 0)) loadRef.current();
    });
    o.observe(el);
    return () => o.disconnect();
  }, [trigger, tracks]);

  // the page's own player plays on through the tracks loaded after it started (the app's player keeps its queue)
  useEffect(() => {
    if (!canPlayAll()) extendQueue(tracks.map(listenTrack));
  }, [tracks]);

  return { tracks, trigger, sentinel, loading: cur.loading, failed: cur.failed, loadMore };
}

function CatalogResults({ q }: { q: string }) {
  const res = useAsync((s) => api.search(q, s).then((out) => ({ q, out })), [q]);
  const fresh = res.data?.q === q ? res.data.out : peekCache<SearchOut>(withQuery('/api/search', { q }));
  // while a new search loads, the results of the previous one stay on screen
  const [dataQ, data] = fresh ? [q, fresh] : res.data ? [res.data.q, res.data.out] : [q, undefined];
  const more = useMoreTracks(dataQ, data);
  const artistsRow = useRememberScrollX(`search:${q}:artists`);
  const albumsRow = useRememberScrollX(`search:${q}:albums`);
  if (res.loading && !data) return <SkeletonRows n={8} />;
  if (res.error) return <ErrorState text={res.error} onRetry={() => res.reload()} />;
  if (!data) return null;
  const empty = !data.library.length && !data.artists.length && !data.albums.length && !data.tracks.length;
  return (
    <div className="pb-4">
      {data.errors.length > 0 && (
        <div className="px-4 pt-2">
          <Notice>{data.errors.join('. ')}</Notice>
        </div>
      )}
      {empty && <Empty icon="search" title="Ничего не найдено" text="Попробуйте другое написание или поищите среди файлов в источниках." />}

      {data.library.length > 0 && (
        <section aria-label="В библиотеке">
          <SectionTitle>В библиотеке</SectionTitle>
          {data.library.slice(0, 5).map((t, _, shown) => (
            <LibraryTrackRow key={t.id} t={t} onPlay={canPlay() && t.nd_song_id ? () => playList(shown, (x) => x.nd_song_id, t) : undefined} />
          ))}
        </section>
      )}

      {data.artists.length > 0 && (
        <section aria-label="Исполнители">
          <SectionTitle>Исполнители</SectionTitle>
          <div ref={artistsRow} className="no-scrollbar flex gap-3 overflow-x-auto px-4 pb-1">
            {data.artists.map((a) => (
              <ArtistBubble key={a.ref} artist={a} />
            ))}
          </div>
        </section>
      )}

      {data.albums.length > 0 && (
        <section aria-label="Альбомы">
          <SectionTitle>Альбомы</SectionTitle>
          <div ref={albumsRow} className="no-scrollbar flex gap-4 overflow-x-auto px-4 pb-1">
            {data.albums.map((a) => (
              <div key={a.ref} className="w-36 shrink-0">
                <ReleaseCard album={a} showArtist />
              </div>
            ))}
          </div>
        </section>
      )}

      {more.tracks.length > 0 && (
        <section aria-label="Треки">
          <SectionTitle>Треки</SectionTitle>
          {more.tracks.map((t, i) => (
            <div key={t.ref}>
              {i === more.trigger && <div ref={more.sentinel} aria-hidden />}
              <TrackRow track={t} showAlbum queue={more.tracks} />
            </div>
          ))}
          {more.loading && (
            <div className="flex justify-center py-4 text-muted" role="status" aria-label="Загружаю ещё">
              <Spinner size={20} />
            </div>
          )}
          {more.failed && (
            <div className="flex items-center justify-center gap-3 px-4 py-3 text-sm text-muted">
              Не удалось загрузить ещё
              <Button small variant="ghost" onClick={more.loadMore}>
                Повторить
              </Button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function LibraryTrackRow({ t, onPlay }: { t: LibTrackOut; onPlay?: () => void }) {
  const inner = (
    <>
      <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-md bg-accent/10 text-accent">
        <Icon name="check" size={20} stroke={2.6} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[15px]">{t.title}</div>
        <div className="truncate text-[13px] text-muted">{[t.artist, t.album].filter(Boolean).join(' · ')}</div>
      </div>
      <span className="shrink-0 text-xs tabular-nums text-dim">{fmtDuration(t.duration_s)}</span>
    </>
  );
  const row = t.album_id ? (
    <Link to={`/library/album/${t.album_id}`} className="flex min-h-16 min-w-0 flex-1 items-center gap-3 py-2 pl-4 hover:bg-fg/5">
      {inner}
    </Link>
  ) : (
    <div className="flex min-h-16 min-w-0 flex-1 items-center gap-3 py-2 pl-4">{inner}</div>
  );
  return (
    <div className="flex items-center pr-1">
      {row}
      {t.nd_song_id && <TrackMenuButton track={{ title: t.title, artist: t.artist, nd_song_id: t.nd_song_id }} />}
      <LikeButton songId={t.nd_song_id} title={t.title} className={cx('w-10', !onPlay && 'mr-1')} />
      {onPlay && <IconButton icon="play" label={`Слушать «${t.title}»`} onClick={onPlay} className="!text-accent" />}
    </div>
  );
}

/** Raw files from the regular sources. */
function ProviderResults({ q }: { q: string }) {
  const res = useAsync<ProviderSearchOut>((s) => api.searchProviders(q, s, 'sources'), [q]);
  return (
    <div className="pb-4">
      <p className="px-4 pt-1 text-xs text-muted">Сырые файлы из hitmoz, hitmos.fm и YouTube. Выберите конкретный файл, если автоматический выбор не подходит; ▷ — послушать его до скачивания.</p>
      <FileResults res={res} skeleton={6} empty={<Empty icon="search" title="Файлы не найдены" />} />
    </div>
  );
}

/** Re-uploads by small channels (slower: SoundCloud answers in ~7 s). */
function ReuploadResults({ q }: { q: string }) {
  const res = useAsync<ProviderSearchOut>((s) => api.searchProviders(q, s, 'reuploads'), [q]);
  return (
    <div className="pb-4">
      <p className="px-4 pt-1 text-xs text-muted">
        Копии с маленьких каналов YouTube и SoundCloud: там выкладывают треки, которые после закона 2026 года отредактировали в официальных версиях. Перед скачиванием их можно послушать.
      </p>
      <FileResults res={res} skeleton={4} empty={<Empty icon="search" title="Перезаливов не нашлось" />} />
    </div>
  );
}

function FileResults({ res, skeleton, empty }: { res: AsyncState<ProviderSearchOut>; skeleton: number; empty: ReactNode }) {
  const [queued, setQueued] = useState<Record<string, 'busy' | 'done'>>({});

  const download = async (c: Candidate) => {
    const key = `${c.provider}:${c.provider_id}`;
    setQueued((m) => ({ ...m, [key]: 'busy' }));
    const r = await startDownload({ track_refs: [fileRef(c)] });
    if (r) markQueued(fileRef(c)); // the player shows it too
    setQueued((m) => {
      const next = { ...m };
      if (r) next[key] = 'done';
      else delete next[key];
      return next;
    });
  };

  if (res.loading) return <SkeletonRows n={skeleton} />;
  if (res.error) return <ErrorState text={res.error} onRetry={() => res.reload()} />;
  const data = res.data;
  if (!data) return null;
  return (
    <>
      {data.errors.length > 0 && (
        <div className="px-4 pt-2">
          <Notice>{data.errors.join('. ')}</Notice>
        </div>
      )}
      {!data.candidates.length && empty}
      <ul className="divide-y divide-line/60">
        {data.candidates.map((c) => {
          const key = `${c.provider}:${c.provider_id}`;
          return (
            <li key={key}>
              <CandidateRow c={c} queue={data.candidates} action={<FileAction c={c} st={queued[key]} onDownload={() => download(c)} />} />
            </li>
          );
        })}
      </ul>
    </>
  );
}

/** «Скачать этот файл», or «В очереди» once it is queued (here, or from the player while it played). */
function FileAction({ c, st, onDownload }: { c: Candidate; st?: 'busy' | 'done'; onDownload: () => void }) {
  const dl = useListen((s) => s.downloads.get(fileRef(c)));
  if (st === 'done' || dl)
    return (
      <span className="inline-flex items-center gap-1 text-sm text-accent">
        <Icon name="check" size={16} /> {dl === 'done' ? 'В библиотеке' : 'В очереди'}
      </span>
    );
  return (
    <Button small icon="download" busy={st === 'busy'} onClick={onDownload}>
      Скачать этот файл
    </Button>
  );
}
