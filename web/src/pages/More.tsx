import { useEffect, useState, type ReactNode } from 'react';
import { STANDALONE } from '../lib/embedded';
import { useSession } from '../lib/session';
import { Icon, type IconName } from '../components/Icon';
import { PageHeader, cx } from '../components/ui';
import { notifyPermission, requestNotifyPermission, type NotifyPermission } from '../lib/notify';
import { Link } from '../lib/router';
import { setSmartEnabled, useSmartEnabled } from '../lib/smartShuffle';
import { confirmDialog, toast } from '../lib/ui';

interface InstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

// beforeinstallprompt fires early, possibly before this page mounts: keep it globally.
let deferredInstall: InstallPromptEvent | null = null;
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredInstall = e as InstallPromptEvent;
});

export function MorePage() {
  const { me, logout } = useSession();
  const [perm, setPerm] = useState<NotifyPermission>(notifyPermission);
  const [canInstall, setCanInstall] = useState(!!deferredInstall);
  const smart = useSmartEnabled();
  const standalone = window.matchMedia('(display-mode: standalone)').matches;

  useEffect(() => {
    const on = () => setCanInstall(!!deferredInstall);
    window.addEventListener('beforeinstallprompt', on);
    window.addEventListener('appinstalled', on);
    return () => {
      window.removeEventListener('beforeinstallprompt', on);
      window.removeEventListener('appinstalled', on);
    };
  }, []);

  const enableNotifications = async () => {
    const p = await requestNotifyPermission();
    setPerm(p);
    if (p === 'granted') toast('Уведомления включены', 'success');
    else if (p === 'denied') toast('Уведомления запрещены. Разрешите их в настройках сайта в браузере.', 'error');
  };

  const install = async () => {
    if (!deferredInstall) return;
    await deferredInstall.prompt();
    await deferredInstall.userChoice.catch(() => undefined);
    deferredInstall = null;
    setCanInstall(false);
  };

  const doLogout = async () => {
    if (await confirmDialog({ title: 'Выйти из аккаунта?', okLabel: 'Выйти' })) await logout();
  };

  const permText: Record<NotifyPermission, string> = {
    granted: 'Включены',
    denied: 'Запрещены в настройках браузера',
    default: 'Нажмите, чтобы включить',
    unsupported: 'Не поддерживаются этим браузером',
  };

  return (
    <div>
      <PageHeader title="Ещё" />
      <div className="mx-4 mb-4 flex items-center gap-3 rounded-xl bg-surface p-4">
        <span className="flex h-12 w-12 items-center justify-center rounded-full bg-accent text-lg font-bold uppercase text-on-accent">{me.username.slice(0, 1)}</span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-semibold">{me.username}</div>
          <div className="truncate text-xs text-muted">
            {me.is_admin ? 'Администратор' : 'Пользователь'}
            {me.email ? ` · ${me.email}` : ''}
            {me.google_email ? ` · Google: ${me.google_email}` : ''}
          </div>
        </div>
      </div>

      <ul className="mx-4 overflow-hidden rounded-xl bg-surface">
        {STANDALONE && <Item icon="transfer" to="/player" title="Плеер и устройства" sub="Перенести воспроизведение или управлять компьютером с телефона" />}
        {STANDALONE && <Item icon="sparkle" to="/assistant" title="Ассистент" sub="Скриншот, список или просьба — найдёт и скачает" />}
        {STANDALONE && (
          <Item
            icon="shuffleSmart"
            title="Умное перемешивание в режимах"
            sub="Кнопка перемешивания в плейлисте и «Понравившихся» добавляет рекомендации"
            onClick={() => setSmartEnabled(!smart)}
            right={<Switch on={smart} />}
          />
        )}
        <Item icon="upload" to="/upload" title="Загрузить файлы" sub="Свои аудиофайлы с телефона" />
        <Item icon="playlist" to="/playlists" title="Плейлисты" sub="Ваши плейлисты в Navidrome" />
        <Item icon="transfer" to="/import" title="Перенести музыку" sub="Из Яндекс Музыки: ♥, плейлисты, альбомы" />
        {me.is_admin && <Item icon="user" to="/admin" title="Пользователи и приглашения" sub="Расходы на ИИ, песни, коды для новых людей" />}
        {/* the player is here: in the browser, and the app's own on Android */}
        <Item icon="install" href="/app/latest.apk" title="Приложение для Android" sub="HomeMusic: офлайн, плеер в шторке, тот же аккаунт" />
        {me.public_music_url && <Item icon="external" href={me.public_music_url} title="Navidrome" sub="Веб-интерфейс библиотеки" />}
        <Item
          icon="bell"
          title="Уведомления"
          sub={permText[perm]}
          onClick={perm === 'default' ? enableNotifications : undefined}
          right={perm === 'granted' ? <Icon name="check" size={20} className="text-accent" /> : undefined}
        />
        {canInstall && !standalone && <Item icon="install" title="Установить приложение" sub="На домашний экран, с «Поделиться» → HomeMusic" onClick={install} />}
        <Item icon="logout" title="Выйти" onClick={doLogout} danger />
      </ul>

      <p className="px-6 py-6 text-center text-xs text-dim">HomeMusic · личный музыкальный сервис</p>
    </div>
  );
}

function Switch({ on }: { on: boolean }) {
  return (
    <span role="switch" aria-checked={on} className={cx('relative h-6 w-10 shrink-0 rounded-full transition-colors', on ? 'bg-accent' : 'bg-line')}>
      <span className={cx('absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all', on ? 'left-[18px]' : 'left-0.5')} />
    </span>
  );
}

function Item({
  icon,
  title,
  sub,
  to,
  href,
  onClick,
  right,
  danger,
}: {
  icon: IconName;
  title: string;
  sub?: string;
  to?: string;
  href?: string;
  onClick?: () => void;
  right?: ReactNode;
  danger?: boolean;
}) {
  const inner = (
    <>
      <Icon name={icon} size={22} className={danger ? 'text-danger' : 'text-muted'} />
      <span className="min-w-0 flex-1">
        <span className={cx('block', danger && 'text-danger')}>{title}</span>
        {sub && <span className="block truncate text-xs text-muted">{sub}</span>}
      </span>
      {right ?? ((to || href || onClick) && <Icon name={href ? 'external' : 'chevron'} size={18} className="text-dim" />)}
    </>
  );
  const cls = 'flex min-h-16 w-full items-center gap-4 px-4 py-2 text-left hover:bg-fg/5';
  return (
    <li className="border-b border-line/60 last:border-0">
      {to ? (
        <Link to={to} className={cls}>
          {inner}
        </Link>
      ) : href ? (
        <a href={href} target="_blank" rel="noopener noreferrer" className={cls}>
          {inner}
        </a>
      ) : onClick ? (
        <button type="button" onClick={onClick} className={cls}>
          {inner}
        </button>
      ) : (
        <div className={cls}>{inner}</div>
      )}
    </li>
  );
}
