import { useState } from 'react';
import { fmtDuration } from '../lib/format';
import { useListen } from '../lib/listen';
import { attachPlayback, claimPlayback, controlPlayback, detachPlayback, handoffPlayback, unlockPlayback, useSharedPlayback } from '../lib/sharedPlayback';

/** Kept outside the local-track branch: an empty phone is still a remote control. */
export function PlaybackDevices() {
  const { view, busy, connected, error, enabled, localOnly } = useSharedPlayback();
  const localTrack = useListen((s) => s.track);
  const [seekValue, setSeekValue] = useState<number | null>(null);
  const [volumeValue, setVolumeValue] = useState<number | null>(null);
  if (!enabled) return null;
  const session = view?.state;
  const current = session?.queue[session.index];
  const owner = view?.devices.find((device) => device.id === view.owner);
  const transfer = view?.handoff;
  const transferring = !!transfer && ['preparing', 'stopping', 'starting'].includes(transfer.phase);
  const recoveryFenced = !!transfer?.recovery && transfer.source === view?.owner;
  const unavailable = !connected || busy || transferring || recoveryFenced || !owner?.online || !session;
  const duration = current?.duration_s ?? 0;
  const commitSeek = () => {
    if (seekValue !== null) void controlPlayback('seek', seekValue);
    setSeekValue(null);
  };
  const commitVolume = () => {
    if (volumeValue !== null) void controlPlayback('volume', volumeValue / 100);
    setVolumeValue(null);
  };
  return (
    <section aria-label="Устройства и общий сеанс" className="mx-4 my-4 space-y-3 rounded-xl border border-white/10 p-4">
      <h2 className="font-semibold">Устройства и общий сеанс</h2>
      <p className="text-sm text-muted">{localOnly ? 'Локальный режим: только этот браузер, без общего сеанса' : connected ? 'Общий режим: связь с сервером установлена' : 'Общий режим: нет связи с сервером'}{!localOnly && ` · ${owner ? `Владелец: ${owner.name}${owner.id === view?.device_id ? ' (здесь)' : ''}` : 'Общий сеанс не выбран'}`}</p>
      {localOnly
        ? <button className="rounded-lg bg-white/10 px-3 py-2 text-sm" onClick={attachPlayback}>Подключиться к общему сеансу</button>
        : <button className="rounded-lg bg-white/10 px-3 py-2 text-sm" onClick={detachPlayback}>Слушать только здесь / Отключиться от общей сессии</button>}
      {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
      {transfer && <p role="status" className="text-sm text-muted">
        {({ preparing: 'Подготовка устройства…', stopping: 'Остановка источника…', starting: 'Запуск на устройстве…', done: 'Передача завершена', failed: 'Передача не выполнена' } as Record<string, string>)[transfer.phase] ?? transfer.phase}
        {transfer.error ? `: ${transfer.error}` : ''}
      </p>}
      {transfer?.recovery && <p className="text-sm text-muted">Восстановление после отключения источника. Старое устройство остановлено до явного подтверждения продолжения или новой передачи.</p>}
      {current && session && <div className="space-y-2">
        <div className="font-medium">{current.artist} — {current.title}</div>
        <p className="text-xs text-muted">{session.playing ? 'Играет' : 'Пауза'} · {session.index + 1} / {session.queue.length} · Повтор: {session.repeat === 'one' ? 'трек' : session.repeat === 'all' ? 'очередь' : 'выключен'} · Перемешивание: {session.shuffle ? 'включено' : 'выключено'}</p>
        <label className="block text-sm">
          Позиция устройства: {fmtDuration(session.position)} / {fmtDuration(duration)}
          <input aria-label="Позиция на устройстве" type="range" min={0} max={Math.max(duration, session.position, 1)} step={0.1}
            value={seekValue ?? session.position} disabled={unavailable}
            onChange={(event) => setSeekValue(Number(event.target.value))} onPointerUp={commitSeek} onKeyUp={commitSeek}
            className="block w-full accent-accent" />
        </label>
        <div className="flex flex-wrap gap-2">
          <button className="rounded-lg bg-white/10 px-3 py-2 disabled:opacity-40" disabled={unavailable} onClick={() => void controlPlayback('prev')}>Предыдущий</button>
          <button className="rounded-lg bg-white/10 px-3 py-2 disabled:opacity-40" disabled={unavailable} onClick={() => void controlPlayback('play')}>Играть</button>
          <button className="rounded-lg bg-white/10 px-3 py-2 disabled:opacity-40" disabled={unavailable} onClick={() => void controlPlayback('pause')}>Пауза</button>
          <button className="rounded-lg bg-white/10 px-3 py-2 disabled:opacity-40" disabled={unavailable} onClick={() => void controlPlayback('next')}>Следующий</button>
        </div>
        <label className="block text-sm">Громкость: {Math.round(volumeValue ?? session.volume * 100)}%
          <input aria-label="Громкость на устройстве" type="range" min={0} max={100} step={1}
            value={volumeValue ?? session.volume * 100} disabled={unavailable}
            onChange={(event) => setVolumeValue(Number(event.target.value))} onPointerUp={commitVolume} onKeyUp={commitVolume}
            className="block w-full accent-accent" />
        </label>
        <details className="text-sm"><summary>Очередь общего сеанса</summary>
          <ol className="max-h-48 overflow-y-auto py-2">
            {session.queue.map((track, index) => <li key={index} aria-current={index === session.index ? 'true' : undefined} className={index === session.index ? 'font-semibold text-accent' : 'text-muted'}>
              {index + 1}. {track.artist} — {track.title}
            </li>)}
          </ol>
        </details>
      </div>}
      <ul className="space-y-2 text-sm">
        {view?.devices.map((device) => <li key={device.id} className="flex flex-wrap items-center justify-between gap-2">
          <span>{device.name} ({device.kind === 'desktop' ? 'компьютер' : 'браузер'}){device.id === view.device_id ? ' · здесь' : ''} — {device.online && connected ? 'в сети' : `не в сети · ${new Date(device.last_seen * 1000).toLocaleTimeString('ru-RU')}`}{view.owner === device.id ? ' · владелец' : ''}</span>
          {view.owner && view.owner !== device.id && <button className="rounded-lg bg-white/10 px-3 py-2 disabled:opacity-40"
            disabled={!device.online || !connected || busy || transferring || !session} onClick={() => void handoffPlayback(device.id)}>
            {device.id === view.device_id ? 'Слушать здесь' : 'Передать сюда'}
          </button>}
        </li>)}
      </ul>
      <div className="flex flex-wrap gap-2 text-sm">
        {!localOnly && (!view?.owner || (view.owner === view.device_id && recoveryFenced)) && <button className="rounded-lg bg-white/10 px-3 py-2 disabled:opacity-40" disabled={!connected || busy || transferring || !localTrack} onClick={() => void claimPlayback()}>{recoveryFenced ? 'Подтвердить общий сеанс здесь' : 'Сделать этот браузер владельцем'}</button>}
        <button className="rounded-lg bg-white/10 px-3 py-2 disabled:opacity-40" disabled={busy || transferring} onClick={() => void unlockPlayback()}>Разрешить звук</button>
      </div>
      <p className="text-xs text-muted">Телефон управляет владельцем сеанса. Для переноса звука нажмите «Слушать здесь» или «Передать сюда». Если браузер запретил звук, разрешите его и повторите передачу. После потери связи воспроизведение не возобновляется автоматически.</p>
    </section>
  );
}
