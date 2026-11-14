import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api/client';
import { recommendationsApi } from '../api/recommendations';
import { PAUSE_LABELS } from '../components/Recommendations';
import { Button, ErrorState, Loading, Notice, PageHeader, Segmented } from '../components/ui';
import { useAsync } from '../lib/hooks';
import { Link } from '../lib/router';
import { useSession } from '../lib/session';
import { refreshWaveRecommendations } from '../lib/wave';

const rate = (n: number | null) => n === null ? 'Недостаточно данных' : `${(n * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })} %`;

export function RecommendationQualityPage() {
  const { me } = useSession();
  const [days, setDays] = useState<'7' | '30' | '90'>('30');
  const res = useAsync((signal) => recommendationsApi.quality(Number(days) as 7 | 30 | 90, signal), [me.id, days]);
  const d = res.data;
  return <div>
    <PageHeader title="Качество рекомендаций" />
    <div className="space-y-4 px-4 pb-6">
      <Segmented label="Период качества" value={days} onChange={setDays} options={[{ value: '7', label: '7 дней' }, { value: '30', label: '30 дней' }, { value: '90', label: '90 дней' }]} />
      <p className="text-sm text-muted">Только ваши реальные прослушивания рекомендаций. Предложенные, но не включённые треки не обучают подбор.</p>
      <div className="flex flex-wrap gap-4 text-sm text-accent"><Link to="/recommendations/pauses">Временные паузы</Link><Link to="/releases">Новые релизы</Link></div>
      {res.loading ? <Loading /> : res.error ? <ErrorState text={res.error} onRetry={() => res.reload()} /> : d && <>
        {!d.attempts && <Notice kind="info">Пока нет прослушиваний рекомендаций за этот период. Включите волну или микс — статистика появится после прослушивания.</Notice>}
        <dl className="grid grid-cols-2 gap-3">
          {([
            ['Дослушивания', rate(d.completion_rate)], ['Ранние пропуски', rate(d.early_skip_rate)],
            ['Лайки нового', rate(d.new_track_like_rate)], ['Повторы', rate(d.repeat_rate)],
            ['Попытки воспроизведения', d.attempts], ['Начали звучать', d.starts],
            ['Учтено для качества', d.eligible_plays], ['Дослушано', d.completed],
            ['Рано пропущено', d.early_skips], ['Ошибки воспроизведения', d.playback_errors],
            ['Прервано', d.interrupted], ['Прослушивания нового', d.new_track_plays],
            ['Лайки нового, всего', d.new_track_likes], ['Повторы, всего', d.repeats],
          ] as Array<[string, number | string]>).map(([label, value]) => <div key={label} className="rounded-xl bg-surface p-3"><dt className="text-xs text-muted">{label}</dt><dd className="mt-1 break-words text-lg font-semibold tabular-nums">{value}</dd></div>)}
        </dl>
        <p className="text-xs text-muted">Ошибки, прерывания и незавершённые попытки не оценивают вкус. Доли дослушиваний, ранних пропусков и повторов — от завершённых или вручную пропущенных прослушиваний; лайки нового — от прослушиваний новых треков. Повторы — повторные прослушивания трека за выбранный период. Нет данных — нет процента.</p>
        <h2 className="text-lg font-bold">Полные доступные записи</h2>
        <p className="text-sm">{rate(d.recordings.rate)} · полных {d.recordings.full} / проверено {d.recordings.known} · неизвестно {d.recordings.unknown} / наблюдений {d.recordings.observations}</p>
        <p className="text-xs text-muted">{d.recordings.metric_version} · {d.recordings.provenance.join(', ') || 'Нет наблюдений'}</p>
        {d.recordings.limitations.map((text, i) => <p key={i} className="text-xs text-muted">{text}</p>)}
        <h2 className="text-lg font-bold">Сравнение с предыдущим периодом</h2>
        <p className="text-xs text-muted">{d.metric_version} · {d.provenance}</p>
        <div className="grid gap-3 sm:grid-cols-2">
          {([['Текущий', d.comparison.current], ['Предыдущий', d.comparison.previous]] as const).map(([label, window]) =>
            <section key={label} className="rounded-xl bg-surface p-3">
              <h3 className="font-semibold">{label}</h3>
              <p className="text-xs text-muted">{new Date(window.since).toLocaleDateString()} — {new Date(window.until).toLocaleDateString()}</p>
              <p className="mt-2 text-sm">N = {window.eligible_plays} · попыток {window.attempts}</p>
              <p className="text-sm">Дослушивания: {rate(window.completion_rate)} ({window.completed})</p>
              <p className="text-sm">Ранние пропуски: {rate(window.early_skip_rate)} ({window.early_skips})</p>
              <ul className="mt-2 space-y-1 text-xs text-muted">{window.policy_cohorts.map((cohort) =>
                <li key={cohort.policy_version}>Политика {cohort.policy_version === 'unknown' ? 'неизвестна' : cohort.policy_version}: N = {cohort.eligible_plays}; дослушивания {rate(cohort.completion_rate)}; ранние пропуски {rate(cohort.early_skip_rate)}</li>)}</ul>
            </section>)}
        </div>
        {d.comparison.limitations.map((text, i) => <Notice key={`comparison:${i}`} kind="info">{text}</Notice>)}
        <h2 className="text-lg font-bold">Ваши источники рекомендаций</h2>
        <p className="text-sm text-muted">Множитель меняет вклад источника осторожно, от ×0,75 до ×1,25. Мало подтверждённых примеров — нейтральный вклад. Обучение и число примеров всегда учитывают последние 90 дней, независимо от выбранного периода.</p>
        {!d.sources.length && <p className="text-sm text-muted">Пока нет подтверждённых источников.</p>}
        <ul className="space-y-3">{d.sources.map((source) => <li key={source.id} className="rounded-xl bg-surface p-3">
          <div className="flex items-start justify-between gap-3"><h3 className="font-semibold">{source.title}</h3><span className="shrink-0 font-semibold tabular-nums text-accent">×{source.multiplier.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span></div>
          <p className="mt-1 text-xs text-muted">Примеров обучения: {source.learning_samples}{source.learning_samples < 20 ? ' · пока мало данных' : ''}</p>
          <p className="mt-2 text-sm text-muted">Попытки: {source.attempts} · дослушано: {source.completed} · ранние пропуски: {source.early_skips} · ошибки: {source.playback_errors} · лайки нового: {source.new_track_likes}</p>
        </li>)}</ul>
        {d.limitations.map((text, i) => <Notice key={i} kind="info">{text}</Notice>)}
      </>}
    </div>
  </div>;
}

export function RecommendationPausesPage() {
  const { me } = useSession();
  const res = useAsync((signal) => recommendationsApi.pauses(signal), [me.id]);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), [me.id]);
  const remove = async (id: number) => {
    if (active.current) return;
    const ctl = new AbortController(); active.current = ctl;
    setBusy(id); setError(null);
    try {
      await recommendationsApi.unpause(id, ctl.signal);
      if (ctl.signal.aborted) return;
      res.setData((data) => data && ({ items: data.items.filter((p) => p.id !== id) }));
      refreshWaveRecommendations();
    } catch (e) { if (!ctl.signal.aborted) setError(errorMessage(e)); }
    finally { if (!ctl.signal.aborted) { active.current = null; setBusy(null); } }
  };
  return <div>
    <PageHeader title="Паузы рекомендаций" />
    <div className="space-y-4 px-4 pb-6">
      <p className="text-sm text-muted">Только для волны, миксов и умного перемешивания. Не меняет лайки и не запрещает слушать вручную. Добавить паузу можно в плеере у текущего трека.</p>
      {error && <Notice kind="error">{error}</Notice>}
      {res.loading ? <Loading /> : res.error ? <ErrorState text={res.error} onRetry={() => res.reload()} /> : !res.data?.items.length ? <p className="py-8 text-center text-muted">Активных пауз нет</p> :
        <ul className="space-y-3">{res.data.items.map((pause) => <li key={pause.id} className="rounded-xl bg-surface p-3">
          <h2 className="break-words font-semibold">{pause.label}</h2>
          <p className="mt-1 text-sm text-muted">{PAUSE_LABELS[pause.action]}</p>
          <p className="mt-1 text-xs text-muted">До {new Date(pause.expires_at).toLocaleString()}</p>
          <Button small className="mt-3" disabled={busy !== null} busy={busy === pause.id} onClick={() => void remove(pause.id)}>Отменить паузу</Button>
        </li>)}</ul>}
      <Link to="/recommendations" className="inline-block py-2 text-sm text-accent">Качество и источники</Link>
    </div>
  </div>;
}
