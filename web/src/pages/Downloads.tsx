import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorMessage, isAbort } from '../api/client';
import { downloadPage, type DownloadFilter, type DownloadPage } from '../api/downloads';
import type { DownloadItemOut } from '../api/types';
import { Icon } from '../components/Icon';
import { Button, Cover, Empty, ErrorState, IconButton, PageHeader, ProgressBar, Segmented, SkeletonRows, StatusChip, cx } from '../components/ui';
import { useHubConnected, useHubEvent } from '../lib/events';
import { DOWNLOAD_ACTIVE, fmtWhen, providerLabel, warningText } from '../lib/format';
import { useAsync, useInterval } from '../lib/hooks';
import { canPlay, playList } from '../lib/player';
import { Link } from '../lib/router';
import { useSession } from '../lib/session';
import { attempt, toast } from '../lib/ui';

type LoadMode = 'refresh' | 'more';

function useDownloadHistory(filter: DownloadFilter) {
  const [data, setData] = useState<DownloadPage>();
  const [error, setError] = useState<string>();
  const [pending, setPending] = useState<LoadMode | null>('refresh');
  const current = useRef<DownloadPage | undefined>(undefined);
  const active = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const refreshPending = useRef(false);
  const timer = useRef<number | undefined>(undefined);
  const failedMode = useRef<LoadMode>('refresh');

  const load = useCallback(async (mode: LoadMode) => {
    if (!mounted.current) return;
    if (active.current) {
      if (mode === 'refresh') refreshPending.current = true;
      return;
    }
    const previous = current.current;
    if (mode === 'more' && previous?.next_cursor == null) return;
    const ctl = new AbortController();
    active.current = ctl;
    setPending(mode);
    setError(undefined);
    try {
      let page: DownloadPage;
      if (mode === 'more') {
        const next = await downloadPage(filter, previous!.next_cursor!, ctl.signal);
        page = { ...next, items: [...previous!.items, ...next.items] };
      } else {
        // Refresh through the oldest loaded id, not just page one: new arrivals must not discard
        // history the user has already opened. The server owns membership, order and all counters.
        const oldest = previous?.items.at(-1)?.id;
        page = await downloadPage(filter, undefined, ctl.signal);
        while (!ctl.signal.aborted && oldest !== undefined && page.next_cursor !== null && page.next_cursor > oldest) {
          const next = await downloadPage(filter, page.next_cursor, ctl.signal);
          page = { ...next, items: [...page.items, ...next.items] };
        }
      }
      if (!ctl.signal.aborted) {
        current.current = page;
        setData(page);
      }
    } catch (e) {
      if (!ctl.signal.aborted && !isAbort(e)) {
        failedMode.current = mode;
        setError(errorMessage(e));
      }
    } finally {
      if (!ctl.signal.aborted) {
        active.current = null;
        setPending(null);
        if (refreshPending.current) {
          refreshPending.current = false;
          scheduleRefresh();
        }
      }
    }
  }, [filter]);

  function scheduleRefresh() {
    if (!mounted.current || timer.current !== undefined) return;
    // A throttle, not a debounce: a busy queue must still update. Never cancel an in-flight
    // page append; coalesce invalidations and refresh its expanded range when it finishes.
    timer.current = window.setTimeout(() => {
      timer.current = undefined;
      void load('refresh');
    }, 500);
  }

  useEffect(() => {
    mounted.current = true;
    void load('refresh');
    return () => {
      mounted.current = false;
      active.current?.abort();
      active.current = null;
      clearTimeout(timer.current);
      timer.current = undefined;
      refreshPending.current = false;
    };
  }, [load]);

  // Events intentionally invalidate rather than upsert: an unseen id can be an old item outside
  // this page, and its previous status/visibility (needed for global counters) is unknown.
  useHubEvent('download', scheduleRefresh);
  useHubEvent('reconnect', () => void load('refresh'));
  return {
    data, error, pending,
    reload: () => void load('refresh'),
    loadMore: () => void load('more'),
    retry: () => void load(failedMode.current),
  };
}

export function DownloadsPage() {
  const { me } = useSession();
  const [filter, setFilter] = useState<DownloadFilter>('all');
  // Filter/account changes synchronously discard the previous view and abort all its requests.
  return <DownloadHistory key={`${me.id}:${filter}`} filter={filter} onFilterChange={setFilter} />;
}

function DownloadHistory({ filter, onFilterChange }: { filter: DownloadFilter; onFilterChange: (filter: DownloadFilter) => void }) {
  const res = useDownloadHistory(filter);
  const connected = useHubConnected();
  const [clearing, setClearing] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const imports = useAsync((s) => api.imports(s), []);
  useHubEvent('import', () => imports.reload(true));
  const running = (imports.data ?? []).filter((x) => x.status === 'running');
  useInterval(() => imports.reload(true), running.length ? 15_000 : null);
  const shown = res.data?.items ?? [];
  const counts = res.data?.counts ?? { all: 0, active: 0, problems: 0, finished: 0 };

  const clear = async () => {
    setClearing(true);
    const r = await attempt(() => api.clearDownloads());
    setClearing(false);
    if (r) {
      toast(r.removed ? `Убрано из списка: ${r.removed}` : 'Нечего очищать', 'success');
      res.reload();
    }
  };

  const act = async (item: DownloadItemOut, kind: 'retry' | 'cancel') => {
    setBusyId(item.id);
    const r = await attempt(() => (kind === 'retry' ? api.retry(item.id) : api.cancel(item.id)));
    setBusyId(null);
    if (r) res.reload();
  };

  return (
    <div data-testid="downloads-page" data-filter={filter}>
      <PageHeader
        title="Загрузки"
        sub={
          <span className="inline-flex items-center gap-1.5">
            <span className={cx('h-2 w-2 rounded-full', res.error ? 'bg-danger' : connected ? 'bg-accent' : 'bg-dim')} />
            {res.error ? 'нет связи' : connected ? 'онлайн' : 'переподключаюсь…'}
          </span>
        }
        right={
          <Button small variant="ghost" busy={clearing} disabled={!counts.finished} onClick={clear}>
            Очистить готовые
          </Button>
        }
      />
      {running.map((imp) => (
        <Link key={imp.id} to={`/import/${imp.id}`} className="card mx-4 mb-3 flex items-center gap-3 p-3 hover:bg-hover">
          <Icon name="transfer" size={22} className="shrink-0 text-accent" />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-semibold">Перенос из Яндекс Музыки</span>
            <span className="block truncate text-xs text-muted">
              {imp.stats.pending ? `сопоставляем · осталось ${imp.stats.pending}` : `скачано ${imp.stats.done} · качается ${imp.stats.downloading}`}
            </span>
          </span>
          <Icon name="chevron" size={18} className="text-dim" />
        </Link>
      ))}
      <div className="px-4 pb-2">
        <Segmented<DownloadFilter>
          label="Фильтр"
          value={filter}
          onChange={onFilterChange}
          options={[
            { value: 'all', label: `Все ${counts.all || ''}`.trim() },
            { value: 'active', label: `Активные ${counts.active || ''}`.trim() },
            { value: 'problems', label: `Проблемы ${counts.problems || ''}`.trim() },
          ]}
        />
      </div>

      {res.error && <ErrorState text={res.error} onRetry={res.retry} />}
      {!res.data && res.pending ? (
        <SkeletonRows />
      ) : !res.data ? null : shown.length === 0 ? (
        <Empty icon="download" title={filter === 'all' ? 'Загрузок пока нет' : 'Здесь пусто'} text={filter === 'all' ? 'Найдите трек или альбом в поиске и нажмите «Скачать».' : undefined}>
          {filter === 'all' && (
            <Link to="/search" className="mt-2 rounded-full bg-fg px-5 py-2.5 font-semibold text-bg">
              Перейти к поиску
            </Link>
          )}
        </Empty>
      ) : (
        <ul className="divide-y divide-line/60" data-testid="download-history" aria-label="История загрузок">
          {shown.map((item) => (
            <DownloadRow
              key={item.id}
              item={item}
              busy={busyId === item.id}
              onRetry={() => act(item, 'retry')}
              onCancel={() => act(item, 'cancel')}
              onPlay={canPlay() && item.status === 'done' && item.nd_song_id ? () => playList(shown.filter((x) => x.status === 'done'), (x) => x.nd_song_id, item) : undefined}
            />
          ))}
        </ul>
      )}
      {res.data?.next_cursor != null && (
        <div className="px-4 py-4">
          <Button block data-testid="downloads-load-more" busy={res.pending === 'more'} disabled={res.pending !== null} onClick={res.loadMore}>
            Загрузить ещё
          </Button>
        </div>
      )}
    </div>
  );
}

function DownloadRow({
  item,
  busy,
  onRetry,
  onCancel,
  onPlay,
}: {
  item: DownloadItemOut;
  busy: boolean;
  onRetry: () => void;
  onCancel: () => void;
  /** Done and scanned by Navidrome: plays it (and the finished ones around it) in the app's player. */
  onPlay?: () => void;
}) {
  const active = DOWNLOAD_ACTIVE.includes(item.status);
  const problem = item.status === 'failed' || item.status === 'needs_review';
  const warnings = (item.warnings ?? []).map(warningText);
  const cover = item.chosen?.cover_url;
  return (
    <li className="flex items-start gap-3 py-3 pl-4 pr-1" data-testid="download-row" data-download-id={item.id} data-status={item.status}>
      <Link to={`/downloads/${item.id}`} className="flex min-w-0 flex-1 gap-3" aria-label={`Подробности: ${item.artist} — ${item.title}`}>
        <div className="relative shrink-0">
          <Cover src={cover} size={48} alt="" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[15px]">{item.title}</div>
          <div className="truncate text-[13px] text-muted">{[item.artist, item.album].filter(Boolean).join(' · ')}</div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-dim">
            <StatusChip status={item.status} progress={item.progress} />
            {item.liked && item.status !== 'done' && <span className="text-accent">♥ после загрузки</span>}
            {item.provider && <span>{providerLabel(item.provider)}</span>}
            <span>{fmtWhen(item.updated_at)}</span>
          </div>
          {item.status === 'downloading' && <ProgressBar value={item.progress} className="mt-2" />}
          {warnings.map((w, i) => (
            <div key={i} className="mt-1 flex items-center gap-1 text-xs text-warn">
              <Icon name="alert" size={13} />
              {w}
            </div>
          ))}
          {problem && item.error && <div className="mt-1 line-clamp-2 text-xs text-danger">{item.error}</div>}
          {item.status === 'needs_review' && <div className="mt-1 text-xs font-medium text-warn">Нажмите, чтобы выбрать файл вручную</div>}
        </div>
      </Link>
      <div className="flex shrink-0 flex-col items-center">
        {active ? (
          <IconButton icon="x" label="Отменить" busy={busy} onClick={onCancel} className="text-muted" />
        ) : problem || item.status === 'cancelled' ? (
          <IconButton icon="refresh" label="Повторить" busy={busy} onClick={onRetry} className="text-muted" />
        ) : onPlay ? (
          <IconButton icon="play" label={`Слушать «${item.title}»`} onClick={onPlay} className="!text-accent" />
        ) : item.status === 'done' ? (
          <span className="flex h-11 w-11 items-center justify-center text-accent" aria-label="Готово">
            <Icon name="check" size={20} stroke={3} />
          </span>
        ) : null}
      </div>
    </li>
  );
}
