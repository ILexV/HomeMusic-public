import { request } from '../api/client';
import { Button, ErrorState, Notice, PageHeader, SkeletonRows } from '../components/ui';
import { fmtSize, fmtStamp } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { useSession } from '../lib/session';

type BackupState = { status?: string; last_attempt?: string; last_success?: string; error?: string | null };
interface SystemHealth {
  ok: boolean;
  status: string;
  components: Record<string, string>;
  queue: { status: string; counts?: Record<string, number>; oldest_age_seconds?: Record<string, number | null> };
  disk: Record<string, { status: string; free_bytes?: number; total_bytes?: number }>;
  backups: { external_enabled: boolean; local?: BackupState; external?: BackupState };
  background: { status: string; expected?: number; tasks: Record<string, string> };
}
const labels: Record<string, string> = {
  ok: 'Работает', unavailable: 'Недоступно', degraded: 'Частично недоступно', disabled: 'Выключено',
  loading: 'Загрузка', running: 'Выполняется', stopped: 'Остановлено', cancelled: 'Отменено', error: 'Ошибка', low: 'Мало места',
  database: 'База Hub', navidrome: 'Navidrome', workers: 'Фоновые задачи', embedder: 'Звуковая модель',
  queued: 'В очереди', done: 'Завершено', failed: 'Ошибки', data: 'Данные Hub', music: 'Музыка',
};

export function SystemHealthPage() {
  const { me } = useSession();
  const res = useAsync((signal) => request<SystemHealth>('GET', '/api/system/health', { signal }), [me.id], me.is_admin);
  if (!me.is_admin) return <ErrorState text="Только для администратора" />;
  const d = res.data;
  return <div className="pb-6">
    <PageHeader title="Состояние сервиса" back="/admin" right={<Button small busy={res.loading} onClick={() => res.reload(true)}>Обновить</Button>} />
    {res.error && <ErrorState text={res.error} onRetry={() => res.reload()} />}
    {!d && res.loading ? <SkeletonRows /> : d && <div className="space-y-5 px-4">
      <Notice kind={d.ok && d.status === 'ok' ? 'info' : 'warn'}>{labels[d.status] ?? d.status}. Необязательная звуковая модель не блокирует библиотеку.</Notice>
      <section aria-label="Компоненты">
        <h2 className="mb-2 font-semibold">Компоненты</h2>
        <dl className="space-y-2">{Object.entries(d.components).map(([key, value]) => <div key={key} className="flex justify-between gap-3 rounded-lg bg-surface p-3"><dt>{labels[key] ?? key}</dt><dd>{labels[value] ?? value}</dd></div>)}</dl>
      </section>
      <section aria-label="Очередь задач">
        <h2 className="mb-2 font-semibold">Очередь задач</h2>
        {d.queue.status !== 'ok' ? <p>Очередь недоступна</p> : <dl className="space-y-2">{Object.entries(d.queue.counts ?? {}).map(([key, count]) => <div key={key} className="rounded-lg bg-surface p-3"><dt>{labels[key] ?? key}: {count}</dt>{d.queue.oldest_age_seconds?.[key] != null && <dd className="text-sm text-muted">Самая старая: {Math.floor(d.queue.oldest_age_seconds[key]! / 60)} мин</dd>}</div>)}</dl>}
        {d.queue.status === 'ok' && Object.keys(d.queue.counts ?? {}).length === 0 && <p className="text-sm text-muted">Задач пока нет</p>}
        {Object.keys(d.background.tasks).length > 0 && <details className="mt-3 text-sm"><summary>Фоновые процессы</summary><ul className="space-y-1 pt-2">{Object.entries(d.background.tasks).map(([name, state]) => <li key={name}>{name}: {labels[state] ?? state}</li>)}</ul></details>}
      </section>
      <section aria-label="Свободное место">
        <h2 className="mb-2 font-semibold">Свободное место</h2>
        <dl className="space-y-2">{Object.entries(d.disk).map(([key, disk]) => <div key={key} className="rounded-lg bg-surface p-3"><dt>{labels[key] ?? key}</dt><dd>{disk.free_bytes == null ? 'Нет данных' : `${fmtSize(disk.free_bytes)} из ${fmtSize(disk.total_bytes ?? 0)}`} · {labels[disk.status] ?? disk.status}</dd></div>)}</dl>
      </section>
      <section aria-label="Резервные копии">
        <h2 className="mb-2 font-semibold">Резервные копии</h2>
        {(['local', 'external'] as const).map((kind) => {
          const backup = d.backups[kind];
          const disabled = kind === 'external' && !d.backups.external_enabled;
          return <div key={kind} className="mb-2 rounded-lg bg-surface p-3">
            <h3 className="font-medium">{kind === 'local' ? 'Локальная база Hub' : 'Внешняя зашифрованная копия'}</h3>
            <p>{disabled ? 'Не настроена' : backup?.status ? labels[backup.status] ?? backup.status : 'Ещё не выполнялась'}</p>
            <p className="text-sm text-muted">Последняя успешная: {fmtStamp(backup?.last_success)}</p>
            {backup?.error && <p role="alert" className="text-sm text-danger">{backup.error}</p>}
          </div>;
        })}
        <p className="text-sm text-muted">Локальная копия не защищает от потери диска. Наличие бэкапа не заменяет проверку восстановления; музыка включается во внешний бэкап отдельно.</p>
      </section>
    </div>}
  </div>;
}
