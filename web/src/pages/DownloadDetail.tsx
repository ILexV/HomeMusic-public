import { useState, type FormEvent } from 'react';
import { api, withQuery } from '../api/client';
import type { DownloadItemOut } from '../api/types';
import { CandidateRow } from '../components/candidate';
import { Icon } from '../components/Icon';
import { Button, ErrorState, Field, Loading, Notice, PageHeader, ProgressBar, SectionTitle, StatusChip } from '../components/ui';
import { useHubEvent } from '../lib/events';
import { DOWNLOAD_ACTIVE, fmtWhen, providerLabel, warningText } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { canPlay, playSongs } from '../lib/player';
import { Link } from '../lib/router';
import { attempt } from '../lib/ui';

export function DownloadDetailPage({ id }: { id: number }) {
  const res = useAsync((s) => api.downloadItem(id, s), [id]);
  const [busy, setBusy] = useState<string | null>(null);
  const [url, setUrl] = useState('');

  useHubEvent('download', (d) => {
    if (d.id !== id) return;
    // Status changed → candidates may have changed too; refetch the full detail.
    res.setData((prev) => (prev ? { ...prev, ...d, candidates: prev.candidates } : prev));
    if (d.status === 'needs_review' || d.status === 'failed' || d.status === 'done') res.reload(true);
  });
  useHubEvent('reconnect', () => res.reload(true));

  if (res.loading)
    return (
      <>
        <PageHeader title="Загрузка" back="/downloads" />
        <Loading />
      </>
    );
  if (res.error || !res.data)
    return (
      <>
        <PageHeader title="Загрузка" back="/downloads" />
        <ErrorState text={res.error ?? 'Не найдено'} onRetry={() => res.reload()} />
      </>
    );

  const item = res.data;
  const active = DOWNLOAD_ACTIVE.includes(item.status);
  const apply = (r: DownloadItemOut | undefined) => r && res.setData((prev) => ({ ...prev, ...r, candidates: r.candidates ?? prev?.candidates }));

  const run = async (key: string, fn: () => Promise<DownloadItemOut>) => {
    setBusy(key);
    apply(await attempt(fn));
    setBusy(null);
  };

  const submitUrl = (e: FormEvent) => {
    e.preventDefault();
    const u = url.trim();
    if (!u) return;
    void run('url', () => api.retry(item.id, { url: u })).then(() => setUrl(''));
  };

  const candidates = item.candidates ?? [];
  const chosenKey = item.chosen ? `${item.chosen.provider}:${item.chosen.provider_id}` : null;

  return (
    <div>
      <PageHeader title={item.title} sub={item.artist} back="/downloads" />
      <div className="space-y-3 px-4 pb-2">
        <div className="card space-y-2 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <StatusChip status={item.status} progress={item.progress} />
            {item.provider && <span className="text-sm text-muted">{providerLabel(item.provider)}</span>}
            <span className="text-sm text-dim">{fmtWhen(item.updated_at)}</span>
          </div>
          {item.status === 'downloading' && <ProgressBar value={item.progress} />}
          <div className="text-sm text-muted">
            {[item.artist, item.album].filter(Boolean).join(' · ')}
          </div>
          {item.error && <Notice kind="error">{item.error}</Notice>}
          {(item.warnings ?? []).map((w, i) => (
            <Notice key={i}>{warningText(w)}</Notice>
          ))}
          {item.status === 'needs_review' && (
            <Notice kind="info">Подходящий файл не найден автоматически. Выберите вариант ниже или вставьте ссылку на страницу трека.</Notice>
          )}
          {item.liked && item.status !== 'done' && (
            <Notice kind="info">Загрузка по ♥: песня попадёт в «Понравившиеся», когда файл скачается.</Notice>
          )}
          <div className="flex flex-wrap gap-2 pt-1">
            {active ? (
              <Button small variant="danger" icon="x" busy={busy === 'cancel'} onClick={() => run('cancel', () => api.cancel(item.id))}>
                Отменить
              </Button>
            ) : (
              item.status !== 'done' && (
                <Button small icon="refresh" busy={busy === 'retry'} onClick={() => run('retry', () => api.retry(item.id))}>
                  Повторить поиск
                </Button>
              )
            )}
            {item.status === 'done' && item.nd_song_id && canPlay() && (
              <Button small variant="primary" icon="play" onClick={() => playSongs([item.nd_song_id as string], 0)}>
                Слушать
              </Button>
            )}
            {item.status === 'done' && item.track_id && (
              <Link to={withQuery('/search', { q: `${item.artist} ${item.title}` })} className="inline-flex min-h-9 items-center gap-1 rounded-full bg-raised px-3.5 text-sm">
                <Icon name="check" size={16} /> В библиотеке
              </Link>
            )}
          </div>
        </div>

        {item.chosen && (
          <div className="card overflow-hidden">
            <div className="px-4 pt-3 text-xs font-medium uppercase tracking-wide text-muted">Выбранный файл</div>
            <CandidateRow c={item.chosen} />
          </div>
        )}

        {item.status !== 'done' && (
          <form onSubmit={submitUrl} className="card space-y-2 p-4">
            <Field label="Своя ссылка">
              <input
                className="input"
                type="url"
                inputMode="url"
                placeholder="hitmoz, hitmos.fm, YouTube или SoundCloud"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
            </Field>
            <Button type="submit" small variant="primary" icon="link" busy={busy === 'url'} disabled={!url.trim()}>
              Скачать по ссылке
            </Button>
          </form>
        )}
      </div>

      {candidates.length > 0 && (
        <section aria-label="Кандидаты">
          <SectionTitle>Найденные файлы</SectionTitle>
          <ul className="divide-y divide-line/60">
            {candidates.map((c, i) => {
              const key = `${c.provider}:${c.provider_id}`;
              const isChosen = key === chosenKey;
              return (
                <li key={key + i}>
                  <CandidateRow
                    c={c}
                    action={
                      isChosen ? (
                        <span className="text-sm text-accent">Выбран</span>
                      ) : (
                        <Button small icon="download" busy={busy === `c${i}`} disabled={busy !== null} onClick={() => run(`c${i}`, () => api.retry(item.id, { candidate_index: i }))}>
                          Скачать этот
                        </Button>
                      )
                    }
                  />
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}
