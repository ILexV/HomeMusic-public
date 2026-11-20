import { useEffect, useRef, type ReactNode } from 'react';
import type { Candidate } from '../api/types';
import { fmtDate, fmtDuration, providerLabel } from '../lib/format';
import { listen, touchTrack, useListen, watchOnScreen, type ListenTrack } from '../lib/listen';
import { canPlayAll, playHubItems, type HubItem } from '../lib/player';
import { ListenButton } from './catalog';
import { Icon } from './Icon';
import { LikeButton } from './LikeButton';
import { TrackMenuButton } from './TrackMenu';
import { Cover, ExplicitBadge, cx } from './ui';

/** A provider's file named like a catalog track: the Hub streams it (`/api/listen`) and downloads it by this ref.
 * Only files the Hub has just shown in a search (`/api/search/providers`) are known to it. */
export const fileRef = (c: Candidate) => `file:${c.provider}:${c.provider_id}`;

const fileTrack = (c: Candidate): ListenTrack & HubItem => ({
  ref: fileRef(c),
  title: c.title,
  artist: c.artist,
  album: c.album ?? undefined,
  cover_url: c.cover_url,
  duration_s: c.duration_s,
});

/** Plays the files of one search from `c` on: in the app's player (1.2.0+), else in the page. */
export function listenFiles(list: readonly Candidate[], c: Candidate) {
  const at = list.indexOf(c);
  const tracks = at >= 0 ? list.map(fileTrack) : [fileTrack(c)];
  const i = Math.max(0, at);
  if (canPlayAll()) playHubItems(tracks, i);
  else listen(tracks[i], tracks);
}

/** Uploads before this date predate the law of 2026-03-01 that got tracks edited (PLAN 3.2). */
const CENSORSHIP_LAW = '2026-03-01';

/** What a file says about censorship: its re-uploader's word, else its upload date (as the Hub's
 * `trusted_original`: an official YouTube Music copy doesn't count by date, labels replace its audio in place). */
function censorship(c: Candidate): { text: string; tone: 'good' | 'bad' } | null {
  const marker = c.extra?.uncensored;
  if (marker === true) return { text: 'без цензуры', tone: 'good' };
  if (marker === false) return { text: 'с цензурой', tone: 'bad' };
  const old = c.upload_date && c.upload_date < CENSORSHIP_LAW;
  if (old && c.explicit !== false && c.extra?.source !== 'ytmusic') return { text: 'до 01.03.2026', tone: 'good' };
  return null;
}

/** A raw downloadable file from a provider (hitmoz / hitmos.fm / YouTube / a re-upload …). With `queue` (the
 * search's files it is among) it can be listened to: ▷ plays the list from it on, and while the row is on screen
 * the Hub opens the file in the background (`watchOnScreen`), so the tap starts at once. ♥ downloads that very file
 * and stars it after the import, like ♥ on a catalog track. */
export function CandidateRow({ c, action, queue }: { c: Candidate; action?: ReactNode; queue?: readonly Candidate[] }) {
  const ref = fileRef(c);
  const listenable = !!queue;
  const rowRef = useRef<HTMLDivElement>(null);
  const current = useListen((s) => listenable && s.track?.ref === ref);
  useEffect(() => (rowRef.current && listenable ? watchOnScreen(rowRef.current, ref) : undefined), [ref, listenable]);
  const str = (k: string) => (typeof c.extra?.[k] === 'string' ? (c.extra[k] as string) : null);
  // hitmoz's full title with the version; a re-upload's own title ("Трек – Исполнитель (без цензуры)")
  const version = str('full_title') ?? str('original_title');
  const channel = c.extra?.reupload ? str('channel') : null;
  const mark = censorship(c);
  const facts = [
    c.bitrate_kbps ? `${c.bitrate_kbps} kbps` : null,
    fmtDuration(c.duration_s) || null,
    c.upload_date ? `загружен ${fmtDate(c.upload_date)}` : null,
  ].filter(Boolean);
  return (
    <div ref={rowRef} className={cx('flex gap-3 py-3 pl-4', listenable ? 'pr-1' : 'pr-4')}>
      <Cover src={c.cover_url} size={52} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          {c.explicit && <ExplicitBadge />}
          <span className={cx('truncate text-[15px]', current && 'text-accent')}>{c.title}</span>
        </div>
        <div className="truncate text-[13px] text-muted">{[c.artist, c.album, c.year].filter(Boolean).join(' · ')}</div>
        {version && version !== c.title && <div className="truncate text-xs text-dim">{version}</div>}
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
          <span className="rounded bg-fg/8 px-1.5 py-0.5 font-medium text-fg">{providerLabel(c.provider)}</span>
          {mark && (
            <span className={cx('rounded px-1.5 py-0.5 font-medium', mark.tone === 'good' ? 'bg-accent/15 text-accent' : 'bg-warn/15 text-warn')}>
              {mark.text}
            </span>
          )}
          {channel && <span className="max-w-full truncate">канал {channel}</span>}
          {facts.map((f) => (
            <span key={f as string}>{f}</span>
          ))}
          {c.score ? <span className="text-dim">совпадение {Math.round(c.score * 100)}%</span> : null}
          {c.page_url && (
            <a href={c.page_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-0.5 text-dim underline-offset-2 hover:underline" aria-label="Открыть страницу источника">
              источник <Icon name="external" size={12} />
            </a>
          )}
        </div>
        {action && <div className="mt-2">{action}</div>}
      </div>
      {queue && (
        <div className="flex shrink-0 items-center self-center">
          <LikeButton refId={ref} title={c.title} className="w-10" />
          <span onPointerDown={() => touchTrack(ref)}>
            <ListenButton track={{ ref, title: c.title }} onListen={() => listenFiles(queue, c)} />
          </span>
          <TrackMenuButton track={{ ...fileTrack(c), listen: fileTrack(c) }} className="w-8" />
        </div>
      )}
    </div>
  );
}
