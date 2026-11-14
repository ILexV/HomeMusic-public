import { useState, type FormEvent, type ReactNode } from 'react';
import { api } from '../api/client';
import type { AdminUserOut, InviteOut } from '../api/types';
import { Icon } from '../components/Icon';
import { Button, Empty, ErrorState, Field, IconButton, PageHeader, SectionTitle, Segmented, Sheet, SkeletonRows, cx } from '../components/ui';
import { fmtDate, fmtUsd, fmtWhen, parseTime, plural } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { Link } from '../lib/router';
import { useSession } from '../lib/session';
import { EMBEDDED } from '../lib/embedded';
import { canShare, copyText, shareText } from '../lib/share';
import { attempt, confirmDialog, toast } from '../lib/ui';

const DAYS = [
  { value: '1', label: '1 день' },
  { value: '7', label: '7 дней' },
  { value: '30', label: '30 дней' },
];

export function AdminPage() {
  const users = useAsync((s) => api.adminUsers(s), []);
  const invites = useAsync((s) => api.invites(s), []);
  const [creating, setCreating] = useState(false);
  const [shown, setShown] = useState<InviteOut>();

  const revoke = async (inv: InviteOut) => {
    const ok = await confirmDialog({
      title: 'Отозвать приглашение?',
      body: `Код ${inv.code}${inv.note ? ` (${inv.note})` : ''} перестанет действовать.`,
      okLabel: 'Отозвать',
      danger: true,
    });
    if (!ok) return;
    if (await attempt(() => api.deleteInvite(inv.id), 'Приглашение отозвано')) {
      invites.setData((prev) => prev?.filter((i) => i.id !== inv.id));
    }
  };

  const active = invites.data?.filter((i) => i.status === 'active') ?? [];
  const past = invites.data?.filter((i) => i.status !== 'active') ?? [];
  const d = users.data;

  return (
    <div className="pb-6">
      {/* in the app the page is opened from its settings: the system back leaves it */}
      <PageHeader title="Пользователи" back={EMBEDDED ? undefined : '/more'} right={<IconButton icon="refresh" label="Обновить" onClick={() => (users.reload(true), invites.reload(true))} />} />

      <ul className="mx-4 mb-3 overflow-hidden rounded-xl bg-surface">
        <InsightLink to="/admin/insights" icon="history" title="Аналитика пользователей" sub="прослушивания, миксы, волна по пользователям" />
        <InsightLink to="/admin/taste" icon="heart" title="Веса песен у всех" sub="песни × пользователи по профилю вкуса" />
        <InsightLink to="/admin/system" icon="history" title="Состояние сервиса" sub="очередь, свободное место, резервные копии и фоновые задачи" />
      </ul>

      {d && (
        <div className="grid grid-cols-3 gap-2 px-4 pb-2">
          <Tile label="ИИ за 30 дней" value={fmtUsd(d.ai_cost_30d_usd)} />
          <Tile label="ИИ всего" value={fmtUsd(d.ai_cost_usd)} />
          <Tile label="Песен всего" value={String(d.library_tracks)} />
        </div>
      )}

      <SectionTitle
        right={
          <Button small variant="primary" icon="plus" onClick={() => setCreating(true)}>
            Пригласить
          </Button>
        }
      >
        Приглашения
      </SectionTitle>
      {invites.loading ? (
        <SkeletonRows n={2} />
      ) : invites.error ? (
        <ErrorState text={invites.error} onRetry={() => invites.reload()} />
      ) : !active.length && !past.length ? (
        <p className="px-4 pb-2 text-sm text-muted">
          Новый человек входит по коду приглашения: создайте код и отправьте его. Код одноразовый.
        </p>
      ) : (
        <ul className="mx-4 overflow-hidden rounded-xl bg-surface">
          {active.map((inv) => (
            <li key={inv.id} className="flex min-h-16 items-center gap-2 border-b border-line/60 py-2 pl-4 pr-1 last:border-0">
              <button type="button" className="min-w-0 flex-1 text-left" onClick={() => setShown(inv)}>
                <span className="block font-mono text-base font-semibold tracking-wider">{inv.code}</span>
                <span className="block truncate text-xs text-muted">
                  {[inv.note, `до ${fmtDate(inv.expires_at)}`].filter(Boolean).join(' · ')}
                </span>
              </button>
              <IconButton icon="send" label="Отправить код" onClick={() => setShown(inv)} />
              <IconButton icon="trash" label="Отозвать" onClick={() => revoke(inv)} />
            </li>
          ))}
          {past.map((inv) => (
            <li key={inv.id} className="flex min-h-14 items-center gap-3 border-b border-line/60 px-4 py-2 text-muted last:border-0">
              <span className="min-w-0 flex-1">
                <span className="block font-mono text-sm line-through">{inv.code}</span>
                <span className="block truncate text-xs">
                  {inv.status === 'used'
                    ? `${inv.note ? inv.note + ' · ' : ''}аккаунт ${inv.used_by ?? '—'} создан ${fmtWhen(inv.used_at)}`
                    : `${inv.note ? inv.note + ' · ' : ''}истёк ${fmtDate(inv.expires_at)}`}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}

      <SectionTitle>Пользователи</SectionTitle>
      {users.loading ? (
        <SkeletonRows n={3} />
      ) : users.error ? (
        <ErrorState text={users.error} onRetry={() => users.reload()} />
      ) : !d?.users.length ? (
        <Empty icon="user" title="Пользователей нет" />
      ) : (
        <ul className="space-y-2 px-4">
          {d.users.map((u) => (
            <li key={u.username}>
              <UserCard u={u} />
            </li>
          ))}
        </ul>
      )}

      <CreateInviteSheet
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(inv) => {
          setCreating(false);
          invites.setData((prev) => [inv, ...(prev ?? [])]);
          setShown(inv);
        }}
      />
      <InviteSheet invite={shown} onClose={() => setShown(undefined)} />
    </div>
  );
}

function InsightLink({ to, icon, title, sub }: { to: string; icon: 'history' | 'heart'; title: string; sub: string }) {
  return (
    <li className="border-b border-line/60 last:border-0">
      <Link to={to} className="flex min-h-14 items-center gap-3 px-4 py-2 hover:bg-fg/5">
        <Icon name={icon} size={20} className="shrink-0 text-accent" />
        <span className="min-w-0 flex-1">
          <span className="block font-semibold">{title}</span>
          <span className="block truncate text-xs text-muted">{sub}</span>
        </span>
        <Icon name="chevron" size={18} className="shrink-0 text-dim" />
      </Link>
    </li>
  );
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-xl bg-surface px-3 py-2.5">
      <div className="truncate text-lg font-bold">{value}</div>
      <div className="truncate text-[11px] text-muted">{label}</div>
    </div>
  );
}

/** The latest of the Hub's and the player's last activity. */
export function lastActivity(u: AdminUserOut): string | null {
  const times = [u.last_seen_at, u.player_seen_at].filter((t): t is string => !!t);
  if (!times.length) return null;
  return times.reduce((a, b) => (parseTime(a) > parseTime(b) ? a : b));
}

export function UserBadges({ u }: { u: AdminUserOut }) {
  const badges: ReactNode[] = [];
  if (u.is_admin) badges.push(<Badge key="a">Админ</Badge>);
  if (u.google_email) badges.push(<Badge key="g">Google</Badge>);
  if (u.email) badges.push(<Badge key="e">почта</Badge>);
  if (!u.in_hub) badges.push(<Badge key="p">только плеер</Badge>);
  return <>{badges}</>;
}

function Badge({ children }: { children: ReactNode }) {
  return <span className="shrink-0 rounded-full bg-raised px-2 py-0.5 text-[11px] font-semibold text-muted">{children}</span>;
}

function UserCard({ u }: { u: AdminUserOut }) {
  const seen = lastActivity(u);
  const body = (
    <>
      <div className="flex items-center gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent font-bold uppercase text-on-accent">{u.username.slice(0, 1)}</span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            <span className="truncate font-semibold">{u.username}</span>
            <UserBadges u={u} />
          </span>
          <span className="block truncate text-xs text-muted">
            {[u.name && u.name !== u.username ? u.name : null, seen ? `был(а) ${fmtWhen(seen)}` : 'ещё не заходил(а)'].filter(Boolean).join(' · ')}
          </span>
        </span>
        {u.id != null && <Icon name="chevron" size={18} className="shrink-0 text-dim" />}
      </div>
      {u.in_hub && (
        <div className="mt-3 grid grid-cols-3 gap-2 text-center">
          <Stat value={fmtUsd(u.ai_cost_usd)} label={`ИИ · ${u.ai_requests} ${plural(u.ai_requests, ['запрос', 'запроса', 'запросов'])}`} />
          <Stat value={String(u.downloads_done)} label={plural(u.downloads_done, ['песня скачана', 'песни скачано', 'песен скачано'])} />
          <Stat value={u.starred_songs == null ? '—' : String(u.starred_songs)} label="♥ понравилось" />
        </div>
      )}
    </>
  );
  const cls = 'block rounded-xl bg-surface p-3';
  return u.id != null ? (
    <Link to={`/admin/user/${u.id}`} className={cx(cls, 'hover:bg-fg/5')}>
      {body}
    </Link>
  ) : (
    <div className={cls}>{body}</div>
  );
}

export function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div className="min-w-0 rounded-lg bg-raised px-1 py-2">
      <div className="truncate font-semibold">{value}</div>
      <div className="truncate text-[11px] text-muted">{label}</div>
    </div>
  );
}

function CreateInviteSheet({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (inv: InviteOut) => void }) {
  const [note, setNote] = useState('');
  const [days, setDays] = useState('7');
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    const inv = await attempt(() => api.createInvite({ note: note.trim(), days: Number(days) }));
    setBusy(false);
    if (inv) {
      setNote('');
      onCreated(inv);
    }
  };

  return (
    <Sheet open={open} onClose={onClose} title="Новое приглашение">
      <form onSubmit={submit} className="space-y-4 pb-2">
        <Field label="Для кого">
          <input className="input" placeholder="Например, Анна" maxLength={128} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <div>
          <span className="label">Код действует</span>
          <Segmented label="Срок действия" value={days} options={DAYS} onChange={setDays} />
        </div>
        <p className="text-xs text-muted">Код одноразовый: по нему человек сам выберет имя и пароль. Имя «Для кого» станет его отображаемым именем.</p>
        <Button type="submit" variant="primary" block busy={busy}>
          Создать код
        </Button>
      </form>
    </Sheet>
  );
}

function inviteMessage(inv: InviteOut, hubUrl: string): string {
  const apk = `${hubUrl.replace(/\/$/, '')}/app/latest.apk`;
  return [
    `Приглашение в HomeMusic${inv.note ? ` для: ${inv.note}` : ''}.`,
    `1. Установите приложение: ${apk}`,
    '2. На экране входа нажмите «Далее», затем «Регистрация по коду».',
    `3. Введите код ${inv.code} и придумайте имя и пароль.`,
    `Код действует до ${fmtDate(inv.expires_at)}.`,
  ].join('\n');
}

function InviteSheet({ invite, onClose }: { invite?: InviteOut; onClose: () => void }) {
  const { me } = useSession();
  if (!invite) return null;
  const text = inviteMessage(invite, me.public_hub_url || window.location.origin);
  const copy = async (what: string, done: string) => toast((await copyText(what)) ? done : 'Не удалось скопировать', 'success');
  return (
    <Sheet open onClose={onClose} title="Код приглашения">
      <div className="space-y-4 pb-2 text-center">
        <div className="rounded-xl bg-raised py-5 font-mono text-3xl font-bold tracking-[0.2em]">{invite.code}</div>
        <p className="text-sm text-muted">
          {invite.note ? `Для: ${invite.note}. ` : ''}Действует до {fmtDate(invite.expires_at)}, один раз.
        </p>
        <pre className="whitespace-pre-wrap rounded-lg bg-bg p-3 text-left text-xs text-muted">{text}</pre>
        <div className="flex gap-2">
          {canShare() && (
            <Button variant="primary" icon="send" block onClick={() => void shareText(text)}>
              Отправить
            </Button>
          )}
          <Button icon="paste" block onClick={() => void copy(text, 'Сообщение скопировано')}>
            Скопировать
          </Button>
        </div>
        <button type="button" className="min-h-11 text-sm font-semibold text-accent" onClick={() => void copy(invite.code, 'Код скопирован')}>
          Скопировать только код
        </button>
      </div>
    </Sheet>
  );
}
