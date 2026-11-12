import { request } from './client';

export interface PlaybackTrack {
  ref: string;
  nd_song_id: string | null;
  title: string;
  artist: string;
  album: string;
  duration_s: number;
}

export interface PlaybackSnapshot {
  queue: PlaybackTrack[];
  index: number;
  occurrence: string;
  position: number;
  playing: boolean;
  volume: number;
  repeat: 'off' | 'all' | 'one';
  shuffle: boolean;
}

export type PlaybackControl = 'play' | 'pause' | 'next' | 'prev' | 'seek' | 'volume';
export interface PlaybackCommand {
  id: string;
  kind: PlaybackControl | 'prepare' | 'stop' | 'start';
  expires_at: number;
  state?: PlaybackSnapshot;
  value?: number;
  handoff_id?: string;
}

export interface PlaybackView {
  device_id: string;
  devices: { id: string; name: string; kind: 'desktop' | 'web'; last_seen: number; online: boolean }[];
  owner: string | null;
  revision: number;
  state: PlaybackSnapshot | null;
  handoff: { id: string; source: string; target: string; phase: string; error?: string | null; recovery?: boolean } | null;
  commands: PlaybackCommand[];
  error: string | null;
}

export interface PlaybackAck {
  device_id: string;
  command_id: string;
  ok: boolean;
  error?: string;
  state?: PlaybackSnapshot;
}

/** Same cookie authentication as the rest of the Hub; never store resolved media URLs. */
export async function playbackRequest(path: 'heartbeat' | 'claim' | 'handoff' | 'command' | 'ack', body: unknown, signal: AbortSignal): Promise<PlaybackView> {
  const ctl = new AbortController();
  const abort = () => ctl.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) ctl.abort();
  const timer = setTimeout(abort, 8000);
  try {
    return await request<PlaybackView>('POST', `/api/playback/${path}`, { json: body, signal: ctl.signal });
  } finally {
    clearTimeout(timer);
    signal.removeEventListener('abort', abort);
  }
}
