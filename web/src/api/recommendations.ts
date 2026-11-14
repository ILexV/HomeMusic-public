import { request, withQuery } from './client';

export interface QualityCohort {
  policy_version: string; attempts: number; eligible_plays: number; completed: number; early_skips: number;
  completion_rate: number | null; early_skip_rate: number | null;
}
export interface QualityWindow {
  since: string; until: string; attempts: number; eligible_plays: number; completed: number; early_skips: number;
  completion_rate: number | null; early_skip_rate: number | null; policy_cohorts: QualityCohort[];
}
export interface RecommendationQuality {
  days: number;
  since: string;
  until: string;
  metric_version: string;
  provenance: string;
  policy_cohorts: QualityCohort[];
  comparison: { current: QualityWindow; previous: QualityWindow; limitations: string[] };
  recordings: { metric_version: string; observations: number; known: number; full: number; not_full: number;
    unknown: number; rate: number | null; provenance: string[]; limitations: string[] };
  attempts: number;
  starts: number;
  eligible_plays: number;
  completed: number;
  early_skips: number;
  playback_errors: number;
  interrupted: number;
  new_track_plays: number;
  new_track_likes: number;
  repeats: number;
  completion_rate: number | null;
  early_skip_rate: number | null;
  new_track_like_rate: number | null;
  repeat_rate: number | null;
  sources: Array<{ id: string; title: string; attempts: number; completed: number; early_skips: number;
    playback_errors: number; new_track_likes: number; multiplier: number; learning_samples: number }>;
  limitations: string[];
}

export type PauseAction = 'today' | 'week' | 'less_artist';
export interface RecommendationPause {
  id: number;
  action: PauseAction;
  label: string;
  expires_at: string;
  ref: string | null;
  nd_song_id: string | null;
  artist: string | null;
}

export interface Release {
  ref: string;
  title: string;
  artist: string;
  artist_ref: string;
  cover_url: string | null;
  release_date: string;
  track_count: number | null;
  seen: boolean;
}
export type ReleasePeriod = 'week' | 'month';
export interface Releases {
  period: ReleasePeriod;
  generated_at: string;
  partial: boolean;
  errors: string[];
  items: Release[];
}

export const recommendationsApi = {
  currentRecording: (ref: string, signal?: AbortSignal) =>
    request<{ ref: string; binding: string }>('GET', withQuery('/api/recording-feedback/current', { ref }), { signal }),
  wrongRecording: (ref: string, binding: string, signal?: AbortSignal) =>
    request<{ id: number; ref: string; message: string }>('POST', '/api/recording-feedback', { json: { ref, binding }, signal }),
  undoRecording: (id: number, signal?: AbortSignal) =>
    request<void>('DELETE', `/api/recording-feedback/${id}`, { signal }),
  quality: (days: 7 | 30 | 90, signal?: AbortSignal) =>
    request<RecommendationQuality>('GET', withQuery('/api/recommendations/quality', { days }), { signal }),
  explain: (context: string, ref: string, nd_song_id?: string | null, signal?: AbortSignal) =>
    request<{ reasons: string[] }>('GET', withQuery('/api/recommendations/explain', { context, ref, nd_song_id }), { signal }),
  pauses: (signal?: AbortSignal) => request<{ items: RecommendationPause[] }>('GET', '/api/recommendations/pauses', { signal }),
  pause: (action: PauseAction, track: { ref?: string; nd_song_id?: string | null }, signal?: AbortSignal) =>
    request<RecommendationPause>('POST', '/api/recommendations/pauses', { json: { action, ref: track.ref, nd_song_id: track.nd_song_id }, signal }),
  unpause: (id: number, signal?: AbortSignal) => request<void>('DELETE', `/api/recommendations/pauses/${id}`, { signal }),
  releases: (period: ReleasePeriod, unseen: boolean, signal?: AbortSignal) =>
    request<Releases>('GET', withQuery('/api/releases', { period, unseen }), { signal }),
  seen: (ref: string, seen: boolean, signal?: AbortSignal) =>
    request<{ ref: string; seen: boolean }>('PUT', '/api/releases/seen', { json: { ref, seen }, signal }),
};
