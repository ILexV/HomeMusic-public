import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api/client';
import { recommendationsApi, type PauseAction } from '../api/recommendations';
import { useAsync } from '../lib/hooks';
import { interruptRecording, retryRecording } from '../lib/listen';
import { Link } from '../lib/router';
import { useSession } from '../lib/session';
import { refreshWaveRecommendations } from '../lib/wave';
import { Button, ErrorState, Loading, Notice } from './ui';

export const PAUSE_LABELS: Record<PauseAction, string> = {
  today: 'Не предлагать до конца дня',
  week: 'Пауза трека на 7 дней',
  less_artist: 'Меньше исполнителя на 30 дней',
};

/** Fetch only recorded reasons, and only when expanded; never guess from today's taste. */
export function RecommendationExplanation({ context, refId, songId, reasons }: {
  context: string; refId: string; songId?: string | null; reasons?: string[];
}) {
  const { me } = useSession();
  const [open, setOpen] = useState(false);
  const res = useAsync((signal) => recommendationsApi.explain(context, refId, songId, signal),
    [me.id, context, refId, songId], open && !reasons?.length);
  const shown = reasons?.length ? reasons : res.data?.reasons;
  return <details className="text-sm" onToggle={(e) => setOpen(e.currentTarget.open)}>
    <summary className="cursor-pointer py-2 text-muted">Почему этот трек?</summary>
    {open && (res.loading && !reasons?.length ? <Loading text="Ищем сохранённое объяснение…" />
      : res.error && !reasons?.length ? <ErrorState text={res.error} onRetry={() => res.reload()} />
      : shown?.length ? <ul className="list-disc space-y-1 pb-2 pl-5 text-muted">{shown.map((r, i) => <li key={i}>{r}</li>)}</ul>
      : <p className="pb-2 text-muted">Для этого решения объяснение не сохранено.</p>)}
  </details>;
}

export function TemporaryTrackActions({ refId, songId }: { refId: string; songId?: string | null }) {
  const { me } = useSession();
  const [busy, setBusy] = useState<PauseAction | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    request.current = null;
    setBusy(null); setMessage(null); setError(null);
    return () => request.current?.abort();
  }, [me.id, refId, songId]);
  const pause = async (action: PauseAction) => {
    if (request.current) return;
    const ctl = new AbortController();
    request.current = ctl;
    setBusy(action); setError(null); setMessage(null);
    try {
      const saved = await recommendationsApi.pause(action, { ref: refId, nd_song_id: songId }, ctl.signal);
      if (ctl.signal.aborted) return;
      setMessage(`${saved.label} · до ${new Date(saved.expires_at).toLocaleString()}`);
      refreshWaveRecommendations();
    } catch (e) { if (!ctl.signal.aborted) setError(errorMessage(e)); }
    finally { if (!ctl.signal.aborted) { request.current = null; setBusy(null); } }
  };
  return <details className="text-sm">
    <summary className="cursor-pointer py-2 text-muted">Временно не рекомендовать</summary>
    <p className="mb-2 text-xs text-muted">Это не дизлайк. Песня продолжит играть; вручную её можно слушать всегда. До конца дня — по часовому поясу сервера.</p>
    <div className="flex flex-wrap gap-2">{(Object.keys(PAUSE_LABELS) as PauseAction[]).map((action) =>
      <Button key={action} small busy={busy === action} disabled={!!busy} onClick={() => void pause(action)}>{PAUSE_LABELS[action]}</Button>)}</div>
    {message && <div className="mt-2"><Notice kind="info">{message}</Notice></div>}
    {error && <div className="mt-2"><Notice kind="error">{error}</Notice></div>}
    <Link to="/recommendations/pauses" className="mt-2 inline-block py-2 text-accent hover:underline">Все паузы и отмена</Link>
  </details>;
}

export function RecordingFeedback({ refId, binding }: { refId: string; binding?: string | null }) {
  const { me } = useSession();
  const [saved, setSaved] = useState<{ id: number; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => {
    setSaved(null); setError(null); setBusy(false);
    return () => { active.current?.abort(); active.current = null; };
  }, [me.id, refId]);
  const submit = async (undo = false) => {
    if (active.current || (!undo && !binding)) return;
    const ctl = new AbortController(); active.current = ctl;
    setBusy(true); setError(null);
    try {
      if (undo && saved) {
        await recommendationsApi.undoRecording(saved.id, ctl.signal);
        if (!ctl.signal.aborted) setSaved(null);
      } else if (binding) {
        const actual = await recommendationsApi.currentRecording(refId, ctl.signal);
        const result = await recommendationsApi.wrongRecording(refId, actual.binding, ctl.signal);
        if (!ctl.signal.aborted) {
          setSaved(result);
          interruptRecording(refId, binding);
        }
      }
    } catch (e) { if (!ctl.signal.aborted) setError(errorMessage(e)); }
    finally { if (!ctl.signal.aborted) { active.current = null; setBusy(false); } }
  };
  if (!binding && !saved) return null;
  return <details className="text-sm">
    <summary className="cursor-pointer py-2 text-muted">Не та версия записи</summary>
    <p className="mb-2 text-xs text-muted">Ремикс вместо оригинала, live или другой файл? Это сообщение о записи, не дизлайк песни. Неверный файл будет исключён только для вас.</p>
    {!saved ? <Button small busy={busy} disabled={busy || !binding} onClick={() => void submit()}>Подтвердить неверную запись</Button>
      : <div className="space-y-2"><Notice kind="info">{saved.message}</Notice>
        <div className="flex flex-wrap gap-2">
          <Button small onClick={() => retryRecording(refId)}>Найти другую запись</Button>
          <Button small busy={busy} disabled={busy} onClick={() => void submit(true)}>Отменить сообщение</Button>
        </div>
      </div>}
    {error && <Notice kind="error">{error}</Notice>}
  </details>;
}
