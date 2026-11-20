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
const credit = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });

export function RecommendationQualityPage() {
  const { me } = useSession();
  const [days, setDays] = useState<'7' | '30' | '90'>('30');
  const res = useAsync((signal) => recommendationsApi.quality(Number(days) as 7 | 30 | 90, signal), [me.id, days]);
  const d = res.data;
  return <div>
    <PageHeader title="Качество рекомендаций" />
    <div className="space-y-4 px-4 pb-6">
      <Segmented label="Период качества" value={days} onChange={setDays} options={[{ value: '7', label: '7 дней' }, { value: '30', label: '30 дней' }, { value: '90', label: '90 дней' }]} />
      <p className="text-sm text-muted">Данные только вашего аккаунта. Повторы в подборках считаем по предложенным трекам, а возвраты к песням — по прослушиваниям и лайкам.</p>
      <div className="flex flex-wrap gap-4 text-sm text-accent"><Link to="/recommendations/pauses">Временные паузы</Link><Link to="/releases">Новые релизы</Link></div>
      {res.loading ? <Loading /> : res.error ? <ErrorState text={res.error} onRetry={() => res.reload()} /> : d && <>
        {!d.attempts && <Notice kind="info">Пока нет прослушиваний рекомендаций за этот период. Включите волну или микс — статистика появится после прослушивания.</Notice>}
        {d.offer_repeats && <section className="space-y-1 rounded-xl bg-surface p-4">
          <h2 className="text-lg font-bold">Повторы в подборках</h2>
          <p className="text-lg font-semibold tabular-nums">{d.offer_repeats.per_50 === null ? 'Нет выдач за период' : `${credit.format(d.offer_repeats.per_50)} на 50 рекомендаций`}</p>
          {d.offer_repeats.offers > 0 && <p className="text-sm">Из {d.offer_repeats.offers} выдач в {d.offer_repeats.repeats} случаях трек уже предлагался за предыдущие {d.offer_repeats.lookback_days} дней. Это выдачи, а не прослушивания.</p>}
          {d.offer_repeats.limitations.map((note) => <p key={note} className="text-xs text-muted">{note}</p>)}
        </section>}
        {d.discovery_adoption && <section className="space-y-2 rounded-xl bg-surface p-4">
          <h2 className="text-lg font-bold">Какие открытия прижились за {d.discovery_adoption.window_days} дней</h2>
          <p className="text-sm">Считаем трек, если после открытия вы поставили лайк или сами включили его хотя бы дважды.</p>
          {d.discovery_adoption.mature > 0
            ? <p className="text-lg font-semibold tabular-nums">{rate(d.discovery_adoption.rate)} · {d.discovery_adoption.adopted} из {d.discovery_adoption.mature}</p>
            : <p className="text-sm text-muted">Пока нет треков с полным сроком наблюдения.</p>}
          <p className="text-xs text-muted">Период открытий ({days} дн.) заканчивается {d.discovery_adoption.window_days} дней назад. Более свежих треков: {d.discovery_adoption.pending} — они ещё не входят в процент.</p>
          {d.discovery_adoption.subtypes && <>
            <h3 className="pt-2 font-semibold">К каким открытиям вы возвращаетесь</h3>
            <ul className="divide-y divide-border text-sm">{d.discovery_adoption.subtypes.map((s) =>
              <li key={s.id} className="flex items-baseline justify-between gap-3 py-1"><span>{s.title}</span>
                <span className="shrink-0 tabular-nums">{s.mature ? `${rate(s.rate)} · ${s.adopted} из ${s.mature}` : 'Пока нет данных'}</span></li>
            )}</ul>
          </>}
          {!!d.discovery_adoption.unknown_return_tracks && <p className="text-xs text-muted">У {d.discovery_adoption.unknown_return_tracks} треков были возвраты без записи о том, как их включили. Такие запуски не считаются ручным выбором.</p>}
          {d.discovery_balance && <div className="border-l-2 border-accent pl-3 text-sm">
            <h3 className="font-semibold">Баланс режима «Незнакомое»</h3>
            <p>{d.discovery_balance.reason}</p>
            {d.discovery_balance.known_artist_share !== null && <p>Ориентир для новых песен знакомых исполнителей: {rate(d.discovery_balance.known_artist_share)} среди открытий.</p>}
          </div>}
          {d.discovery_adoption.sources.length > 0 && <h3 className="pt-2 font-semibold">По источникам</h3>}
          {d.discovery_adoption.sources.length > 0 && <ul className="divide-y divide-border text-sm">
            {d.discovery_adoption.sources.map((s) => <li className="flex justify-between gap-2 py-1" key={s.id}>
              <span>{s.title}</span><span className="tabular-nums">{rate(s.rate)} · вклад {credit.format(s.adopted)} из {credit.format(s.mature)}</span>
            </li>)}
          </ul>}
          {d.discovery_adoption.limitations?.map((note) => <p className="text-xs text-muted" key={note}>{note}</p>)}
        </section>}
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
        {d.discovery_adoption && <p className="text-sm text-muted">Если песню нашли несколько источников, они делят одно прослушивание пропорционально вкладу. Без сохранённых долей учитываем только записанного лидера. Множитель меняется при 20 эффективных примерах за 90 дней; прижившиеся открытия на него не влияют.</p>}
        {!d.sources.length && <p className="text-sm text-muted">Пока нет подтверждённых источников.</p>}
        <ul className="space-y-3">{d.sources.map((source) => <li key={source.id} className="rounded-xl bg-surface p-3">
          <div className="flex items-start justify-between gap-3"><h3 className="font-semibold">{source.title}</h3><span className="shrink-0 font-semibold tabular-nums text-accent">×{source.multiplier.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span></div>
          {source.effective_learning_samples == null ? <p className="text-sm">Примеров за 90 дней: {source.learning_samples}</p> : <>
            <p className="mt-1 text-xs text-muted">Эффективных примеров за 90 дней: {credit.format(source.effective_learning_samples)} · при учёте только лидера: ×{credit.format(source.winner_only_multiplier)}</p>
            <p className="mt-2 text-sm">Вклад за период: попыток {credit.format(source.effective_attempts)} · дослушиваний {credit.format(source.effective_completed)} · ранних пропусков {credit.format(source.effective_early_skips)}</p>
          </>}
          {source.fallback_attempts > 0 && <p className="text-xs text-muted">Попыток без сохранённых долей: {source.fallback_attempts}</p>}
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
