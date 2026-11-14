import { api } from '../api/client';
import { Icon, type IconName } from '../components/Icon';
import { WaveCard } from '../components/Wave';
import { WaveHero } from '../components/WaveHero';
import { ErrorState, SectionTitle, Spinner } from '../components/ui';
import { useDesktop } from '../lib/desktop';
import { useHubEvent } from '../lib/events';
import { useAsync } from '../lib/hooks';
import { Link } from '../lib/router';
import { useSession } from '../lib/session';
import { AlbumRow, ArtistRow } from './MediaLibrary';
import { MixCard } from './Mix';

/** «Главная» of the PWA in a plain browser (`STANDALONE`), like the app's: «Микс дня», «Моя волна», shortcuts to
 * the library, «Радиостанции» (a wave by a library artist, liked ones first) and rows of albums Navidrome knows the
 * user plays. Inside the app this is its native home screen. */
export function HomePage() {
  const { me } = useSession();
  const res = useAsync((s) => api.ndHome(s), []);
  useHubEvent('library', () => res.reload(true));
  const d = res.data;
  const desktop = useDesktop();

  return (
    <div className="pb-4">
      <header className="pt-safe px-4 pb-1 pt-4">
        <h1 className="text-2xl font-bold">{greeting()}, {me.username}</h1>
      </header>
      {desktop ? (
        <>
          <WaveHero />
          <MixCard />
        </>
      ) : (
        <>
          <MixCard />
          <WaveCard />
        </>
      )}
      <div className="grid grid-cols-2 gap-2 px-4 pt-3">
        <Shortcut to="/liked" icon="heart" title="Понравившиеся" />
        <Shortcut to="/playlists" icon="playlist" title="Плейлисты" />
        <Shortcut to="/artists" icon="user" title="Исполнители" />
        <Shortcut to="/lib/albums" icon="disc" title="Все альбомы" />
        <Shortcut to="/releases" icon="disc" title="Новые релизы" />
        <Shortcut to="/recommendations" icon="sparkle" title="Мои рекомендации" />
      </div>

      {res.loading ? (
        <div className="flex justify-center py-10">
          <Spinner className="text-accent" />
        </div>
      ) : res.error ? (
        <ErrorState text={res.error} onRetry={() => res.reload()} />
      ) : d ? (
        <>
          {d.radio.length > 0 && (
            <section aria-label="Радиостанции">
              <SectionTitle right={<Link to="/artists" className="text-sm text-muted hover:text-fg">Все</Link>}>Радиостанции</SectionTitle>
              <ArtistRow artists={d.radio} rowKey="home:radio" radio />
            </section>
          )}
          <AlbumRow title="Недавно слушали" albums={d.recent} rowKey="home:recent" />
          <AlbumRow title="Часто слушаете" albums={d.frequent} rowKey="home:frequent" />
          <AlbumRow title="Понравившиеся альбомы" albums={d.starred} rowKey="home:starred" more="/liked" />
          <AlbumRow title="Новое в библиотеке" albums={d.newest} rowKey="home:newest" more="/lib/albums" />
        </>
      ) : null}
    </div>
  );
}

function greeting(): string {
  const h = new Date().getHours();
  return h < 5 ? 'Доброй ночи' : h < 12 ? 'Доброе утро' : h < 18 ? 'Добрый день' : 'Добрый вечер';
}

function Shortcut({ to, icon, title }: { to: string; icon: IconName; title: string }) {
  return (
    <Link to={to} className="flex min-h-14 items-center gap-2.5 rounded-lg bg-raised px-3 py-2 text-sm font-semibold transition hover:bg-hover">
      <Icon name={icon} size={20} className="shrink-0 text-accent" />
      <span className="truncate">{title}</span>
    </Link>
  );
}
