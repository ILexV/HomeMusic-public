import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api/client';
import { recommendationsApi, type Release, type ReleasePeriod } from '../api/recommendations';
import { Button, Cover, ErrorState, Loading, Notice, PageHeader, Segmented } from '../components/ui';
import { useAsync } from '../lib/hooks';
import { Link, enc } from '../lib/router';
import { useSession } from '../lib/session';

export function ReleasesPage() {
  const [period, setPeriod] = useState<ReleasePeriod>('week');
  const [unseen, setUnseen] = useState(false);
  return <div>
    <PageHeader title="Новые релизы" />
    <div className="space-y-4 px-4 pb-6">
      <p className="text-sm text-muted">Новое у исполнителей из вашего профиля вкуса. Без автоматических лайков и скачиваний.</p>
      <Segmented label="Период релизов" value={period} onChange={setPeriod} options={[{ value: 'week', label: 'За 7 дней' }, { value: 'month', label: 'За 30 дней' }]} />
      <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" checked={unseen} onChange={(e) => setUnseen(e.target.checked)} className="h-5 w-5 accent-accent" />Только непрочитанные</label>
      <ReleaseList key={`${period}:${unseen}`} period={period} unseen={unseen} />
    </div>
  </div>;
}

function ReleaseList({ period, unseen }: { period: ReleasePeriod; unseen: boolean }) {
  const { me } = useSession();
  const res = useAsync((signal) => recommendationsApi.releases(period, unseen, signal), [me.id, period, unseen]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), [me.id]);
  const mark = async (release: Release) => {
    if (active.current) return;
    const ctl = new AbortController(); active.current = ctl;
    setBusy(release.ref); setError(null);
    try {
      const result = await recommendationsApi.seen(release.ref, !release.seen, ctl.signal);
      if (ctl.signal.aborted) return;
      res.setData((data) => data && ({ ...data, items: data.items
        .map((item) => item.ref === result.ref ? { ...item, seen: result.seen } : item)
        .filter((item) => !unseen || !item.seen) }));
    } catch (e) { if (!ctl.signal.aborted) setError(errorMessage(e)); }
    finally { if (!ctl.signal.aborted) { active.current = null; setBusy(null); } }
  };
  if (res.loading) return <Loading text="Ищем релизы…" />;
  if (res.error) return <ErrorState text={res.error} onRetry={() => res.reload()} />;
  if (!res.data) return null;
  const data = res.data;
  return <>
    {error && <Notice kind="error">{error}</Notice>}
    {data.partial && <Notice>Не все источники ответили. Список может быть неполным.</Notice>}
    {data.errors.map((message, i) => <Notice key={i}>{message}</Notice>)}
    {(data.partial || data.errors.length > 0) && <Button small icon="refresh" onClick={() => res.reload()}>Обновить</Button>}
    {!data.items.length && <p className="py-8 text-center text-muted">{unseen ? 'Непрочитанных релизов за этот период нет.' : 'Релизов за этот период пока нет.'}</p>}
    <ul className="space-y-3">{data.items.map((release) => <li key={release.ref} className="rounded-xl bg-surface p-3">
      <Link to={`/album/${enc(release.ref)}`} className="flex min-w-0 items-start gap-3">
        <Cover src={release.cover_url} alt="" size={72} />
        <div className="min-w-0 flex-1"><h2 className="break-words font-semibold">{release.title}</h2><p className="break-words text-sm text-muted">{release.artist}</p>
          <p className="mt-1 text-xs text-muted">{release.release_date}{release.track_count != null ? ` · треков: ${release.track_count}` : ''}</p>
          {!release.seen && <span className="mt-1 inline-block text-xs font-semibold text-accent">Новый</span>}
        </div>
      </Link>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Link to={`/album/${enc(release.ref)}`} className="py-2 text-sm text-accent hover:underline">Открыть и слушать</Link>
        <Button small disabled={busy !== null} busy={busy === release.ref} onClick={() => void mark(release)}>{release.seen ? 'В непрочитанные' : 'Отметить прочитанным'}</Button>
      </div>
    </li>)}</ul>
    <p className="text-xs text-muted">В альбоме доступны воспроизведение, очередь и скачивание. Проверено: {new Date(data.generated_at).toLocaleString()}.</p>
  </>;
}
