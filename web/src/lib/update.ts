import { listenBusy } from './listen';
import { ROUTE_EVENT } from './router';

/** Picks up a new PWA build without the user doing anything. The Android app keeps one WebView with the page for
 * the whole life of its process (days), so a rebuilt Hub would otherwise never reach it.
 *
 * `__HM_BUILD__` is this bundle's build id; the Hub reports the one it serves in `/healthz` (`web_build`, read from
 * index.html). Checked when the page comes back on screen (`visibilitychange`, and `hm:resume` from the app when
 * the Hub tab is shown again) and on route changes (at most once a minute). A page with another build reloads
 * itself, keeping its route (the hash):
 * - right away, if it is idle: no sheet or dialog open, no field holding typed text, not on the
 *   upload/share/login screens, the page's own player not playing;
 * - otherwise on the next move to another screen (a new route path; a query rewritten by typing in the search box
 *   does not count), unless the page's player plays. */

const BUILD: string = __HM_BUILD__;
const CHECK_EVERY_MS = 60_000;
/** sessionStorage: the build a reload was already made for (a page still old after it does not reload again). */
const RELOADED_FOR = 'hm-reloaded-for';
/** Routes whose state lives only in the page (files picked, a share being handled, typing a password). */
const HOLD_ROUTES = ['/upload', '/share', '/login'];
/** Event the Android app fires when the Hub tab is shown again (`HubFragment.onStart`). */
export const RESUME_EVENT = 'hm:resume';

let lastCheck = 0;
let checking: Promise<void> | null = null;
/** The build the Hub serves, once it is not ours. */
let newer: string | null = null;

let pendingOperations = 0;

/** For work that survives route changes but would be interrupted by a document reload. */
export function holdAutoUpdate(): () => void {
  pendingOperations++;
  return () => { pendingOperations--; };
}

/** Asks the Hub which build it serves; `throttled` skips it when asked less than a minute ago. */
async function check(throttled: boolean): Promise<void> {
  if (checking) return checking;
  if (throttled && Date.now() - lastCheck < CHECK_EVERY_MS) return;
  checking = (async () => {
    try {
      const res = await fetch('/healthz', { cache: 'no-store' });
      if (!res.ok) return;
      const build = ((await res.json()) as { web_build?: string | null }).web_build;
      lastCheck = Date.now();
      newer = build && build !== BUILD ? build : null;
    } catch {
      /* offline: try again on the next resume */
    } finally {
      checking = null;
    }
  })();
  return checking;
}

/** The screen shown: the route without its query (search and filters keep what is typed in the query). */
const routePath = () => (window.location.hash.replace(/^#/, '') || '/').split('?')[0];
/** The screen the last route event was for. */
let shownPath = routePath();

function onHoldRoute(): boolean {
  const path = routePath();
  return HOLD_ROUTES.some((r) => path === r || path.startsWith(r + '/'));
}

const TEXT_INPUT = 'input:not([type]),input[type=text],input[type=url],input[type=email],input[type=tel],input[type=number],input[type=password]';

/** Something on screen that a reload would lose. */
function holdsState(): boolean {
  if (document.querySelector('[aria-modal="true"], [data-hold-update]')) return true;
  // a field being typed in (an empty one, like the autofocused search box, holds nothing)
  const active = document.activeElement as HTMLElement | null;
  if (active?.isContentEditable) return true;
  if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) {
    if (active.value.trim()) return true;
  }
  for (const el of document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>(`textarea,${TEXT_INPUT}`)) {
    if (el.value.trim()) return true;
  }
  for (const el of document.querySelectorAll<HTMLInputElement>('input[type=file]')) {
    if (el.files?.length) return true;
  }
  return onHoldRoute();
}

function canReload(): boolean {
  if (!newer || pendingOperations || listenBusy()) return false;
  try {
    if (sessionStorage.getItem(RELOADED_FOR) === newer) return false;
    sessionStorage.setItem(RELOADED_FOR, newer);
  } catch {
    /* no storage: reload anyway, the Hub serves the new build */
  }
  return true;
}

async function onResume() {
  if (document.visibilityState !== 'visible') return;
  await check(false);
  if (newer && !holdsState() && canReload()) window.location.reload();
}

function onRouteChange() {
  const path = routePath();
  // the same screen rewriting its query (typing in the search box, a filter) is not leaving it
  const left = path !== shownPath;
  shownPath = path;
  if (newer && left) {
    // the previous screen is gone: whatever it held is not lost by a reload
    if (!onHoldRoute() && canReload()) window.location.reload();
    return;
  }
  if (!newer) void check(true);
}

export function startUpdateWatcher() {
  if (BUILD === 'dev') return; // vite dev server / mock: no build to compare
  document.addEventListener('visibilitychange', () => void onResume());
  window.addEventListener(RESUME_EVENT, () => void onResume());
  window.addEventListener('hashchange', onRouteChange);
  window.addEventListener(ROUTE_EVENT, onRouteChange);
  void check(false);
}
