import { request, withQuery } from './client';
import type { DownloadItemOut } from './types';

export type DownloadFilter = 'all' | 'active' | 'problems';

export interface DownloadCounts {
  all: number;
  active: number;
  problems: number;
  finished: number;
}

export interface DownloadPage {
  items: DownloadItemOut[];
  next_cursor: number | null;
  counts: DownloadCounts;
}

/** History is uncached and account-scoped; the legacy downloads endpoint serves other callers. */
export function downloadPage(filter: DownloadFilter, beforeId?: number, signal?: AbortSignal): Promise<DownloadPage> {
  return request('GET', withQuery('/api/downloads/page', { filter, limit: 50, before_id: beforeId }), { signal });
}
