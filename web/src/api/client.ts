import type {
  AdminUserDetailOut,
  AdminUsersOut,
  AlbumCreateIn,
  AlbumMergeIn,
  AlbumOut,
  AlbumPatchIn,
  ArtistPageOut,
  AssistantConfirmIn,
  AssistantTaskOut,
  ChatBriefOut,
  ChatConfirmOut,
  ChatMessage,
  ChatOut,
  ChatSavedOut,
  ClearOut,
  DislikeIn,
  DislikeOut,
  DownloadBatchOut,
  DownloadIn,
  DownloadItemOut,
  ImportIn,
  ImportItemsOut,
  ImportOut,
  ImportStartIn,
  InviteIn,
  InviteOut,
  LibAlbumOut,
  LibTrackOut,
  ListenOut,
  LoginIn,
  MeOut,
  MixOut,
  MixSettingsIn,
  MixSettingsOut,
  MixTodayOut,
  OkOut,
  PlayEventIn,
  PlayEventsOut,
  PlaylistAddIn,
  PlaylistOut,
  PlaylistPageOut,
  NdAlbumList,
  NdAlbumOut,
  NdArtistOut,
  NdArtistPageOut,
  NdHomeOut,
  NdLikedOut,
  ProviderSearchOut,
  RegisterIn,
  AuthOptionsOut,
  EmailCodeOut,
  EmailRegisterIn,
  RetryIn,
  SearchOut,
  SearchHistoryOut,
  SearchTracksOut,
  StarIn,
  StarOut,
  StarredOut,
  TasteOnboardingOut,
  TrackPatchIn,
  TracksMoveIn,
  UploadCommitIn,
  UploadCommitOut,
  UploadOut,
  SmartAddOut,
  SmartNextIn,
  SmartOut,
  SmartStartIn,
  WaveNextIn,
  WaveOut,
  WaveStartIn,
} from './types';

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = 'ApiError';
  }
}

/** Fired when the server answers 401: the app switches to the login screen. */
export const UNAUTHORIZED_EVENT = 'hm:unauthorized';

function onUnauthorized(path: string) {
  if (!path.startsWith('/api/auth/login') && !path.startsWith('/api/auth/register') && !path.startsWith('/api/auth/email/')) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
}

function detailToMessage(detail: unknown, status: number): string {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc, msg, type}]
    const msgs = detail
      .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : ''))
      .filter(Boolean);
    if (msgs.length) return `Неверные данные: ${msgs.join('; ')}`;
  }
  if (status === 401) return 'Требуется вход';
  if (status === 403) return 'Нет доступа';
  if (status === 404) return 'Не найдено';
  if (status === 413) return 'Файл слишком большой';
  if (status === 429) return 'Слишком много запросов, попробуйте позже';
  if (status >= 500) return 'Ошибка сервера';
  return `Ошибка ${status}`;
}

async function errorFromResponse(res: Response): Promise<ApiError> {
  let detail: unknown;
  try {
    detail = ((await res.json()) as { detail?: unknown }).detail;
  } catch {
    /* not JSON */
  }
  return new ApiError(res.status, detailToMessage(detail, res.status));
}

type Query = Record<string, string | number | boolean | null | undefined>;

export function withQuery(path: string, query?: Query): string {
  if (!query) return path;
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null && v !== '') p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `${path}?${s}` : path;
}

interface RequestOpts {
  json?: unknown;
  form?: FormData;
  signal?: AbortSignal;
}

export async function request<T>(method: string, path: string, opts: RequestOpts = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  let body: BodyInit | undefined;
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.json);
  } else if (opts.form) {
    body = opts.form;
  }
  let res: Response;
  try {
    res = await fetch(path, { method, headers, body, credentials: 'include', signal: opts.signal });
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e;
    throw new ApiError(0, 'Нет связи с сервером');
  }
  if (res.status === 401) onUnauthorized(path);
  if (!res.ok) throw await errorFromResponse(res);
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get('content-type') ?? '';
  if (!ct.includes('json')) return undefined as T;
  return (await res.json()) as T;
}

// Short-lived cache for GET pages (back navigation feels instant).
const cache = new Map<string, { at: number; data: unknown }>();
const CACHE_TTL = 3 * 60 * 1000;

export async function cachedGet<T>(path: string, signal?: AbortSignal, fresh = false): Promise<T> {
  const hit = cache.get(path);
  if (!fresh && hit && Date.now() - hit.at < CACHE_TTL) return hit.data as T;
  const data = await request<T>('GET', path, { signal });
  cache.set(path, { at: Date.now(), data });
  return data;
}

export function peekCache<T>(path: string): T | undefined {
  const hit = cache.get(path);
  return hit && Date.now() - hit.at < CACHE_TTL ? (hit.data as T) : undefined;
}

export function invalidateCache(prefix = '') {
  for (const key of cache.keys()) if (key.startsWith(prefix)) cache.delete(key);
}

/** multipart POST via XMLHttpRequest to get upload progress (0..1). */
export function uploadWithProgress<T>(
  url: string,
  form: FormData,
  onProgress?: (fraction: number) => void,
  signal?: AbortSignal,
  method = 'POST',
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let data: unknown;
      try {
        data = xhr.responseText ? JSON.parse(xhr.responseText) : undefined;
      } catch {
        data = undefined;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data as T);
      } else {
        if (xhr.status === 401) onUnauthorized(url);
        const detail = data && typeof data === 'object' ? (data as { detail?: unknown }).detail : undefined;
        reject(new ApiError(xhr.status, detailToMessage(detail, xhr.status)));
      }
    };
    xhr.onerror = () => reject(new ApiError(0, 'Нет связи с сервером'));
    xhr.onabort = () => reject(new DOMException('Aborted', 'AbortError'));
    signal?.addEventListener('abort', () => xhr.abort());
    xhr.send(form);
  });
}

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message || 'Неизвестная ошибка';
  return 'Неизвестная ошибка';
}

export const isAbort = (e: unknown) => e instanceof DOMException && e.name === 'AbortError';

const enc = encodeURIComponent;

// --- typed endpoints ------------------------------------------------------------------------------

export const api = {
  // auth
  login: (body: LoginIn) => request<MeOut>('POST', '/api/auth/login', { json: body }),
  logout: () => request<OkOut>('POST', '/api/auth/logout'),
  me: (signal?: AbortSignal) => request<MeOut>('GET', '/api/auth/me', { signal }),
  register: (body: RegisterIn) => request<MeOut>('POST', '/api/auth/register', { json: body }),
  authOptions: (signal?: AbortSignal) => request<AuthOptionsOut>('GET', '/api/auth/options', { signal }),
  emailCode: (body: { email: string }) => request<EmailCodeOut>('POST', '/api/auth/email/code', { json: body }),
  emailRegister: (body: EmailRegisterIn) => request<MeOut>('POST', '/api/auth/email/register', { json: body }),
  unlinkGoogle: () => request<MeOut>('DELETE', '/api/auth/google/link'),

  // admin
  adminUsers: (signal?: AbortSignal) => request<AdminUsersOut>('GET', '/api/admin/users', { signal }),
  adminUser: (id: number, signal?: AbortSignal) => request<AdminUserDetailOut>('GET', `/api/admin/users/${id}`, { signal }),
  invites: (signal?: AbortSignal) => request<InviteOut[]>('GET', '/api/admin/invites', { signal }),
  createInvite: (body: InviteIn) => request<InviteOut>('POST', '/api/admin/invites', { json: body }),
  deleteInvite: (id: number) => request<OkOut>('DELETE', `/api/admin/invites/${id}`),

  // search / catalog
  search: (q: string, signal?: AbortSignal) => cachedGet<SearchOut>(withQuery('/api/search', { q }), signal),
  searchHistory: (signal?: AbortSignal) => request<SearchHistoryOut>('GET', '/api/search/history', { signal }),
  rememberSearch: (query: string) => request<OkOut>('POST', '/api/search/history', { json: { query } }),
  clearSearchHistory: () => request<OkOut>('DELETE', '/api/search/history'),
  // more tracks of the same search, from `offset` (`SearchOut.tracks_next`, then `next_offset`)
  searchTracks: (q: string, offset: number, signal?: AbortSignal) =>
    cachedGet<SearchTracksOut>(withQuery('/api/search/tracks', { q, offset }), signal),
  /** `group`: only the regular sources, or only re-uploads by small channels (slower). */
  searchProviders: (q: string, signal?: AbortSignal, group?: 'sources' | 'reuploads') =>
    cachedGet<ProviderSearchOut>(withQuery('/api/search/providers', { q, group }), signal),
  artist: (ref: string, signal?: AbortSignal, fresh = false) =>
    cachedGet<ArtistPageOut>(`/api/catalog/artist/${enc(ref)}`, signal, fresh),
  album: (ref: string, signal?: AbortSignal, fresh = false) =>
    cachedGet<AlbumOut>(`/api/catalog/album/${enc(ref)}`, signal, fresh),

  // listening before download: picks the file (may take seconds), `stream_url` plays it
  listen: (ref: string, signal?: AbortSignal, full = false) => request<ListenOut>('GET', withQuery('/api/listen', { ref, full: full ? 1 : undefined }), { signal }),
  // the catalog tracks on screen, top first: the Hub picks their files in the background (replaces the last list)
  listenPrefetch: (refs: string[]) => request<{ queued: number }>('POST', '/api/listen/prefetch', { json: { refs } }),

  // downloads
  download: (body: DownloadIn, signal?: AbortSignal) => request<DownloadBatchOut>('POST', '/api/downloads', { json: body, signal }),
  downloads: (status?: string, limit = 100, signal?: AbortSignal) =>
    request<DownloadItemOut[]>('GET', withQuery('/api/downloads', { status, limit }), { signal }),
  downloadItem: (id: number, signal?: AbortSignal) =>
    request<DownloadItemOut>('GET', `/api/downloads/${id}`, { signal }),
  retry: (id: number, body: RetryIn = {}) =>
    request<DownloadItemOut>('POST', `/api/downloads/items/${id}/retry`, { json: body }),
  cancel: (id: number) => request<DownloadItemOut>('POST', `/api/downloads/items/${id}/cancel`),
  clearDownloads: () => request<ClearOut>('POST', '/api/downloads/clear'),

  // library
  libAlbums: (q: string, offset = 0, limit = 50, signal?: AbortSignal) =>
    request<LibAlbumOut[]>('GET', withQuery('/api/library/albums', { q, offset, limit }), { signal }),
  libAlbum: (id: number, signal?: AbortSignal) => request<LibAlbumOut>('GET', `/api/library/albums/${id}`, { signal }),
  createAlbum: (body: AlbumCreateIn) => request<LibAlbumOut>('POST', '/api/library/albums', { json: body }),
  patchAlbum: (id: number, body: AlbumPatchIn) =>
    request<LibAlbumOut>('PATCH', `/api/library/albums/${id}`, { json: body }),
  setAlbumCover: (id: number, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<LibAlbumOut>('PUT', `/api/library/albums/${id}/cover`, { form });
  },
  mergeAlbums: (body: AlbumMergeIn) => request<LibAlbumOut>('POST', '/api/library/albums/merge', { json: body }),
  moveTracks: (body: TracksMoveIn) => request<LibAlbumOut>('POST', '/api/library/tracks/move', { json: body }),
  patchTrack: (id: number, body: TrackPatchIn) =>
    request<LibTrackOut>('PATCH', `/api/library/tracks/${id}`, { json: body }),
  deleteTrack: (id: number) => request<OkOut>('DELETE', `/api/library/tracks/${id}`),
  star: (body: StarIn, signal?: AbortSignal) => request<StarOut>('POST', '/api/star', { json: body, signal }),
  starred: (signal?: AbortSignal) => request<StarredOut>('GET', '/api/starred', { signal }),

  // listening data for mixes: player events, «Не нравится»
  playEvents: (events: PlayEventIn[], expectedUserId?: number) => request<PlayEventsOut>('POST', '/api/plays/events', { json: { events, expected_user_id: expectedUserId } }),
  dislikes: (signal?: AbortSignal) => request<DislikeOut[]>('GET', '/api/dislikes', { signal }),
  dislike: (body: DislikeIn) => request<DislikeOut>('POST', '/api/dislikes', { json: body }),
  undislike: (q: { ref?: string | null; nd_song_id?: string | null; artist_ref?: string | null }) =>
    request<{ removed: number }>('DELETE', withQuery('/api/dislikes', q)),

  // «Микс дня» and the taste onboarding
  mixToday: (signal?: AbortSignal) => request<MixTodayOut>('GET', '/api/mixes/today', { signal }),
  mix: (id: number, signal?: AbortSignal) => request<MixOut>('GET', `/api/mixes/${id}`, { signal }),
  mixSettings: (body: MixSettingsIn) => request<MixSettingsOut>('PUT', '/api/mixes/settings', { json: body }),
  rebuildMix: () => request<{ queued: boolean }>('POST', '/api/mixes/rebuild', { json: {} }),
  tasteOnboarding: (q?: string, signal?: AbortSignal) =>
    request<TasteOnboardingOut>('GET', withQuery('/api/taste/onboarding', { q }), { signal }),
  pickArtists: (artist_refs: string[]) => request<MixSettingsOut>('POST', '/api/taste/artists', { json: { artist_refs } }),

  // «Моя волна»
  smartStart: (body: SmartStartIn) => request<SmartOut>('POST', '/api/smart-shuffle/start', { json: body }),
  smartNext: (id: string, body: SmartNextIn) => request<SmartOut>('POST', `/api/smart-shuffle/${encodeURIComponent(id)}/next`, { json: body }),
  smartHide: (id: string, body: { ref?: string | null; nd_song_id?: string | null }) =>
    request<{ ok: boolean }>('POST', `/api/smart-shuffle/${encodeURIComponent(id)}/hide`, { json: body }),
  smartAdd: (id: string, body: { ref?: string | null; nd_song_id?: string | null }) =>
    request<SmartAddOut>('POST', `/api/smart-shuffle/${encodeURIComponent(id)}/add`, { json: body }),
  waveStart: (body: WaveStartIn) => request<WaveOut>('POST', '/api/wave/start', { json: body }),
  waveNext: (id: string, body: WaveNextIn) => request<WaveOut>('POST', `/api/wave/${encodeURIComponent(id)}/next`, { json: body }),

  // uploads
  uploads: (status = 'uploaded', signal?: AbortSignal) =>
    request<UploadOut[]>('GET', withQuery('/api/uploads', { status }), { signal }),
  uploadFiles: (files: File[], onProgress?: (f: number) => void, signal?: AbortSignal) => {
    const form = new FormData();
    for (const f of files) form.append('files', f, f.name);
    return uploadWithProgress<UploadOut[]>('/api/uploads', form, onProgress, signal);
  },
  deleteUpload: (id: number) => request<OkOut>('DELETE', `/api/uploads/${id}`),
  commitUploads: (payload: UploadCommitIn, cover?: File | null) => {
    const form = new FormData();
    form.append('payload', JSON.stringify(payload));
    if (cover) form.append('cover', cover, cover.name);
    return request<UploadCommitOut>('POST', '/api/uploads/commit', { form });
  },

  // playlists
  playlists: (signal?: AbortSignal) => request<PlaylistOut[]>('GET', '/api/playlists', { signal }),
  createPlaylist: (name: string) => request<PlaylistOut>('POST', '/api/playlists', { json: { name } }),
  addToPlaylist: (id: string, body: PlaylistAddIn) =>
    request<OkOut>('POST', `/api/playlists/${enc(id)}/add`, { json: body }),
  playlist: (id: string, signal?: AbortSignal) => request<PlaylistPageOut>('GET', `/api/playlists/${enc(id)}`, { signal }),
  removeFromPlaylist: (id: string, indexes: number[]) =>
    request<OkOut>('POST', `/api/playlists/${enc(id)}/remove`, { json: { indexes } }),
  deletePlaylist: (id: string) => request<OkOut>('DELETE', `/api/playlists/${enc(id)}`),

  // the Navidrome library (a browser shows it; the app has it natively)
  ndHome: (signal?: AbortSignal) => request<NdHomeOut>('GET', '/api/nd/home', { signal }),
  ndLiked: (signal?: AbortSignal) => request<NdLikedOut>('GET', '/api/nd/liked', { signal }),
  ndArtists: (signal?: AbortSignal) => cachedGet<NdArtistOut[]>('/api/nd/artists', signal),
  ndArtist: (id: string, signal?: AbortSignal) => request<NdArtistPageOut>('GET', `/api/nd/artists/${enc(id)}`, { signal }),
  ndAlbum: (id: string, signal?: AbortSignal) => request<NdAlbumOut>('GET', `/api/nd/albums/${enc(id)}`, { signal }),
  ndAlbums: (type: NdAlbumList, offset = 0, size = 50, signal?: AbortSignal) =>
    request<NdAlbumOut[]>('GET', withQuery('/api/nd/albums', { type, offset, size }), { signal }),

  // imports from music services
  imports: (signal?: AbortSignal) => request<ImportOut[]>('GET', '/api/imports', { signal }),
  createImport: (body: ImportIn) => request<ImportOut>('POST', '/api/imports', { json: body }),
  importGet: (id: number, signal?: AbortSignal) => request<ImportOut>('GET', `/api/imports/${id}`, { signal }),
  importItems: (id: number, q: { collection_id?: number; status?: string; match?: string; offset?: number; limit?: number }, signal?: AbortSignal) =>
    request<ImportItemsOut>('GET', withQuery(`/api/imports/${id}/items`, q), { signal }),
  importStart: (id: number, body: ImportStartIn) => request<ImportOut>('POST', `/api/imports/${id}/start`, { json: body }),
  importDownloadAll: (id: number) => request<ImportOut>('POST', `/api/imports/${id}/download_all`),
  importSync: (id: number) => request<ImportOut>('POST', `/api/imports/${id}/sync`),
  importForgetToken: (id: number) => request<ImportOut>('DELETE', `/api/imports/${id}/token`),
  importCancel: (id: number) => request<ImportOut>('POST', `/api/imports/${id}/cancel`),
  importDelete: (id: number) => request<OkOut>('DELETE', `/api/imports/${id}`),
  importPick: (itemId: number) => request<{ download_item_id: number }>('POST', `/api/imports/items/${itemId}/pick`),
  importSkip: (itemId: number) => request<OkOut>('POST', `/api/imports/items/${itemId}/skip`),

  // assistant
  assistantTask: (id: number, signal?: AbortSignal) =>
    request<AssistantTaskOut>('GET', `/api/assistant/tasks/${id}`, { signal }),
  assistantConfirm: (id: number, body: AssistantConfirmIn) =>
    request<AssistantTaskOut>('POST', `/api/assistant/tasks/${id}/confirm`, { json: body }),
  chat: (messages: ChatMessage[], chatId: number | null, images: Blob[] = [], signal?: AbortSignal) => {
    if (!images.length) return request<ChatOut>('POST', '/api/assistant/chat', { json: { messages, chat_id: chatId }, signal });
    const form = new FormData();
    form.append('messages', JSON.stringify(messages));
    if (chatId) form.append('chat_id', String(chatId));
    images.forEach((img, i) => form.append('images', img, img instanceof File ? img.name : `screenshot-${i + 1}.png`));
    return request<ChatOut>('POST', '/api/assistant/chat', { form, signal });
  },
  chatConfirm: (taskId: number, confirm_token: string, chatId: number | null) =>
    request<ChatConfirmOut>('POST', `/api/assistant/chat/${taskId}/confirm`, { json: { confirm_token, chat_id: chatId } }),
  chatDismiss: (taskId: number, chatId: number | null) =>
    request<OkOut>('POST', `/api/assistant/chat/${taskId}/dismiss`, { json: { chat_id: chatId } }),
  chats: (signal?: AbortSignal) => request<ChatBriefOut[]>('GET', '/api/assistant/chats', { signal }),
  savedChat: (id: number, signal?: AbortSignal) => request<ChatSavedOut>('GET', `/api/assistant/chats/${id}`, { signal }),
  deleteChat: (id: number) => request<OkOut>('DELETE', `/api/assistant/chats/${id}`),
};

export const libraryCoverUrl = (albumId: number, size = 300, bust?: string | number) =>
  withQuery(`/api/library/albums/${albumId}/cover`, { size, v: bust });

/** Audio of a catalog track for `<audio src>`; the Hub picks the file on the first request if needed. */
export const listenStreamUrl = (ref: string, full = false) => withQuery('/api/listen/stream', { ref, full: full ? 1 : undefined });

export const uploadCoverUrl = (uploadId: number) => `/api/uploads/${uploadId}/cover`;
