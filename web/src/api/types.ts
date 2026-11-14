// Mirrors hub/app/api/schemas.py, hub/app/catalog/models.py and hub/app/providers/base.py.
// Dates arrive as ISO strings ("YYYY-MM-DD" for date, ISO 8601 for datetime).

export type ISODate = string;
export type ISODateTime = string;

// --- auth ---------------------------------------------------------------------------------

export interface LoginIn {
  username: string;
  password: string;
}

export interface MeOut {
  id: number;
  username: string;
  is_admin: boolean;
  public_music_url: string;
  public_hub_url?: string;
  google_email?: string | null;
  email?: string | null;
}

export interface RegisterIn {
  code: string;
  username: string;
  password: string;
}

export interface AuthOptionsOut {
  invites: boolean;
  email_signup?: boolean;
  google_client_id: string | null;
  music_url?: string;
}

export interface EmailCodeOut {
  resend_in: number;
}

export interface EmailRegisterIn extends RegisterIn {
  email: string;
}

// --- admin ---------------------------------------------------------------------------------

export interface InviteIn {
  note: string;
  days?: number | null;
}

export interface InviteOut {
  id: number;
  code: string;
  note: string;
  status: 'active' | 'used' | 'expired';
  created_at: string;
  expires_at: string;
  used_at: string | null;
  used_by: string | null;
}

export interface AdminUserOut {
  id: number | null;
  username: string;
  name: string;
  is_admin: boolean;
  in_hub: boolean;
  created_at: string | null;
  last_seen_at: string | null;
  player_seen_at: string | null;
  google_email: string | null;
  email?: string | null;
  invite_note: string | null;
  ai_cost_usd: number;
  ai_cost_30d_usd: number;
  ai_requests: number;
  ai_last_at: string | null;
  downloads_done: number;
  downloads_failed: number;
  downloads_active: number;
  tracks_added: number;
  uploads: number;
  starred_songs: number | null;
  playlists: number | null;
}

export interface AdminUsersOut {
  users: AdminUserOut[];
  ai_cost_usd: number;
  ai_cost_30d_usd: number;
  library_tracks: number;
}

export interface AdminUserDetailOut extends AdminUserOut {
  ai_months: Array<{ month: string; cost_usd: number; requests: number }>;
  ai_recent: Array<{ kind: string; tokens_in: number; tokens_out: number; cost_usd: number; created_at: string }>;
  recent_downloads: Array<{ artist: string; title: string; status: string; created_at: string }>;
}

export interface OkOut {
  ok: boolean;
}

// --- providers ------------------------------------------------------------------------------

export interface Candidate {
  provider: string;
  provider_id: string;
  artist: string;
  title: string;
  duration_s?: number | null;
  bitrate_kbps?: number | null;
  explicit?: boolean | null;
  upload_date?: ISODate | null;
  page_url: string;
  download_url?: string | null;
  cover_url?: string | null;
  album?: string | null;
  year?: number | null;
  score?: number;
  enriched?: boolean;
  extra?: Record<string, unknown>;
}

// --- catalog ----------------------------------------------------------------------------------

/** `nd`: a library-only song; `file`: an exact search file used as a wave seed. */
export type CatalogSource = 'dz' | 'hm' | 'mb' | 'nd' | 'file';
export type RecordType = 'album' | 'single' | 'ep' | 'compile';
export type OwnedStatus = 'full' | 'partial' | 'none';

export interface CatalogArtist {
  ref: string;
  source: CatalogSource;
  id: string;
  name: string;
  picture_url?: string | null;
  /** for lists (Deezer 250 px); the big one only on the artist's own screen */
  picture_small_url?: string | null;
  nb_albums?: number | null;
  nb_fans?: number | null;
}

export interface CatalogTrack {
  ref: string;
  source: CatalogSource;
  id: string;
  title: string;
  artist: string;
  artists?: string[];
  artist_ref?: string | null;
  album_ref?: string | null;
  album_title?: string | null;
  album_artist?: string | null;
  duration_s?: number | null;
  track_no?: number | null;
  disc_no?: number | null;
  isrc?: string | null;
  explicit?: boolean | null;
  release_date?: ISODate | null;
  cover_url?: string | null;
  /** for lists (Deezer 250 px): the big `cover_url` only on a screen of the track or album itself */
  cover_small_url?: string | null;
  preview_url?: string | null;
}

export interface CatalogAlbum {
  ref: string;
  source: CatalogSource;
  id: string;
  title: string;
  artist: string;
  artist_ref?: string | null;
  record_type: RecordType;
  release_date?: ISODate | null;
  year?: number | null;
  cover_url?: string | null;
  cover_small_url?: string | null;
  nb_tracks?: number | null;
  explicit?: boolean | null;
  upc?: string | null;
  genre?: string | null;
}

export interface TrackOut extends CatalogTrack {
  owned: boolean;
  library_track_id?: number | null;
  nd_song_id?: string | null;
  /** a download of it is under way (not counting ones queued only for ♥) */
  downloading: boolean;
  /** the user ♥ it and a download that stars it is under way: the ♥ shows filled */
  star_pending?: boolean;
}

export interface AlbumOut extends CatalogAlbum {
  owned_status: OwnedStatus;
  owned_count: number;
  tracks?: TrackOut[] | null;
}

export interface ArtistPageOut {
  artist: CatalogArtist;
  top_tracks: TrackOut[];
  albums: AlbumOut[];
  singles: AlbumOut[];
  compilations: AlbumOut[];
}

// --- library ----------------------------------------------------------------------------------

export type ReleaseType = 'album' | 'single' | 'ep' | 'compilation';

export interface LibTrackOut {
  id: number;
  nd_song_id: string | null;
  album_id: number | null;
  artist: string;
  title: string;
  album: string | null;
  album_artist: string | null;
  track_no: number | null;
  disc_no: number | null;
  year: number | null;
  duration_s: number | null;
  bitrate_kbps: number | null;
  codec: string | null;
  explicit: boolean | null;
  source: string | null;
  locked: boolean;
  warnings: unknown[];
  path: string;
}

export interface LibAlbumOut {
  id: number;
  uid: string;
  title: string;
  album_artist: string;
  year: number | null;
  release_type: string | null;
  genre: string | null;
  deezer_album_id: string | null;
  track_count: number;
  has_cover: boolean;
  nd_album_id?: string | null;
  tracks?: LibTrackOut[] | null;
}

export interface AlbumPatchIn {
  title?: string | null;
  album_artist?: string | null;
  year?: number | null;
  genre?: string | null;
  release_type?: ReleaseType | null;
}

export interface AlbumCreateIn {
  title: string;
  album_artist: string;
  year?: number | null;
  genre?: string | null;
  release_type?: ReleaseType | null;
}

export interface AlbumMergeIn {
  source_ids: number[];
  target_id: number;
}

export interface TracksMoveIn {
  track_ids: number[];
  album_id: number;
}

export interface TrackPatchIn {
  title?: string | null;
  artist?: string | null;
  track_no?: number | null;
  disc_no?: number | null;
  genre?: string | null;
  explicit?: boolean | null;
}

/** GET /api/starred: own typed decisions plus Navidrome/import snapshot fallbacks. */
export interface StarredOut {
  song_ids: string[];
  album_ids: string[];
  /** Own and imported pending track refs. Own subjects override this snapshot. */
  refs?: string[];
  artist_ids?: string[];
  subjects?: FavoriteSubject[];
}

export type FavoriteType = 'track' | 'album' | 'artist';

export interface FavoriteTarget {
  target_type: FavoriteType;
  ref?: string | null;
  nd_song_id?: string | null;
  nd_album_id?: string | null;
  nd_artist_id?: string | null;
  target_data?: Record<string, unknown> | null;
}

export interface FavoriteSubject {
  target_type: FavoriteType;
  key: string;
  ref?: string | null;
  nd_id?: string | null;
  liked: boolean;
  liked_since?: ISODate | null;
  event_id?: string | null;
  aliases: string[];
  target_data?: Record<string, unknown> | null;
  provenance: 'own' | 'import';
}

export interface StarOut extends OkOut {
  event_ids?: string[];
}

export interface StarIn {
  targets?: FavoriteTarget[];
  event_id?: string;
  ts?: ISODate;
  client?: string;
  context?: string;
  song_ids?: string[];
  album_ids?: string[];
  artist_ids?: string[];
  catalog_album_ref?: string | null;
  /** tracks of `StarredOut.refs`: only with `unstar` */
  refs?: string[];
  unstar?: boolean;
}

// --- search -----------------------------------------------------------------------------------

export interface SearchHistoryOut {
  queries: string[];
}

export interface SearchOut {
  library: LibTrackOut[];
  artists: CatalogArtist[];
  albums: AlbumOut[];
  tracks: TrackOut[];
  /** offset of the next page of tracks (`api.searchTracks`); null — that's all */
  tracks_next?: number | null;
  errors: string[];
}

export interface SearchTracksOut {
  tracks: TrackOut[];
  next_offset?: number | null;
}

export interface ProviderSearchOut {
  candidates: Candidate[];
  errors: string[];
}

// --- downloads --------------------------------------------------------------------------------

export type DownloadStatus =
  | 'queued'
  | 'searching'
  | 'downloading'
  | 'tagging'
  | 'importing'
  | 'done'
  | 'failed'
  | 'needs_review'
  | 'cancelled';

export interface DownloadTrackIn {
  artist: string;
  title: string;
  album?: string | null;
  duration_s?: number | null;
  isrc?: string | null;
  explicit?: boolean | null;
  version?: string | null;
  catalog_ref?: string | null;
  file_ref?: string | null;
}

export interface DownloadIn {
  items?: DownloadTrackIn[];
  track_refs?: string[];
  album_ref?: string | null;
  artist_ref?: string | null;
  record_types?: string[];
  playlist_id?: string | null;
  new_playlist_name?: string | null;
  force?: boolean;
  /** ♥ before download: the Hub stars the songs in Navidrome once they are imported */
  star?: boolean;
}

/** GET /api/listen: what plays for a catalog track before it is downloaded. */
export interface ListenOut {
  ref: string;
  /** full: the file a download would take; preview: Deezer's 30 s preview (no source has it); library: own file */
  kind: 'full' | 'preview' | 'library';
  artist: string;
  title: string;
  duration_s?: number | null;
  provider?: string | null;
  bitrate_kbps?: number | null;
  page_url?: string | null;
  /** library: the Navidrome song (♥), once Navidrome has scanned the file */
  nd_song_id?: string | null;
  /** audio with Range support (same session cookie) */
  stream_url: string;
  /** Server-bound identity used to report the actual recording, never a client-supplied URL. */
  recording_binding?: string | null;
}

export interface DownloadBatchOut {
  batch_id: number;
  queued: number;
  already_owned: number;
  playlist_id?: string | null;
  /** catalog refs are expanded in the background; `queued` counts only explicit items */
  expanding?: boolean;
}

export interface DownloadItemOut {
  id: number;
  batch_id: number;
  status: DownloadStatus | string;
  progress: number;
  artist: string;
  title: string;
  album?: string | null;
  provider?: string | null;
  error?: string | null;
  warnings: unknown[];
  track_id?: number | null;
  /** Navidrome song once imported and scanned */
  nd_song_id?: string | null;
  /** target playlist of the batch */
  playlist_id?: string | null;
  /** catalog track the item came from */
  track_ref?: string | null;
  /** queued only for ♥ or by an import (nobody asked to download it): not shown as a download; false once a
   * download queued only for ♥ fails (the user sees it then) */
  star?: boolean;
  /** queued with ♥: the song is starred once imported */
  liked?: boolean;
  /** queued by this import (`/api/imports`) */
  import_id?: number | null;
  candidates?: Candidate[] | null;
  chosen?: Candidate | null;
  created_at: ISODateTime;
  updated_at: ISODateTime;
}

export interface RetryIn {
  candidate_index?: number | null;
  url?: string | null;
}

export interface ClearOut {
  removed: number;
}

// --- uploads ----------------------------------------------------------------------------------

export interface UploadTags {
  title?: string | null;
  artist?: string | null;
  album?: string | null;
  album_artist?: string | null;
  track_no?: number | null;
  disc_no?: number | null;
  year?: number | null;
  genre?: string | null;
  has_cover?: boolean;
  duration_s?: number | null;
  [key: string]: unknown;
}

export interface UploadOut {
  id: number;
  orig_name: string;
  size: number;
  status: string;
  tags: UploadTags;
  error?: string | null;
  created_at: ISODateTime;
}

export interface UploadTrackIn {
  upload_id: number;
  title: string;
  artist: string;
  track_no?: number | null;
  disc_no?: number | null;
  genre?: string | null;
  explicit?: boolean | null;
}

export interface UploadAlbumIn {
  mode: 'existing' | 'new';
  album_id?: number | null;
  title?: string | null;
  album_artist?: string | null;
  year?: number | null;
  genre?: string | null;
  release_type?: ReleaseType | null;
}

export interface UploadCommitIn {
  album: UploadAlbumIn;
  tracks: UploadTrackIn[];
  cover_from_upload_id?: number | null;
  playlist_id?: string | null;
}

export interface UploadCommitOut {
  album_id: number;
  imported: number;
}

// --- playlists --------------------------------------------------------------------------------

export interface PlaylistOut {
  id: string;
  name: string;
  song_count: number;
  duration_s: number;
  cover_art?: string | null;
}

export interface PlaylistPageOut extends PlaylistOut {
  tracks: NdSongOut[];
}

// --- the Navidrome library (the PWA in a browser: /api/nd) ------------------------------------

/** A library song or a catalog-only favorite (/nd/liked, without nd_song_id until downloaded). */
export interface NdSongOut extends TrackOut {
  nd_album_id?: string | null;
  nd_artist_id?: string | null;
}

export interface NdAlbumOut {
  id: string;
  title: string;
  artist: string;
  artist_id?: string | null;
  year?: number | null;
  song_count: number;
  duration_s: number;
  cover_url?: string | null;
  starred: boolean;
  tracks?: NdSongOut[] | null;
}

export interface NdArtistOut {
  id: string;
  name: string;
  album_count: number;
  cover_url?: string | null;
  starred: boolean;
}

export interface NdArtistPageOut {
  artist: NdArtistOut;
  albums: NdAlbumOut[];
}

export interface NdLikedOut {
  songs: NdSongOut[];
  albums: NdAlbumOut[];
  artists: NdArtistOut[];
}

export interface NdHomeOut {
  recent: NdAlbumOut[];
  frequent: NdAlbumOut[];
  newest: NdAlbumOut[];
  starred: NdAlbumOut[];
  /** «Радиостанции»: liked artists first */
  radio: NdArtistOut[];
}

export type NdAlbumList = 'recent' | 'frequent' | 'newest' | 'starred' | 'alphabeticalByName' | 'alphabeticalByArtist' | 'random';

export interface PlaylistCreateIn {
  name: string;
}

export interface PlaylistAddIn {
  song_ids?: string[];
  library_track_ids?: number[];
}

// --- assistant --------------------------------------------------------------------------------

export type AssistantTaskStatus =
  | 'queued'
  | 'extracting'
  | 'resolving'
  | 'awaiting_confirmation'
  | 'confirmed'
  | 'done'
  | 'failed';

export type AssistantRowStatus = 'auto' | 'confirm' | 'not_found' | 'owned';

export interface AssistantChoice {
  kind: 'catalog' | 'library' | 'candidate' | 'manual' | 'skip';
  ref?: string | null;
  track_id?: number | null;
  file_ref?: string | null;
  artist?: string | null;
  title?: string | null;
}

export interface AssistantRowIn {
  index: number;
  selected: boolean;
  choice?: AssistantChoice | null;
}

export interface AssistantTargetIn {
  kind: 'new' | 'existing' | 'library';
  name?: string | null;
  playlist_id?: string | null;
}

export interface AssistantConfirmIn {
  confirm_token: string;
  rows: AssistantRowIn[];
  target: AssistantTargetIn;
}

export interface AssistantRowOut {
  index: number;
  artist_raw: string;
  title_raw: string;
  album_raw?: string | null;
  duration?: string | null;
  truncated: boolean;
  status: AssistantRowStatus;
  best?: TrackOut | null;
  options: TrackOut[];
  score: number;
  library_track_id?: number | null;
}

export interface AssistantTaskOut {
  id: number;
  status: AssistantTaskStatus | string;
  input_kind: string;
  source_app?: string | null;
  page_type?: string | null;
  context: Record<string, unknown>;
  rows: AssistantRowOut[];
  suggested_playlist_name?: string | null;
  confirm_token?: string | null;
  batch_id?: number | null;
  playlist_id?: string | null;
  error?: string | null;
  cost_usd: number;
  created_at: ISODateTime;
}

export interface AssistantTextIn {
  text?: string | null;
  url?: string | null;
}

// --- assistant free chat (stage 3.8) --------------------------------------------------------------

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface ChatIn {
  messages: ChatMessage[];
  /** the saved chat this turn continues; none → the Hub starts a new one */
  chat_id?: number | null;
}

/** What the assistant may not do without a tap: everything else it does itself (`ChatOut.done`). */
export type ChatAction =
  | { type: 'delete_playlist'; playlist_id: string; name: string }
  | { type: 'delete_library_tracks'; track_ids: number[]; titles: string[] };

export interface ChatOut {
  reply: string;
  cost_usd?: number;
  /** changes the assistant made this turn, written by the Hub */
  done?: string[];
  actions?: ChatAction[];
  task_id?: number | null;
  confirm_token?: string | null;
  summary?: string | null;
  /** the saved chat this turn went into */
  chat_id: number;
  title: string;
}

export interface ChatConfirmIn {
  confirm_token: string;
  chat_id?: number | null;
}

/** A saved chat in the history list. */
export interface ChatBriefOut {
  id: number;
  title: string;
  /** number of messages */
  messages: number;
  created_at: string;
  updated_at: string;
}

/** A message of a saved chat, as the Hub stored it (screenshots only by their count). */
export interface SavedChatMessage {
  role: 'user' | 'assistant';
  content: string;
  images?: number;
  cost?: number;
  done?: string[];
  proposal?: {
    task_id: number;
    token: string;
    summary: string;
    actions: ChatAction[];
    state: 'pending' | 'confirmed' | 'dismissed';
  };
}

export interface ChatSavedOut extends Omit<ChatBriefOut, 'messages'> {
  messages: SavedChatMessage[];
}

export interface ChatConfirmOut {
  ok: boolean;
  playlists_deleted: string[];
  tracks_deleted: number;
}

// --- imports from music services ------------------------------------------------------------

export type ImportMethod = 'device' | 'token' | 'profile';
export type ImportMode = 'economy' | 'full';
export type ImportStatus = 'auth' | 'fetching' | 'preview' | 'running' | 'done' | 'failed' | 'cancelled';
export type ImportCollectionKind = 'likes' | 'playlist' | 'albums' | 'artists' | 'dislikes';

export interface ImportIn {
  source?: 'yandex';
  method: ImportMethod;
  token?: string;
  url?: string;
}

export interface ImportStatsOut {
  tracks: number;
  owned: number;
  to_download: number;
  est_bytes: number;
  free_bytes: number | null;
  pending: number;
  exact: number;
  probable: number;
  not_found: number;
  waiting: number;
  downloading: number;
  done: number;
  failed: number;
}

export interface ImportCollectionOut {
  id: number;
  kind: ImportCollectionKind;
  title: string;
  selected: boolean;
  count: number;
  owned: number;
  cover_url?: string | null;
  nd_playlist_id?: string | null;
}

export interface ImportOut {
  id: number;
  source: 'yandex';
  method: ImportMethod;
  status: ImportStatus | string;
  mode: ImportMode;
  account?: string | null;
  has_token: boolean;
  keep_token: boolean;
  download_albums: boolean;
  /** sign-in by code in progress */
  auth?: { user_code: string; verification_url: string; expires_at: ISODateTime } | null;
  error?: string | null;
  stats: ImportStatsOut;
  collections: ImportCollectionOut[];
  created_at: ISODateTime;
  started_at?: ISODateTime | null;
  finished_at?: ISODateTime | null;
}

export interface ImportStartIn {
  collection_ids: number[];
  mode: ImportMode;
  download_albums?: boolean;
  keep_token?: boolean;
}

export type ImportMatch = 'owned' | 'exact' | 'probable' | 'not_found';
export type ImportItemStatus = 'pending' | 'waiting' | 'queued' | 'done' | 'failed' | 'not_found' | 'skipped';

export interface ImportItemOut {
  id: number;
  collection_id: number;
  position: number;
  kind: 'track' | 'album' | 'artist';
  artist: string;
  title: string;
  album?: string | null;
  duration_s?: number | null;
  cover_url?: string | null;
  ref?: string | null;
  match?: ImportMatch | null;
  score?: number | null;
  status: ImportItemStatus | string;
  nd_song_id?: string | null;
  download_item_id?: number | null;
  download_status?: string | null;
  error?: string | null;
}

export interface ImportItemsOut {
  items: ImportItemOut[];
  total: number;
}

export interface ImportEvent {
  id: number;
  status: string;
}

// --- events (SSE) -------------------------------------------------------------------------------

export interface AssistantEvent {
  id: number;
  status: string;
}

export interface LibraryEvent {
  kind: 'imported' | 'changed' | 'starred' | 'unstarred';
  album_id?: number | null;
  track_id?: number | null;
  /** starred / unstarred: songs the Hub (an import, a download queued with ♥, the assistant) starred or unstarred */
  song_ids?: string[];
}

export interface BatchEvent {
  batch_id: number;
  queued: number;
}

export interface HubEventMap {
  download: DownloadItemOut;
  assistant: AssistantEvent;
  library: LibraryEvent;
  batch: BatchEvent;
  import: ImportEvent;
  search_history: Record<string, never>;
  ping: Record<string, never>;
  /** Pseudo event: the stream (re)connected after a drop; refetch state. */
  reconnect: Record<string, never>;
}

// --- misc ---------------------------------------------------------------------------------------

export interface HealthOut {
  capabilities?: string[];
  ok: boolean;
  navidrome: boolean | null;
  providers: Record<string, unknown>;
  version: string;
  today?: ISODate | null;
}

// --- listening data (MIXES.md 3.2) -----------------------------------------------------------------

export type PlayKind = 'start' | 'progress' | 'interrupted' | 'error' | 'skip' | 'finish' | 'like' | 'unlike' | 'dislike' | 'undislike';

export interface PlayEventIn {
  target_type?: FavoriteType;
  nd_album_id?: string | null;
  nd_artist_id?: string | null;
  target_data?: Record<string, unknown> | null;
  reverts_event_id?: string | null;
  event_id: string;
  /** ISO 8601, UTC */
  ts: string;
  kind: PlayKind;
  ref?: string | null;
  nd_song_id?: string | null;
  playback_id?: string | null;
  device_id?: string | null;
  listened_ms?: number | null;
  track_metadata?: { title?: string; album?: string; artist_credit?: string; artists?: { name: string; ref?: string }[]; duration_s?: number | null; isrc?: string; year?: number } | null;
  position_s?: number | null;
  duration_s?: number | null;
  /** where the list was started from: album, playlist, search, liked, queue, other (mix:<id>, wave:<id> later) */
  context?: string | null;
  client: string;
  /** analytics only (INSIGHTS.md 6.5): one id for every event of a launched list (`wave:<id>` for a wave) */
  launch_id?: string | null;
  /** where the list was launched: `mix_page`, `mix_page_track`, `album`, `search`, `wave`… */
  launch_origin?: LaunchOrigin | null;
}

/** Where in the interface a list or a wave was launched (INSIGHTS.md 6.3, 6.5): `^[a-z][a-z0-9_]*$`, ≤ 32. */
export type LaunchOrigin = string;

export interface PlayEventsOut {
  accepted: number;
  duplicates: number;
  rejected: { index: number; event_id?: string | null; detail: string }[];
}

export interface DislikeIn {
  ref?: string | null;
  nd_song_id?: string | null;
  artist_ref?: string | null;
  artist?: string | null;
  title?: string | null;
  context?: string | null;
  client?: string;
}

export interface DislikeOut {
  id: number;
  ref: string | null;
  nd_song_id: string | null;
  artist_ref: string | null;
  artist: string | null;
  title: string | null;
  source: 'app' | 'pwa' | 'yandex_import' | string;
  created_at: string;
}

// --- «Микс дня» and the taste (MIXES.md 6) ----------------------------------------------------------

export type MixMode = 'normal' | 'favorites' | 'discover';

export interface MixTrackOut extends TrackOut {
  explanation: string[];
  position: number;
  /** the strongest source of the song: favorites, stash, ytm_radio, lastfm_similar… */
  generator: string;
  generator_title: string;
  generators: string[];
  /** familiar to the user or new */
  familiar: boolean;
  score: number;
  /** closeness to the taste by sound; null — not known */
  sim?: number | null;
}

export interface MixOut {
  id: number;
  title: string;
  description: string;
  mode: MixMode;
  mode_title: string;
  created_at: ISODateTime;
  nd_playlist_id?: string | null;
  playlist_status: 'pending' | 'error' | 'published';
  playlist_error: string | null;
  /** songs in the library already; the rest download quietly */
  ready: number;
  total: number;
  tracks: MixTrackOut[];
  generators: Record<string, unknown>;
}

export interface MixSettingsOut {
  mode: MixMode;
  mix_hour: number;
  timezone: string;
  /** local time */
  next_build_at: ISODateTime;
  nd_playlist_id?: string | null;
  artists: Array<{ ref: string; name: string }>;
}

export interface MixTodayOut {
  mix: MixOut | null;
  /** a build is queued or running */
  building: boolean;
  settings: MixSettingsOut;
  /** no history, no picked artists and no mix: show «Что вы слушаете?» */
  onboarding: boolean;
}

export interface MixSettingsIn {
  mode?: MixMode;
  mix_hour?: number;
}

export interface TasteOnboardingOut {
  needed: boolean;
  selected: Array<{ ref: string; name: string }>;
  suggestions: CatalogArtist[];
}

// «Моя волна» (MIXES.md 7, hub/API.md «Моя волна»)

/** What a wave starts from («Волна по треку»): one of them. */
export interface WaveSeedIn {
  ref?: string | null;
  nd_song_id?: string | null;
  artist_ref?: string | null;
  nd_artist_id?: string | null;
  by_artist?: boolean;
}

export interface WavePreferences {
  energy: 'any' | 'calm' | 'energetic';
  vocals: 'any' | 'vocal' | 'instrumental';
  discovery: 'balanced' | 'familiar' | 'new';
}

export interface WaveStartIn {
  preferences?: WavePreferences;
  seed?: WaveSeedIn | null;
  mode?: MixMode | null;
  /** analytics only: where it was started (`home_card`, `catalog_card`, `track_menu`, `artist_menu`, `player`,
   * `artist_page`, `radio`, `search_file`; the app's `restart`), the client (`pwa`, `pwa-app`) */
  origin?: WaveOrigin | null;
  client?: string | null;
  restart_of?: string | null;
}

export type WaveOrigin = 'home_card' | 'catalog_card' | 'track_menu' | 'artist_menu' | 'player' | 'artist_page' | 'radio'
  | 'search_file' | 'restart';

export interface WaveNextIn {
  preferences?: WavePreferences;
  /** the events since the previous call (the same ones go to /api/plays/events: one event_id is stored once) */
  played: PlayEventIn[];
  /** refs of the wave's tracks still ahead in the player, not started: the answer replaces them */
  queued: string[];
}

export interface WaveTrackOut extends TrackOut {
  explanation: string[];
  generator: string;
  generator_title: string;
  familiar: boolean;
  sim?: number | null;
}

// «Умное перемешивание» (MIXES.md 8, hub/API.md «Умное перемешивание»)

export interface SmartStartIn {
  /** `liked` or `playlist:<Navidrome id>` */
  context: string;
  /** the context's songs in its order (a playlist: new ones at the end; «Понравившиеся»: the newest first): a Navidrome
   * song id, or a catalog ref for a ♥ not downloaded yet */
  songs: string[];
  /** the song tapped or playing now: it plays first */
  current?: string | null;
  title?: string | null;
  origin?: string | null;
  client?: string | null;
}

export interface SmartNextIn {
  played: PlayEventIn[];
  /** the own song the player is at */
  at?: string | null;
}

/** A recommendation: it goes right after the own song `after` (an id from `SmartOut.own`). */
export interface SmartRecOut extends WaveTrackOut {
  after: string;
  slot: number;
}

export interface SmartOut {
  session_id: string;
  context: string;
  title?: string | null;
  /** the own songs in the order to play (start only) */
  own: string[];
  recs: SmartRecOut[];
  pending: boolean;
  own_per_rec: number;
  recorded?: PlayEventsOut | null;
}

export interface SmartAddOut {
  ok: boolean;
  /** added to the playlist / downloading and will be added / «Понравившиеся»: the client likes it itself */
  status: 'added' | 'downloading' | 'liked';
  playlist_id?: string | null;
}

export interface WaveOut {
  preferences: WavePreferences;
  preferences_status: string | null;
  preparation: { ready: number; pending: number; unavailable: number };
  session_id: string;
  title: string;
  mode: MixMode;
  seed?: { ref?: string; artist_ref?: string; nd_artist_id?: string; artist?: string; title?: string } | null;
  /** up to 5: they replace `queued` */
  tracks: WaveTrackOut[];
  /** sources still answer: an empty `tracks` — ask again in a few seconds */
  pending: boolean;
  recorded?: PlayEventsOut | null;
}
