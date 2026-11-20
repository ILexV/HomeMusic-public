import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { api } from '../api/client';
import type { ImportCollectionOut, ImportItemOut, ImportMode, ImportOut } from '../api/types';
import { Icon, type IconName } from '../components/Icon';
import { ListenButton } from '../components/catalog';
import { Button, Checkbox, Cover, Empty, ErrorState, IconButton, Loading, Notice, PageHeader, ProgressBar, Radio, SectionTitle, SkeletonRows, Spinner, cx } from '../components/ui';
import { EMBEDDED } from '../lib/embedded';
import { useHubEvent } from '../lib/events';
import { fmtDuration, fmtSize, fmtWhen, plural, tracksWord } from '../lib/format';
import { useAsync, useInterval } from '../lib/hooks';
import { listen, type ListenTrack } from '../lib/listen';
import { TrackMenuButton } from '../components/TrackMenu';
import { canPlay, canPlayAll, playList, playMixed } from '../lib/player';
import { Link, navigate } from '../lib/router';
import { copyText } from '../lib/share';
import { attempt, confirmDialog, toast } from '../lib/ui';

/** «Перенести музыку»: a user's collection from Yandex Music (Spotify and YouTube Music later) — ♥, playlists in
 * their order, liked albums and artists. The Hub reads the lists only (no audio): sign-in by a code on ya.ru/device,
 * a token, or an open profile link. Economy (the default): playlists and ♥ at once from what the library has, the
 * rest plays from the sources and is downloaded once listened to; «Скачать всё» downloads everything in the
 * background, below the users' own downloads. */

const KIND_ICON: Record<ImportCollectionOut['kind'], IconName> = {
  likes: 'heart',
  playlist: 'playlist',
  albums: 'disc',
  artists: 'user',
  dislikes: 'x',
};

const ACTIVE = ['auth', 'fetching', 'running'];

function statusLine(imp: ImportOut): string {
  const st = imp.stats;
  switch (imp.status) {
    case 'auth':
      return 'Ждём входа в Яндекс';
    case 'fetching':
      return 'Читаем коллекцию…';
    case 'preview':
      return `Найдено ${tracksWord(st.tracks)} — выберите, что перенести`;
    case 'running':
      return st.pending ? `Сопоставляем: осталось ${st.pending}` : st.downloading ? `Качается ${st.downloading}, скачано ${st.done}` : 'Переносим…';
    case 'done':
      return `Перенесено${imp.finished_at ? ' ' + fmtWhen(imp.finished_at) : ''}`;
    case 'cancelled':
      return 'Остановлен';
    default:
      return imp.error || 'Не получилось';
  }
}

function sourceTitle(imp: ImportOut): string {
  return `Яндекс Музыка${imp.account ? ` · ${imp.account}` : ''}`;
}

// --- the list of imports + a new one ---------------------------------------------------------------

export function ImportsPage() {
  const res = useAsync((s) => api.imports(s), []);
  useHubEvent('import', () => res.reload(true));
  const [busy, setBusy] = useState<'device' | 'profile' | 'token' | null>(null);
  const [profile, setProfile] = useState('');
  const [token, setToken] = useState('');

  const start = async (method: 'device' | 'profile' | 'token') => {
    setBusy(method);
    const r = await attempt(() =>
      api.createImport(method === 'profile' ? { method, url: profile.trim() } : method === 'token' ? { method, token: token.trim() } : { method }),
    );
    setBusy(null);
    if (r) navigate(`/import/${r.id}`);
  };

  return (
    <div>
      <PageHeader title="Перенести музыку" back={EMBEDDED ? '/library' : '/more'} />
      {res.loading ? (
        <SkeletonRows n={2} />
      ) : res.error ? (
        <ErrorState text={res.error} onRetry={() => res.reload()} />
      ) : res.data?.length ? (
        <ul className="mx-4 mb-2 overflow-hidden rounded-xl bg-surface">
          {res.data.map((imp) => (
            <li key={imp.id} className="border-b border-line/60 last:border-0">
              <Link to={`/import/${imp.id}`} className="flex min-h-16 items-center gap-3 px-4 py-2 hover:bg-fg/5">
                <Icon name="transfer" size={22} className="shrink-0 text-muted" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{sourceTitle(imp)}</span>
                  <span className={cx('block truncate text-xs', imp.status === 'failed' ? 'text-danger' : 'text-muted')}>{statusLine(imp)}</span>
                </span>
                {ACTIVE.includes(imp.status) ? <Spinner size={16} className="text-accent" /> : <Icon name="chevron" size={18} className="text-dim" />}
              </Link>
            </li>
          ))}
        </ul>
      ) : null}

      <SectionTitle>Из Яндекс Музыки</SectionTitle>
      <div className="card mx-4 space-y-3 p-4">
        <p className="text-sm text-muted">«Мне нравится», плейлисты (и закрытые), любимые альбомы и исполнители. Подписка Плюс не нужна.</p>
        <Button variant="primary" block icon="lock" busy={busy === 'device'} disabled={!!busy} onClick={() => start('device')}>
          Войти через Яндекс
        </Button>
        <p className="text-xs text-dim">Хаб покажет код — введите его на ya.ru/device. Хаб читает только списки треков, пароль он не видит. Вход можно удалить в любой момент.</p>
      </div>

      <div className="card mx-4 mt-3 space-y-2 p-4">
        <div className="font-semibold">Без входа — по ссылке на профиль</div>
        <p className="text-xs text-muted">Если медиатека открыта всем. Закрытые плейлисты и «Не нравится» так не перенести.</p>
        <input className="input" value={profile} onChange={(e) => setProfile(e.target.value)} placeholder="music.yandex.ru/users/логин/playlists/3" inputMode="url" autoCapitalize="off" autoCorrect="off" />
        <Button block busy={busy === 'profile'} disabled={!!busy || !profile.trim()} onClick={() => start('profile')}>
          Прочитать профиль
        </Button>
      </div>

      <details className="card mx-4 mt-3 p-4">
        <summary className="cursor-pointer text-sm text-muted">Вставить токен Яндекс Музыки вручную</summary>
        <div className="mt-3 space-y-2">
          <input className="input" type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="Токен" autoComplete="off" />
          <Button block busy={busy === 'token'} disabled={!!busy || !token.trim()} onClick={() => start('token')}>
            Продолжить с токеном
          </Button>
        </div>
      </details>

      <p className="px-6 py-6 text-center text-xs text-dim">Spotify и YouTube Music появятся позже.</p>
    </div>
  );
}

// --- one import ---------------------------------------------------------------------------------------

export function ImportPage({ id }: { id: number }) {
  const res = useAsync((s) => api.importGet(id, s), [id]);
  useHubEvent('import', (d) => d.id === id && res.reload(true));
  useHubEvent('reconnect', () => res.reload(true));
  const imp = res.data;
  // SSE carries status changes; counters (matching, downloads) are refreshed now and then
  useInterval(() => res.reload(true), imp && ACTIVE.includes(imp.status) ? (imp.status === 'running' ? 10_000 : 4_000) : null);

  return (
    <div>
      <PageHeader title="Перенос музыки" sub={imp ? sourceTitle(imp) : undefined} back="/import" />
      {res.loading ? (
        <Loading />
      ) : res.error || !imp ? (
        <ErrorState text={res.error ?? 'Не найдено'} onRetry={() => res.reload()} />
      ) : imp.status === 'auth' ? (
        <AuthStep imp={imp} />
      ) : imp.status === 'fetching' ? (
        <Loading text="Читаем коллекцию из Яндекс Музыки…" />
      ) : imp.status === 'preview' ? (
        <Preview imp={imp} onStarted={(r) => res.setData(r)} />
      ) : imp.status === 'failed' && !imp.started_at ? (
        <div className="space-y-4 px-4 pt-2">
          <Notice kind="error">{imp.error || 'Не получилось'}</Notice>
          <Button block variant="primary" onClick={() => navigate('/import', { replace: true })}>
            Начать заново
          </Button>
        </div>
      ) : (
        <Progress imp={imp} onChange={(r) => res.setData(r)} />
      )}
    </div>
  );
}

function AuthStep({ imp }: { imp: ImportOut }) {
  const auth = imp.auth;
  const [left, setLeft] = useState(0);
  useEffect(() => {
    if (!auth) return;
    const tick = () => setLeft(Math.max(0, Math.round((Date.parse(auth.expires_at + (auth.expires_at.endsWith('Z') ? '' : 'Z')) - Date.now()) / 1000)));
    tick();
    const t = window.setInterval(tick, 1000);
    return () => window.clearInterval(t);
  }, [auth]);
  if (!auth) return <Loading />;
  const copy = async () => {
    if (await copyText(auth.user_code)) toast('Код скопирован', 'success');
    else toast('Не удалось скопировать — введите код вручную', 'info');
  };
  return (
    <div className="space-y-4 px-4 pt-2">
      <ol className="list-decimal space-y-1 pl-5 text-sm text-muted">
        <li>Откройте ya.ru/device и войдите в свой Яндекс ID.</li>
        <li>Введите код ниже и подтвердите доступ для «Яндекс Музыки».</li>
        <li>Вернитесь сюда — Хаб сам продолжит.</li>
      </ol>
      <div className="card flex flex-col items-center gap-3 p-5">
        <div className="select-all break-all text-center font-mono text-3xl font-bold tracking-[0.2em]">{auth.user_code}</div>
        <div className="flex w-full flex-wrap justify-center gap-2">
          <Button small icon="paste" onClick={copy}>
            Скопировать
          </Button>
          <a href={auth.verification_url} target="_blank" rel="noopener noreferrer" className="inline-flex min-h-9 items-center gap-2 rounded-full bg-accent px-3.5 text-sm font-semibold text-on-accent">
            <Icon name="external" size={18} />
            Открыть ya.ru/device
          </a>
        </div>
      </div>
      <div className="flex items-center justify-center gap-2 text-sm text-muted">
        <Spinner size={16} />
        {left > 0 ? `Ждём подтверждения · код действует ${fmtDuration(left)}` : 'Код устарел — начните заново'}
      </div>
    </div>
  );
}

function Preview({ imp, onStarted }: { imp: ImportOut; onStarted: (r: ImportOut) => void }) {
  const [picked, setPicked] = useState<Set<number>>(() => new Set(imp.collections.filter((c) => c.selected && c.kind !== 'dislikes').map((c) => c.id)));
  const [mode, setMode] = useState<ImportMode>('economy');
  const [albums, setAlbums] = useState(false);
  const [keep, setKeep] = useState(false);
  const [busy, setBusy] = useState(false);
  const toggle = (cid: number, on: boolean) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (on) next.add(cid);
      else next.delete(cid);
      return next;
    });

  const st = imp.stats;
  // the Hub counts distinct tracks of all collections; the share of the picked ones scales it
  const est = useMemo(() => {
    const tracks = imp.collections.filter((c) => c.kind === 'likes' || c.kind === 'playlist');
    const all = tracks.reduce((n, c) => n + c.count - c.owned, 0);
    const sel = tracks.filter((c) => picked.has(c.id)).reduce((n, c) => n + c.count - c.owned, 0);
    const k = all ? Math.min(1, sel / all) : 0;
    return { tracks: Math.round(st.to_download * k), bytes: Math.round(st.est_bytes * k) };
  }, [imp, picked, st]);
  const lowSpace = st.free_bytes != null && est.bytes > st.free_bytes * 0.9;
  const albumsPicked = imp.collections.some((c) => c.kind === 'albums' && picked.has(c.id));

  const go = async () => {
    setBusy(true);
    const r = await attempt(() =>
      api.importStart(imp.id, { collection_ids: [...picked], mode, download_albums: mode === 'full' && albums, keep_token: keep }),
    );
    setBusy(false);
    if (r) {
      onStarted(r);
      toast(mode === 'economy' ? 'Переносим: плейлисты и ♥ появятся через минуту' : 'Переносим: загрузка пойдёт в фоне', 'success');
    }
  };

  return (
    <div className="pb-6">
      <p className="px-4 pb-1 text-sm text-muted">
        Найдено {tracksWord(st.tracks)}, из них уже в библиотеке {st.owned}. Отметьте, что перенести.
      </p>
      <ul className="mx-4 overflow-hidden rounded-xl bg-surface">
        {imp.collections.map((c) => (
          <li key={c.id} className="flex items-center gap-1 border-b border-line/60 pr-2 last:border-0">
            <Checkbox checked={picked.has(c.id)} onChange={(v) => toggle(c.id, v)} label={c.title} />
            <Icon name={KIND_ICON[c.kind]} size={18} className="shrink-0 text-muted" />
            <span className="min-w-0 flex-1 py-2 pl-2">
              <span className="block truncate">{c.title}</span>
              <span className="block truncate text-xs text-muted">{collectionSub(c)}</span>
            </span>
          </li>
        ))}
      </ul>

      <SectionTitle>Как переносить</SectionTitle>
      <div className="mx-4 space-y-2">
        <ModeCard on={mode === 'economy'} onClick={() => setMode('economy')} title="Экономно" badge="рекомендуем">
          Плейлисты и ♥ появятся сразу. Всё слушается сразу, а на диск трек скачивается, когда вы его послушали.
        </ModeCard>
        <ModeCard on={mode === 'full'} onClick={() => setMode('full')} title="Скачать всё">
          Около {tracksWord(est.tracks)}, ~{fmtSize(est.bytes)}
          {st.free_bytes != null ? ` (свободно ${fmtSize(st.free_bytes)})` : ''}. Качается в фоне несколько часов, обычные загрузки идут первыми.
        </ModeCard>
        {mode === 'full' && lowSpace && <Notice kind="error">На диске не хватит места — выберите меньше или режим «Экономно».</Notice>}
        {mode === 'full' && albumsPicked && (
          <label className="flex items-center gap-1 text-sm">
            <Checkbox checked={albums} onChange={setAlbums} label="Скачивать любимые альбомы целиком" />
            Скачивать любимые альбомы целиком
          </label>
        )}
        {imp.method !== 'profile' && (
          <label className="flex items-center gap-1 text-sm">
            <Checkbox checked={keep} onChange={setKeep} label="Запомнить вход" />
            <span className="min-w-0">Запомнить вход, чтобы потом подтянуть новое</span>
          </label>
        )}
      </div>
      <div className="px-4 pt-4">
        <Button variant="primary" block busy={busy} disabled={!picked.size || (mode === 'full' && lowSpace)} onClick={go}>
          Перенести
        </Button>
      </div>
    </div>
  );
}

function collectionSub(c: ImportCollectionOut): string {
  if (c.kind === 'albums') return `${c.count} ${plural(c.count, ['альбом', 'альбома', 'альбомов'])} · ★ и по желанию загрузка`;
  if (c.kind === 'artists') return `${c.count} ${plural(c.count, ['исполнитель', 'исполнителя', 'исполнителей'])} · ★ в библиотеке`;
  if (c.kind === 'dislikes') return `${tracksWord(c.count)} · только запомнить`;
  return `${tracksWord(c.count)}${c.owned ? ` · ${c.owned} уже есть` : ''}`;
}

function ModeCard({ on, onClick, title, badge, children }: { on: boolean; onClick: () => void; title: string; badge?: string; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={on} className={cx('card flex w-full items-start gap-3 p-4 text-left ring-1 transition', on ? 'ring-accent' : 'ring-transparent')}>
      <span className="pt-0.5">
        <Radio checked={on} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2 font-semibold">
          {title}
          {badge && <span className="rounded-full bg-accent/15 px-2 py-0.5 text-[11px] font-medium text-accent">{badge}</span>}
        </span>
        <span className="mt-1 block text-sm text-muted">{children}</span>
      </span>
    </button>
  );
}

function Progress({ imp, onChange }: { imp: ImportOut; onChange: (r: ImportOut) => void }) {
  const st = imp.stats;
  const [busy, setBusy] = useState<string | null>(null);
  const matched = st.tracks - st.pending;
  const run = async (key: string, fn: () => Promise<ImportOut | void>, ok?: string) => {
    setBusy(key);
    const r = await attempt(fn, ok);
    setBusy(null);
    if (r) onChange(r);
  };
  const remove = async () => {
    if (!(await confirmDialog({ title: 'Удалить перенос?', body: 'Музыка, ♥ и плейлисты останутся. Удалятся только этот отчёт и вход в Яндекс.', okLabel: 'Удалить', danger: true }))) return;
    if (await attempt(() => api.importDelete(imp.id))) navigate('/import', { replace: true });
  };
  const tracks = imp.collections.filter((c) => c.selected && (c.kind === 'likes' || c.kind === 'playlist'));
  const other = imp.collections.filter((c) => c.selected && (c.kind === 'albums' || c.kind === 'artists'));

  return (
    <div className="pb-6">
      <div className="card mx-4 space-y-2 p-4">
        <div className="flex items-center gap-2 font-semibold">
          {imp.status === 'running' ? <Spinner size={16} className="text-accent" /> : <Icon name={imp.status === 'done' ? 'check' : 'alert'} size={18} className={imp.status === 'done' ? 'text-accent' : 'text-warn'} />}
          {imp.status === 'running' ? 'Переносим' : imp.status === 'done' ? 'Перенесено' : 'Остановлено'}
          <span className="ml-auto text-xs font-normal text-muted">{imp.mode === 'economy' ? 'экономно' : 'скачать всё'}</span>
        </div>
        {st.pending > 0 && (
          <>
            <div className="text-sm text-muted">
              Сопоставлено {matched} из {st.tracks}
            </div>
            <ProgressBar value={st.tracks ? matched / st.tracks : 0} />
          </>
        )}
        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
          <Stat label="уже было" value={st.owned} />
          <Stat label="скачано" value={st.done} />
          {st.downloading > 0 && <Stat label="качается" value={st.downloading} />}
          {st.waiting > 0 && <Stat label="ждут прослушивания" value={st.waiting} />}
        </div>
        {imp.error && <Notice kind="error">{imp.error}</Notice>}
      </div>

      <SectionTitle>Отчёт</SectionTitle>
      <ul className="mx-4 overflow-hidden rounded-xl bg-surface">
        <ReportRow to={`/import/${imp.id}/items?match=exact,owned`} icon="check" tone="text-accent" title="Точно" value={st.exact + st.owned} />
        <ReportRow to={`/import/${imp.id}/items?match=probable`} icon="alert" tone="text-warn" title="Вероятно — проверьте" value={st.probable} />
        <ReportRow to={`/import/${imp.id}/items?match=not_found`} icon="x" tone="text-danger" title="Не найдено" value={st.not_found} />
        {st.failed > 0 && <ReportRow to={`/import/${imp.id}/items?status=failed`} icon="refresh" tone="text-danger" title="Не скачалось" value={st.failed} />}
      </ul>

      <SectionTitle>Что перенесли</SectionTitle>
      <ul className="mx-4 overflow-hidden rounded-xl bg-surface">
        {[...tracks, ...other].map((c) => (
          <li key={c.id} className="border-b border-line/60 last:border-0">
            <Link to={`/import/${imp.id}/items?c=${c.id}`} className="flex min-h-14 items-center gap-3 px-4 py-2 hover:bg-fg/5">
              <Icon name={KIND_ICON[c.kind]} size={20} className="shrink-0 text-muted" />
              <span className="min-w-0 flex-1">
                <span className="block truncate">{c.title}</span>
                <span className="block truncate text-xs text-muted">{collectionSub(c)}</span>
              </span>
              <Icon name="chevron" size={18} className="text-dim" />
            </Link>
          </li>
        ))}
      </ul>

      <div className="space-y-2 px-4 pt-4">
        {imp.mode === 'economy' && st.waiting > 0 && (
          <Button block icon="download" busy={busy === 'all'} onClick={() => run('all', () => api.importDownloadAll(imp.id), 'Остальное скачается в фоне')}>
            Скачать всё сейчас ({st.waiting})
          </Button>
        )}
        {imp.status !== 'running' && (imp.has_token || imp.method === 'profile') && (
          <Button block icon="refresh" busy={busy === 'sync'} onClick={() => run('sync', () => api.importSync(imp.id), 'Подтягиваем новое из Яндекса')}>
            Подтянуть новое из Яндекса
          </Button>
        )}
        {imp.has_token && (
          <Button block variant="ghost" icon="lock" busy={busy === 'token'} onClick={() => run('token', () => api.importForgetToken(imp.id), 'Вход в Яндекс удалён')}>
            Удалить вход в Яндекс
          </Button>
        )}
        {imp.status === 'running' ? (
          <Button block variant="danger" busy={busy === 'stop'} onClick={() => run('stop', () => api.importCancel(imp.id))}>
            Остановить перенос
          </Button>
        ) : (
          <Button block variant="ghost" icon="trash" onClick={remove}>
            Удалить перенос
          </Button>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="min-w-0">
      <span className="font-semibold">{value}</span> <span className="text-muted">{label}</span>
    </div>
  );
}

function ReportRow({ to, icon, tone, title, value }: { to: string; icon: IconName; tone: string; title: string; value: number }) {
  return (
    <li className="border-b border-line/60 last:border-0">
      <Link to={to} className="flex min-h-12 items-center gap-3 px-4 py-2 hover:bg-fg/5">
        <Icon name={icon} size={18} className={cx('shrink-0', tone)} />
        <span className="min-w-0 flex-1 truncate">{title}</span>
        <span className="text-sm text-muted">{value}</span>
        <Icon name="chevron" size={18} className="text-dim" />
      </Link>
    </li>
  );
}

// --- rows of an import: a collection, or a part of the report -------------------------------------------

const PAGE = 300;

const MATCH_LABEL: Record<string, [string, string]> = {
  owned: ['есть', 'text-accent'],
  exact: ['точно', 'text-accent'],
  probable: ['вероятно', 'text-warn'],
  not_found: ['не найдено', 'text-danger'],
};

function rowState(it: ImportItemOut): string {
  if (it.status === 'waiting') return it.kind === 'track' ? 'скачается после прослушивания' : 'в каталоге';
  if (it.status === 'queued') return it.download_status === 'needs_review' ? 'выберите файл' : 'качается';
  if (it.status === 'failed') return it.error || 'не скачалось';
  if (it.status === 'pending') return 'сопоставляем…';
  if (it.status === 'skipped') return 'пропущено';
  return '';
}

export function ImportItemsPage({ id, collectionId, match, status }: { id: number; collectionId?: number; match?: string; status?: string }) {
  const imp = useAsync((s) => api.importGet(id, s), [id]);
  const [limit, setLimit] = useState(PAGE);
  const res = useAsync((s) => api.importItems(id, { collection_id: collectionId, match, status, limit }, s), [id, collectionId, match, status, limit]);
  useHubEvent('import', (d) => d.id === id && res.reload(true));
  const coll = imp.data?.collections.find((c) => c.id === collectionId);
  const title = coll?.title ?? (match === 'not_found' ? 'Не найдено' : match === 'probable' ? 'Вероятно' : status === 'failed' ? 'Не скачалось' : 'Точно');
  const items = res.data?.items ?? [];
  const tracks = items.filter((x) => x.kind === 'track' && x.status !== 'skipped');
  const playable = tracks.filter((x) => x.nd_song_id || x.ref);
  const [busyId, setBusyId] = useState<number | null>(null);

  const play = (from?: ImportItemOut) => {
    if (canPlayAll()) {
      playMixed(
        playable.map((x) => (x.nd_song_id ? { song_id: x.nd_song_id } : { ref: x.ref as string, title: x.title, artist: x.artist, album: x.album, cover_url: x.cover_url, duration_s: x.duration_s })),
        from ? playable.indexOf(from) : null,
      );
    } else if (canPlay() && from?.nd_song_id) {
      playList(playable, (x) => x.nd_song_id, from);
    } else if (from?.ref) {
      const queue = playable.filter((x) => !x.nd_song_id && x.ref).map(listenItem);
      listen(listenItem(from), queue);
    }
  };
  const pick = async (it: ImportItemOut) => {
    setBusyId(it.id);
    const r = await attempt(() => api.importPick(it.id));
    setBusyId(null);
    if (r) navigate(`/downloads/${r.download_item_id}`);
  };

  return (
    <div>
      <PageHeader
        title={title}
        sub={res.data ? `${res.data.total} ${plural(res.data.total, ['строка', 'строки', 'строк'])}` : undefined}
        back={`/import/${id}`}
        right={
          canPlayAll() && playable.length ? <IconButton icon="play" label="Слушать все" onClick={() => play()} className="!text-accent" /> : undefined
        }
      />
      {match === 'not_found' && <p className="px-4 pb-2 text-sm text-muted">Таких треков нет в каталоге. Нажмите «Выбрать файл» — Хаб поищет по источникам, а вы выберете подходящий.</p>}
      {match === 'probable' && <p className="px-4 pb-2 text-sm text-muted">Похоже, но не наверняка (другая длительность или версия). Если трек не тот — выберите файл сами.</p>}
      {res.loading ? (
        <SkeletonRows />
      ) : res.error ? (
        <ErrorState text={res.error} onRetry={() => res.reload()} />
      ) : items.length === 0 ? (
        <Empty icon="check" title="Здесь пусто" />
      ) : (
        <ul className="divide-y divide-line/60">
          {items.map((it, i) => (
            <ItemRow key={it.id} n={i + 1} it={it} review={collectionId == null} busy={busyId === it.id} onPlay={it.kind === 'track' && (it.nd_song_id || it.ref) ? () => play(it) : undefined} onPick={() => pick(it)} />
          ))}
        </ul>
      )}
      {res.data && res.data.total > items.length && (
        <div className="px-4 py-4">
          <Button block onClick={() => setLimit((n) => n + PAGE)}>
            Показать ещё
          </Button>
        </div>
      )}
    </div>
  );
}

const listenItem = (x: ImportItemOut): ListenTrack => ({ ref: x.ref as string, title: x.title, artist: x.artist, cover_url: x.cover_url, duration_s: x.duration_s });

/** `review`: a part of the report — a probable match offers to pick the file (in a collection it plays). */
function ItemRow({ n, it, review, busy, onPlay, onPick }: { n: number; it: ImportItemOut; review: boolean; busy: boolean; onPlay?: () => void; onPick: () => void }) {
  const [label, tone] = MATCH_LABEL[it.match ?? ''] ?? ['', ''];
  const state = rowState(it);
  const needsPick = it.kind === 'track' && (it.status === 'not_found' || it.status === 'failed' || (review && it.match === 'probable' && it.status !== 'queued'));
  const href = it.kind === 'album' && it.ref ? `/album/${encodeURIComponent(it.ref)}` : it.kind === 'artist' && it.ref ? `/artist/${encodeURIComponent(it.ref)}` : it.download_item_id ? `/downloads/${it.download_item_id}` : null;
  const inner = (
    <>
      <span className="w-6 shrink-0 text-right text-xs text-dim">{n}</span>
      <Cover src={it.cover_url} size={44} round={it.kind === 'artist'} icon={it.kind === 'album' ? 'disc' : it.kind === 'artist' ? 'user' : 'note'} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[15px]">{it.title}</span>
        <span className="block truncate text-[13px] text-muted">{it.kind === 'artist' ? 'Исполнитель' : [it.artist, it.album].filter(Boolean).join(' · ')}</span>
        <span className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs">
          {label && <span className={cx('shrink-0 font-medium', tone)}>{label}</span>}
          {state && <span className="truncate text-dim">{state}</span>}
        </span>
      </span>
    </>
  );
  return (
    <li className="flex items-center gap-2 py-2 pl-2 pr-1">
      {href ? (
        <Link to={href} className="flex min-w-0 flex-1 items-center gap-3">
          {inner}
        </Link>
      ) : (
        <div className="flex min-w-0 flex-1 items-center gap-3">{inner}</div>
      )}
      {it.kind === 'track' && (it.ref?.startsWith('dz:') || it.nd_song_id) && <TrackMenuButton track={{ title: it.title, artist: it.artist, ref: it.ref, nd_song_id: it.nd_song_id }} />}
      {needsPick ? (
        <Button small busy={busy} onClick={onPick}>
          Выбрать файл
        </Button>
      ) : onPlay ? (
        !canPlay() && !it.nd_song_id && it.ref ? (
          <ListenButton track={{ ref: it.ref, title: it.title }} onListen={onPlay} />
        ) : canPlay() ? (
          <IconButton icon="play" label={`Слушать «${it.title}»`} onClick={onPlay} className="!text-accent" />
        ) : null
      ) : null}
    </li>
  );
}
