import { setListeningAccount } from './lib/plays';
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { ApiError, UNAUTHORIZED_EVENT, api, errorMessage, invalidateCache } from './api/client';
import type { MeOut } from './api/types';
import { MiniPlayer, PlayerSpacer } from './components/MiniPlayer';
import { Overlays } from './components/Overlays';
import { TabBar, type Tab } from './components/TabBar';
import { ErrorState, Spinner, cx } from './components/ui';
import { STANDALONE } from './lib/embedded';
import { hubEvents } from './lib/events';
import { restoreListening, stopListening } from './lib/listen';
import { restoreWave } from './lib/wave';
import { resetStars } from './lib/stars';
import { startNotificationWatcher, stopNotificationWatcher } from './lib/notify';
import { SessionContext } from './lib/session';
import { matchPath, navigate, useRoute, type Route } from './lib/router';
import { AdminPage } from './pages/Admin';
import { AdminUserPage } from './pages/AdminUser';
import { SystemHealthPage } from './pages/SystemHealth';
import { TasteMatrixPage } from './pages/insights/Matrix';
import { InsightsOverviewPage } from './pages/insights/Overview';
import { MixDetailPage, WaveDetailPage } from './pages/insights/Recs';
import { TrackWeightPage } from './pages/insights/Track';
import { AlbumPage } from './pages/Album';
import { ArtistPage } from './pages/Artist';
import { AssistantChatsPage, AssistantPage } from './pages/Assistant';
import { AssistantTaskPage } from './pages/AssistantTask';
import { DownloadDetailPage } from './pages/DownloadDetail';
import { DownloadsPage } from './pages/Downloads';
import { HomePage } from './pages/Home';
import { ImportItemsPage, ImportPage, ImportsPage } from './pages/Import';
import { LibraryPage } from './pages/Library';
import { LibraryAlbumPage } from './pages/LibraryAlbum';
import { LoginPage } from './pages/Login';
import { ArtistsPage, LikedPage, NdAlbumPage, NdAlbumsPage, NdArtistPage, PlaylistPage } from './pages/MediaLibrary';
import { MixPage, TastePage } from './pages/Mix';
import { MorePage } from './pages/More';
import { PlayerPage } from './pages/Player';
import { PlaylistsPage } from './pages/Playlists';
import { SearchPage, searchTab } from './pages/Search';
import { SharePage } from './pages/Share';
import { UploadPage } from './pages/Upload';
import { RecommendationPausesPage, RecommendationQualityPage } from './pages/Recommendations';
import { ReleasesPage } from './pages/Releases';

type RouteDef = [pattern: string, tab: Tab | null, render: (p: Record<string, string>, r: Route) => ReactNode];

/** Screens of the standalone service (a plain browser, `STANDALONE`): inside the app they are its native ones, so
 * these routes don't exist there (an unknown route goes to the catalog). */
const STANDALONE_ROUTES: RouteDef[] = [
  ['/home', 'home', () => <HomePage />],
  ['/player', null, () => <PlayerPage />],
  ['/liked', 'library', () => <LikedPage />],
  ['/artists', 'library', () => <ArtistsPage />],
  ['/playlist/:id', 'library', (p) => <PlaylistPage key={p.id} id={p.id} />],
  ['/lib/albums', 'library', () => <NdAlbumsPage />],
  ['/lib/album/:id', 'library', (p) => <NdAlbumPage key={p.id} id={p.id} />],
  ['/lib/artist/:id', 'library', (p) => <NdArtistPage key={p.id} id={p.id} />],
];

/** Where an unknown or empty route goes. */
const START = STANDALONE ? '/home' : '/search';

const ROUTES: RouteDef[] = [
  ...(STANDALONE ? STANDALONE_ROUTES : []),
  ['/search', 'search', (_, r) => <SearchPage initialQuery={r.query.get('q') ?? ''} initialTab={searchTab(r.query.get('tab'))} />],
  ['/artist/:ref', 'search', (p) => <ArtistPage key={p.ref} refId={p.ref} />],
  ['/album/:ref', 'search', (p) => <AlbumPage key={p.ref} refId={p.ref} />],
  ['/mix/:id', 'search', (p) => <MixPage key={p.id} id={Number(p.id)} />],
  ['/taste', 'search', () => <TastePage />],
  ['/downloads', 'downloads', () => <DownloadsPage />],
  ['/downloads/:id', 'downloads', (p) => <DownloadDetailPage key={p.id} id={Number(p.id)} />],
  ['/library', 'library', (_, r) => <LibraryPage initialQuery={r.query.get('q') ?? ''} />],
  ['/library/album/:id', 'library', (p) => <LibraryAlbumPage key={p.id} id={Number(p.id)} />],
  ['/assistant', 'assistant', (_, r) => <AssistantPage chatId={Number(r.query.get('chat')) || null} />],
  ['/assistant/chats', 'assistant', () => <AssistantChatsPage />],
  ['/assistant/:id', 'assistant', (p) => <AssistantTaskPage key={p.id} id={Number(p.id)} />],
  ['/upload', 'more', () => <UploadPage />],
  ['/playlists', STANDALONE ? 'library' : 'more', () => <PlaylistsPage />],
  ['/import', 'more', () => <ImportsPage />],
  ['/import/:id', 'more', (p) => <ImportPage key={p.id} id={Number(p.id)} />],
  [
    '/import/:id/items',
    'more',
    (p, r) => (
      <ImportItemsPage
        key={r.full}
        id={Number(p.id)}
        collectionId={r.query.get('c') ? Number(r.query.get('c')) : undefined}
        match={r.query.get('match') ?? undefined}
        status={r.query.get('status') ?? undefined}
      />
    ),
  ],
  ['/more', 'more', () => <MorePage />],
  ['/recommendations', 'more', () => <RecommendationQualityPage />],
  ['/recommendations/pauses', 'more', () => <RecommendationPausesPage />],
  ['/releases', 'more', () => <ReleasesPage />],
  ['/admin', 'more', () => <AdminPage />],
  ['/admin/system', 'more', () => <SystemHealthPage />],
  ['/admin/user/:id', 'more', (p) => <AdminUserPage key={p.id} id={Number(p.id)} />],
  ['/admin/insights', 'more', () => <InsightsOverviewPage />],
  ['/admin/taste', 'more', () => <TasteMatrixPage />],
  ['/admin/user/:id/track', 'more', (p) => <TrackWeightPage key={p.id} userId={Number(p.id)} />],
  ['/admin/mix/:id', 'more', (p) => <MixDetailPage key={p.id} id={Number(p.id)} />],
  ['/admin/wave/:sid', 'more', (p) => <WaveDetailPage key={p.sid} sid={p.sid} />],
  ['/share', null, () => <SharePage />],
];

const lastByTab: Partial<Record<Tab, string>> = {};

function resolve(route: Route): { tab: Tab | null; node: ReactNode } | null {
  for (const [pattern, tab, render] of ROUTES) {
    const params = matchPath(pattern, route.path);
    if (params) return { tab, node: render(params, route) };
  }
  return null;
}

export function App() {
  const [me, setMe] = useState<MeOut | null | undefined>(undefined);
  const [bootError, setBootError] = useState<string>();
  const route = useRoute();

  const boot = useCallback(() => {
    setBootError(undefined);
    api
      .me()
      .then(setMe)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) setMe(null);
        else setBootError(errorMessage(e));
      });
  }, []);

  useEffect(boot, [boot]);

  useEffect(() => {
    const account = me?.id ?? null;
    setListeningAccount(account);
    restoreListening(account);
    restoreWave(account);
  }, [me]);

  useEffect(() => {
    const onUnauthorized = () => {
      invalidateCache();
      restoreWave(null);
      setListeningAccount(null);
      setMe(null);
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  useEffect(() => {
    if (!me) return;
    hubEvents.start();
    startNotificationWatcher();
    return () => {
      stopNotificationWatcher();
      hubEvents.stop();
    };
  }, [me]);

  // Unknown or empty route → the start screen.
  const resolved = resolve(route);
  // Each tab remembers where you were (Spotify-like); tapping the active tab goes to its root.
  if (resolved?.tab) lastByTab[resolved.tab] = route.full;
  useEffect(() => {
    if (me && !resolved) navigate(START, { replace: true });
  }, [me, resolved]);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* ignore: we drop the session anyway */
    }
    invalidateCache();
    restoreWave(null);
    setListeningAccount(null);
    stopListening();
    resetStars();
    setMe(null);
    navigate(START, { replace: true });
  }, []);

  if (me === undefined) {
    return (
      <div className="flex min-h-dvh flex-col items-center justify-center gap-4">
        {bootError ? (
          <ErrorState text={`Не удалось связаться с сервером. ${bootError}`} onRetry={boot} />
        ) : (
          <>
            <img src="/icons/icon.svg" alt="" width={72} height={72} className="rounded-2xl" />
            <Spinner className="text-accent" />
          </>
        )}
        <Overlays />
      </div>
    );
  }

  if (me === null) {
    return (
      <>
        <LoginPage onLogin={setMe} />
        <Overlays />
      </>
    );
  }

  // the full player covers the screen: no mini player and tab bar under it, so no room kept for them
  const fullPlayer = route.path === '/player';
  // the admin's analytics are wide (tables, two columns on a desktop)
  const wide = route.path.startsWith('/admin/');
  return (
    <SessionContext.Provider key={me.id} value={{ me, logout }}>
      <main className={cx('mx-auto min-h-dvh', wide ? 'max-w-6xl' : 'max-w-2xl', !fullPlayer && 'pb-[calc(var(--tabbar-h)+16px+env(safe-area-inset-bottom))]')}>
        {resolved?.node}
        {!fullPlayer && <PlayerSpacer />}
      </main>
      {!fullPlayer && (
        <>
          <MiniPlayer />
          <TabBar active={resolved?.tab ?? null} last={lastByTab} />
        </>
      )}
      <Overlays />
    </SessionContext.Provider>
  );
}
