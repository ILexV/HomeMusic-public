"""Request/response models of the Hub HTTP API (contract for the PWA and the future Android app)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.catalog.models import CatalogAlbum, CatalogArtist, CatalogTrack
from app.providers.base import Candidate

# --- auth ------------------------------------------------------------------------------


class SearchHistoryIn(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=512)]


class SearchHistoryOut(BaseModel):
    queries: list[str]


class LoginIn(BaseModel):
    username: str
    password: str


class MeOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    public_music_url: str = ""
    public_hub_url: str = ""  # for links that leave the Hub (an invite: where to get the app)
    google_email: str | None = None  # the linked Google account (sign-in with Google in the app)
    email: str | None = None  # the email the account registered with (sign-in by it, too)


class AuthOptionsOut(BaseModel):
    """What the sign-in screen offers: registration by invite code, by email (a code in a letter, no invite),
    Google (with the Web client id for the app)."""

    invites: bool = True
    email_signup: bool = False
    google_client_id: str | None = None
    # the public address of the Navidrome these accounts are for: the app offers registration only for it
    music_url: str = ""


class RegisterIn(BaseModel):
    code: str
    username: str
    password: str


class EmailCodeIn(BaseModel):
    email: str = Field(max_length=320)


class EmailCodeOut(BaseModel):
    resend_in: int  # seconds before another letter may be asked for


class EmailRegisterIn(BaseModel):
    email: str = Field(max_length=320)
    code: str = Field(max_length=16)
    username: str
    password: str


class GoogleIn(BaseModel):
    id_token: str
    # for a Google account that is not linked yet: register it with an invite code
    code: str | None = None
    username: str | None = None


class GoogleLoginOut(MeOut):
    """The player signs in to Navidrome itself (Subsonic), so it gets the account's password."""

    password: str


class GoogleLinkIn(BaseModel):
    id_token: str


# --- admin -------------------------------------------------------------------------------

class InviteIn(BaseModel):
    note: str = Field("", max_length=128)
    days: int | None = Field(None, ge=1, le=90)  # default INVITE_DAYS


class InviteOut(BaseModel):
    id: int
    code: str  # formatted «ABCD-EFGH»
    note: str
    status: Literal["active", "used", "expired"]
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    used_by: str | None = None  # username


class AdminUserOut(BaseModel):
    """A Navidrome user with what the Hub knows about them. `id` is null for users who never opened the Hub."""

    id: int | None = None
    username: str
    name: str = ""
    is_admin: bool = False
    in_hub: bool = True
    created_at: datetime | None = None
    last_seen_at: datetime | None = None  # last request to the Hub
    player_seen_at: datetime | None = None  # Navidrome's lastAccessAt (the player, any client)
    google_email: str | None = None
    email: str | None = None  # registered by email
    invite_note: str | None = None
    ai_cost_usd: float = 0
    ai_cost_30d_usd: float = 0
    ai_requests: int = 0
    ai_last_at: datetime | None = None
    downloads_done: int = 0
    downloads_failed: int = 0
    downloads_active: int = 0
    tracks_added: int = 0  # library tracks this user brought in (downloads and own files)
    uploads: int = 0
    starred_songs: int | None = None  # ♥, from Navidrome; null if it did not answer
    playlists: int | None = None


class AdminUsersOut(BaseModel):
    users: list[AdminUserOut]
    ai_cost_usd: float = 0
    ai_cost_30d_usd: float = 0
    library_tracks: int = 0


class AiUsageOut(BaseModel):
    kind: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    created_at: datetime


class AiMonthOut(BaseModel):
    month: str  # "2026-10"
    cost_usd: float
    requests: int


class AdminDownloadOut(BaseModel):
    artist: str
    title: str
    status: str
    created_at: datetime


class AdminUserDetailOut(AdminUserOut):
    ai_months: list[AiMonthOut] = []
    ai_recent: list[AiUsageOut] = []
    recent_downloads: list[AdminDownloadOut] = []


# --- library -----------------------------------------------------------------------------


class LibTrackOut(BaseModel):
    id: int
    nd_song_id: str | None
    album_id: int | None
    artist: str
    title: str
    album: str | None
    album_artist: str | None
    track_no: int | None
    disc_no: int | None
    year: int | None
    duration_s: int | None
    bitrate_kbps: int | None
    codec: str | None
    explicit: bool | None
    source: str | None
    locked: bool
    warnings: list[Any] = []
    path: str


class TrackSourceOut(BaseModel):
    """Where a library file came from: `source` is the provider (hitmoz/hitmos_fm/ytdlp) or upload/manual."""
    source: str | None
    source_url: str | None
    acquired_at: datetime


class LibAlbumOut(BaseModel):
    id: int
    uid: str
    title: str
    album_artist: str
    year: int | None
    release_type: str | None
    genre: str | None
    deezer_album_id: str | None
    track_count: int = 0
    has_cover: bool = False
    nd_album_id: str | None = None
    tracks: list[LibTrackOut] | None = None


class AlbumPatchIn(BaseModel):
    title: str | None = None
    album_artist: str | None = None
    year: int | None = None
    genre: str | None = None
    release_type: Literal["album", "single", "ep", "compilation"] | None = None


class AlbumCreateIn(BaseModel):
    title: str
    album_artist: str
    year: int | None = None
    genre: str | None = None
    release_type: Literal["album", "single", "ep", "compilation"] | None = "album"


class AlbumMergeIn(BaseModel):
    source_ids: list[int]
    target_id: int


class TracksMoveIn(BaseModel):
    track_ids: list[int]
    album_id: int


class TrackPatchIn(BaseModel):
    title: str | None = None
    artist: str | None = None
    track_no: int | None = None
    disc_no: int | None = None
    genre: str | None = None
    explicit: bool | None = None


class FavoriteTarget(BaseModel):
    target_type: Literal["track", "album", "artist"] = "track"
    ref: str | None = Field(default=None, min_length=1, max_length=64)
    nd_song_id: str | None = Field(default=None, min_length=1, max_length=64)
    nd_album_id: str | None = Field(default=None, min_length=1, max_length=64)
    nd_artist_id: str | None = Field(default=None, min_length=1, max_length=64)
    target_data: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _names_target(self):
        import json

        ids = {"track": self.nd_song_id, "album": self.nd_album_id, "artist": self.nd_artist_id}
        if not (self.ref or ids[self.target_type]) or any(v for k, v in ids.items() if k != self.target_type):
            raise ValueError("ref or matching nd_song_id/nd_album_id/nd_artist_id is required; identifiers must name exactly one target type")
        if self.target_data is not None and len(json.dumps(self.target_data, ensure_ascii=False)) > 8192:
            raise ValueError("target_data exceeds 8192 characters")
        return self


class StarIn(BaseModel):
    song_ids: list[str] = []
    album_ids: list[str] = []
    artist_ids: list[str] = []
    # catalog refs: owned tracks/albums are resolved to Navidrome ids
    catalog_album_ref: str | None = None
    # catalog tracks liked by an import and not downloaded yet (`StarredOut.refs`): only unstar applies
    refs: list[str] = []
    unstar: bool = False
    targets: list[FavoriteTarget] = Field(default_factory=list, max_length=500)
    event_id: str | None = Field(default=None, min_length=1, max_length=64)
    ts: datetime | None = None
    client: str = Field(default="pwa", min_length=1, max_length=32)
    context: str | None = Field(default=None, max_length=128)


class StarredOut(BaseModel):
    """The current user's Navidrome stars (♥ in the Hub and the app's «Понравившиеся»)."""

    song_ids: list[str] = []
    album_ids: list[str] = []
    artist_ids: list[str] = []
    subjects: list[dict[str, Any]] = []
    # catalog tracks the user liked in another service (an import) that are not in the library yet
    refs: list[str] = []


# --- catalog (with library ownership) ---------------------------------------------------

OwnedStatus = Literal["full", "partial", "none"]


class TrackOut(CatalogTrack):
    owned: bool = False
    library_track_id: int | None = None
    nd_song_id: str | None = None
    downloading: bool = False  # a download of it is under way (not counting ones queued only for ♥)
    star_pending: bool = False  # the user ♥ it and a download that stars it is under way: show it liked


class AlbumOut(CatalogAlbum):
    owned_status: OwnedStatus = "none"
    owned_count: int = 0
    tracks: list[TrackOut] | None = None  # type: ignore[assignment]


class ArtistPageOut(BaseModel):
    artist: CatalogArtist
    top_tracks: list[TrackOut] = []
    albums: list[AlbumOut] = []
    singles: list[AlbumOut] = []
    compilations: list[AlbumOut] = []


class SearchOut(BaseModel):
    library: list[LibTrackOut] = []
    artists: list[CatalogArtist] = []
    albums: list[AlbumOut] = []
    tracks: list[TrackOut] = []
    tracks_next: int | None = None  # offset of the next page of tracks (GET /api/search/tracks); None — that's all
    errors: list[str] = []  # e.g. "Deezer недоступен"


class SearchTracksOut(BaseModel):
    """A further page of the catalog track search."""

    tracks: list[TrackOut] = []
    next_offset: int | None = None  # None — the end of the results


class ProviderSearchOut(BaseModel):
    candidates: list[Candidate] = []
    errors: list[str] = []


# --- downloads -----------------------------------------------------------------------------


class DownloadTrackIn(BaseModel):
    """Catalog metadata or a server-remembered provider file; never a client-supplied URL."""

    model_config = {"extra": "forbid"}

    artist: str
    title: str
    album: str | None = None
    duration_s: int | None = None
    isrc: str | None = None
    explicit: bool | None = None
    version: str | None = None
    catalog_ref: str | None = None  # "dz:123" track ref (gives album/tags)
    file_ref: str | None = None  # file:<provider>:<id>, issued by provider search


class DownloadIn(BaseModel):
    items: list[DownloadTrackIn] = []
    track_refs: list[str] = []
    album_ref: str | None = None
    artist_ref: str | None = None  # whole discography
    record_types: list[str] = ["album", "single", "ep"]  # for artist_ref
    playlist_id: str | None = None
    new_playlist_name: str | None = None
    force: bool = False  # re-download even if owned (new file next to the old one)
    star: bool = False  # ♥ on a track that isn't downloaded yet: star the songs in Navidrome once they are
    favorite_event_id: str | None = Field(default=None, min_length=1, max_length=64)
    favorite_ts: datetime | None = None
    favorite_client: str = Field(default="hub", min_length=1, max_length=32)
    favorite_context: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _favorite_command(self):
        if self.favorite_event_id and (not self.star or len(self.track_refs) != 1 or self.items or
                                       self.album_ref or self.artist_ref or self.force):
            raise ValueError("favorite_event_id requires one track ref and star=true")
        return self


class ListenOut(BaseModel):
    """What plays for a catalog track before it is downloaded (GET /api/listen)."""

    ref: str
    kind: Literal["full", "preview", "library"]  # the file a download would pick / Deezer 30 s preview / own file
    artist: str
    title: str
    duration_s: int | None = None
    provider: str | None = None  # hitmoz / hitmos_fm / ytdlp for "full"
    bitrate_kbps: int | None = None
    page_url: str | None = None
    nd_song_id: str | None = None  # "library": the Navidrome song (♥, the app's player)
    stream_url: str  # audio with Range support, same session cookie
    recording_binding: str | None = None


class ListenPrefetchIn(BaseModel):
    """Catalog tracks on the user's screen, top first (POST /api/listen/prefetch); replaces the previous list."""

    refs: list[str] = Field(default_factory=list, max_length=100)


class ListenPrefetchOut(BaseModel):
    queued: int  # tracks whose file is not picked yet and will be, in this order


class DownloadBatchOut(BaseModel):
    batch_id: int
    queued: int
    already_owned: int
    playlist_id: str | None = None
    expanding: bool = False  # catalog refs are turned into items by a background job; `queued` doesn't count them


class DownloadItemOut(BaseModel):
    id: int
    batch_id: int
    status: str
    progress: float
    artist: str
    title: str
    album: str | None = None
    provider: str | None = None
    error: str | None = None
    warnings: list[Any] = []
    track_id: int | None = None
    nd_song_id: str | None = None  # Navidrome song once imported and scanned
    playlist_id: str | None = None  # target Navidrome playlist of the batch (Android: sync only that one offline)
    track_ref: str | None = None  # catalog track the item was made from (dz:…)
    # queued only for ♥ (`DownloadIn.star`) or by an import, nobody asked to download it: not shown as a download
    # (PWA, app notifications), not downloaded to the phone; false once the track is downloaded explicitly too, and
    # once a download queued only for ♥ fails (`failed`/`needs_review`): the user sees it then
    star: bool = False
    liked: bool = False  # queued with ♥: the song is starred once imported
    import_id: int | None = None  # queued by this import (`/api/imports`)
    candidates: list[Candidate] | None = None  # only in detail view
    chosen: Candidate | None = None
    created_at: datetime
    updated_at: datetime


class RetryIn(BaseModel):
    candidate_index: int | None = None
    url: str | None = None  # hitmoz/hitmos.fm song page or a YouTube link


# --- uploads -----------------------------------------------------------------------------


class UploadOut(BaseModel):
    id: int
    orig_name: str
    size: int
    status: str
    tags: dict[str, Any] = {}
    error: str | None = None
    created_at: datetime


class UploadTrackIn(BaseModel):
    upload_id: int
    title: str
    artist: str
    track_no: int | None = None
    disc_no: int | None = None
    genre: str | None = None
    explicit: bool | None = None


class UploadAlbumIn(BaseModel):
    mode: Literal["existing", "new"]
    album_id: int | None = None  # mode=existing
    title: str | None = None  # mode=new
    album_artist: str | None = None
    year: int | None = None
    genre: str | None = None
    release_type: Literal["album", "single", "ep", "compilation"] | None = "album"


class UploadCommitIn(BaseModel):
    album: UploadAlbumIn
    tracks: list[UploadTrackIn]
    cover_from_upload_id: int | None = None  # use the embedded cover of this upload
    playlist_id: str | None = None


class UploadCommitOut(BaseModel):
    album_id: int
    imported: int


# --- playlists -----------------------------------------------------------------------------


class PlaylistOut(BaseModel):
    id: str
    name: str
    song_count: int = 0
    duration_s: int = 0
    cover_art: str | None = None


class PlaylistCreateIn(BaseModel):
    name: str


class PlaylistRemoveIn(BaseModel):
    indexes: list[int] = Field(min_length=1, max_length=500)  # positions in the playlist, from 0


# --- the Navidrome library as the player sees it (the PWA in a browser, app/api/medialib.py) ----------------------


class NdSongOut(TrackOut):
    """A Navidrome song as a catalog-like track: `ref` "dz:…" when the Hub knows its Deezer record, else
    "nd:<song>". In /nd/liked, catalog-only favorites may be unowned without an ND id."""

    nd_album_id: str | None = None
    nd_artist_id: str | None = None


class NdAlbumOut(BaseModel):
    id: str
    title: str
    artist: str
    artist_id: str | None = None
    year: int | None = None
    song_count: int = 0
    duration_s: int = 0
    cover_url: str | None = None
    starred: bool = False
    tracks: list[NdSongOut] | None = None


class NdArtistOut(BaseModel):
    id: str
    name: str
    album_count: int = 0
    cover_url: str | None = None
    starred: bool = False


class NdArtistPageOut(BaseModel):
    artist: NdArtistOut
    albums: list[NdAlbumOut] = []


class NdLikedOut(BaseModel):
    songs: list[NdSongOut] = []
    albums: list[NdAlbumOut] = []
    artists: list[NdArtistOut] = []


class NdHomeOut(BaseModel):
    recent: list[NdAlbumOut] = []
    frequent: list[NdAlbumOut] = []
    newest: list[NdAlbumOut] = []
    starred: list[NdAlbumOut] = []
    radio: list[NdArtistOut] = []  # artists for «Радиостанции»: liked ones first


class PlaylistPageOut(PlaylistOut):
    tracks: list[NdSongOut] = []


class PlaylistAddIn(BaseModel):
    song_ids: list[str] = []
    library_track_ids: list[int] = []


# --- assistant -----------------------------------------------------------------------------


class AssistantChoice(BaseModel):
    model_config = {"extra": "forbid"}

    kind: Literal["catalog", "library", "candidate", "manual", "skip"]
    ref: str | None = None  # catalog ref
    track_id: int | None = None  # library track
    file_ref: str | None = None
    artist: str | None = None  # manual
    title: str | None = None


class AssistantRowIn(BaseModel):
    index: int
    selected: bool = True
    choice: AssistantChoice | None = None  # None = keep the suggested best match


class AssistantTargetIn(BaseModel):
    kind: Literal["new", "existing", "library"] = "new"
    name: str | None = None
    playlist_id: str | None = None


class AssistantConfirmIn(BaseModel):
    confirm_token: str
    rows: list[AssistantRowIn]
    target: AssistantTargetIn = AssistantTargetIn()


class AssistantRowOut(BaseModel):
    index: int
    artist_raw: str
    title_raw: str
    album_raw: str | None = None
    duration: str | None = None
    truncated: bool = False
    status: Literal["auto", "confirm", "not_found", "owned"]
    best: TrackOut | None = None
    options: list[TrackOut] = []  # top 3 for "confirm"
    score: float = 0
    library_track_id: int | None = None


class AssistantTaskOut(BaseModel):
    id: int
    status: str
    input_kind: str
    source_app: str | None = None
    page_type: str | None = None
    context: dict[str, Any] = {}
    rows: list[AssistantRowOut] = []
    suggested_playlist_name: str | None = None
    confirm_token: str | None = None
    batch_id: int | None = None
    playlist_id: str | None = None
    error: str | None = None
    cost_usd: float = 0
    created_at: datetime


class AssistantTextIn(BaseModel):
    text: str | None = None
    url: str | None = None


# --- imports from music services ----------------------------------------------------------

ImportSource = Literal["yandex"]
ImportMethod = Literal["device", "token", "profile"]
ImportMode = Literal["economy", "full"]


class ImportIn(BaseModel):
    source: ImportSource = "yandex"
    method: ImportMethod = "device"
    token: str | None = None  # method "token"
    url: str | None = None  # method "profile": a link to the profile or the login


class ImportAuthOut(BaseModel):
    """Sign-in by code in progress: the user enters `user_code` at `verification_url`."""

    user_code: str
    verification_url: str
    expires_at: datetime


class ImportStatsOut(BaseModel):
    tracks: int = 0  # distinct tracks in the selected collections (likes, playlists)
    owned: int = 0  # of them in the library already
    to_download: int = 0  # the rest (an estimate before matching)
    est_bytes: int = 0  # disk space `to_download` takes (320 kbps)
    free_bytes: int | None = None  # free space on the music disk
    pending: int = 0  # rows not matched yet
    exact: int = 0
    probable: int = 0
    not_found: int = 0
    waiting: int = 0  # economy: matched, downloaded once listened to
    downloading: int = 0  # downloads of the import under way
    done: int = 0
    failed: int = 0


class ImportCollectionOut(BaseModel):
    id: int
    kind: Literal["likes", "playlist", "albums", "artists", "dislikes"]
    title: str
    selected: bool
    count: int
    owned: int = 0
    cover_url: str | None = None
    nd_playlist_id: str | None = None


class ImportOut(BaseModel):
    id: int
    source: ImportSource
    method: ImportMethod
    # auth (waiting for the code) / fetching / preview (choose what to move) / running / done / failed / cancelled
    status: str
    mode: ImportMode
    account: str | None = None
    has_token: bool = False
    keep_token: bool = False
    download_albums: bool = False
    auth: ImportAuthOut | None = None
    error: str | None = None
    stats: ImportStatsOut = Field(default_factory=ImportStatsOut)
    collections: list[ImportCollectionOut] = []
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ImportStartIn(BaseModel):
    collection_ids: list[int]  # what to move; the rest is left out
    mode: ImportMode = "economy"
    download_albums: bool = False  # liked albums: download them whole (else only ★ the owned ones)
    keep_token: bool = False  # keep the sign-in for «Обновить»


class ImportItemOut(BaseModel):
    id: int
    collection_id: int
    position: int
    kind: Literal["track", "album", "artist"]
    artist: str = ""
    title: str = ""
    album: str | None = None
    duration_s: int | None = None
    cover_url: str | None = None
    ref: str | None = None  # catalog track / album / artist
    match: str | None = None  # owned/exact/probable/not_found
    score: float | None = None
    # pending/waiting/queued/done/failed/not_found/skipped
    status: str
    nd_song_id: str | None = None  # in the library: plays from Navidrome
    download_item_id: int | None = None
    download_status: str | None = None
    error: str | None = None


class ImportItemsOut(BaseModel):
    items: list[ImportItemOut]
    total: int


class ImportPickOut(BaseModel):
    download_item_id: int


# --- misc ---------------------------------------------------------------------------------


class HealthOut(BaseModel):
    ok: bool
    navidrome: bool | None = None
    providers: dict[str, Any] = Field(default_factory=dict)
    version: str = "0.1.0"
    today: date | None = None
    # id of the PWA build the Hub serves (index.html <meta name="hm-build">); pages compare it with their own
    web_build: str | None = None
    capabilities: list[str] = ["typed_favorites_v1", "favorite_compensation_chains_v1", "catalog_favorite_download_v1",
                               "listening_checkpoints_v1", "launch_events_v1", "wave_origin_v1", "smart_shuffle_v1",
                               "server_file_downloads_v1"]


# --- listening data (MIXES.md 3.2) -------------------------------------------------------------------------------

PlayKind = Literal["start", "progress", "interrupted", "error", "skip", "finish", "like", "unlike", "dislike", "undislike"]
# where in the interface a wave or a list was launched (INSIGHTS.md 6.3, 6.5): a code, unknown ones are kept as sent
ORIGIN_PATTERN = r"^[a-z][a-z0-9_]*$"


class ListeningArtist(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    ref: str | None = Field(default=None, max_length=64)
    nd_artist_id: str | None = Field(default=None, max_length=64)


class ListeningMetadata(BaseModel):
    title: str | None = Field(default=None, max_length=512)
    album: str | None = Field(default=None, max_length=512)
    artist_credit: str | None = Field(default=None, max_length=512)
    primary_nd_artist_id: str | None = Field(default=None, max_length=64)
    artists: list[ListeningArtist] | None = Field(default=None, max_length=30)
    duration_s: float | None = Field(default=None, ge=0, le=86400)
    isrc: str | None = Field(default=None, max_length=16)
    year: int | None = Field(default=None, ge=1000, le=9999)
    genre: str | None = Field(default=None, max_length=128)


class PlayEventIn(FavoriteTarget):
    """One event of a player. `finish`: the track played to its end, or the user left it after ≥ 90 % of it; `skip`:
    left earlier (next, another track); pause and seeking are no events. `position_s`/`duration_s`: where the player
    was (for `skip`/`finish`). Favorite actions can also name an album or artist through FavoriteTarget."""

    event_id: str = Field(min_length=1, max_length=64)
    ts: datetime
    kind: PlayKind
    ref: str | None = Field(default=None, max_length=64)
    nd_song_id: str | None = Field(default=None, max_length=64)
    playback_id: str | None = Field(default=None, min_length=1, max_length=64)
    device_id: str | None = Field(default=None, min_length=1, max_length=64)
    listened_ms: int | None = Field(default=None, ge=0, le=604800000)
    track_metadata: ListeningMetadata | None = None
    position_s: float | None = Field(default=None, ge=0, le=86400)
    duration_s: float | None = Field(default=None, ge=0, le=86400)
    context: str | None = Field(default=None, max_length=128)
    client: str = Field(min_length=1, max_length=32)
    reverts_event_id: str | None = Field(default=None, min_length=1, max_length=64)
    # analytics only (the taste profile never reads them): one id for every event of a launched list ("wave:<sid>"
    # for a wave) and where it was launched (`mix_page`, `album`...)
    launch_id: str | None = Field(default=None, min_length=1, max_length=64)
    launch_origin: str | None = Field(default=None, max_length=32, pattern=ORIGIN_PATTERN)

    @model_validator(mode="after")
    def _names_a_track(self) -> PlayEventIn:
        playback_kinds = ("start", "progress", "interrupted", "error", "skip", "finish")
        if self.target_type != "track" and self.kind in playback_kinds:
            raise ValueError("playback events require a track")
        if (self.playback_id is None) != (self.device_id is None):
            raise ValueError("playback_id and device_id must be provided together")
        if self.kind in ("progress", "interrupted", "error") and (not self.playback_id or self.listened_ms is None):
            raise ValueError("checkpoint requires playback_id, device_id and listened_ms")
        if self.listened_ms is not None and (self.kind not in playback_kinds or not self.playback_id):
            raise ValueError("listened_ms requires a playback attempt")
        if self.track_metadata is not None and (self.target_type != "track" or self.kind not in playback_kinds):
            raise ValueError("track_metadata requires a playback event")
        if self.target_type == "album" and self.kind in ("dislike", "undislike"):
            raise ValueError("dislikes support tracks and artists")
        if self.reverts_event_id and (self.kind not in ("like", "unlike") or self.reverts_event_id == self.event_id):
            raise ValueError("only another favorite decision can be reverted")
        return self


class PlayEventsIn(BaseModel):
    # validated one by one (`PlayEventIn`): a malformed event is rejected alone
    events: list[Any] = Field(max_length=500)
    expected_user_id: int | None = None


class PlayEventRejected(BaseModel):
    index: int
    event_id: str | None = None
    detail: str


class PlayEventsOut(BaseModel):
    accepted: int
    duplicates: int  # event_id already stored: sent before
    rejected: list[PlayEventRejected] = []


class DislikeIn(BaseModel):
    """«Не нравится»: a track (`ref` and/or `nd_song_id`) or an artist (`artist_ref`, catalog). `artist`/`title`
    are for showing the list."""

    ref: str | None = Field(default=None, max_length=64)
    nd_song_id: str | None = Field(default=None, max_length=64)
    artist_ref: str | None = Field(default=None, max_length=64)
    artist: str | None = Field(default=None, max_length=512)
    title: str | None = Field(default=None, max_length=512)
    context: str | None = Field(default=None, max_length=128)
    client: str = Field(default="pwa", min_length=1, max_length=32)

    @model_validator(mode="after")
    def _one_target(self) -> DislikeIn:
        track = bool(self.ref or self.nd_song_id)
        if track == bool(self.artist_ref):
            raise ValueError("a track (ref / nd_song_id) or an artist (artist_ref)")
        return self


class DislikeOut(BaseModel):
    id: int
    ref: str | None = None
    nd_song_id: str | None = None
    artist_ref: str | None = None
    artist: str | None = None
    title: str | None = None
    source: str  # app/pwa/yandex_import
    created_at: datetime


MixMode = Literal["normal", "favorites", "discover"]


class MixTrackOut(TrackOut):
    """A song of a mix: the catalog track (`ref` "dz:…"; a song only the library has — `source` "nd", `ref`
    "nd:<song>") with the library state, and why it is in the mix."""

    position: int
    generator: str  # the strongest of the generators that gave it: favorites/stash/ytm_radio/...
    generator_title: str  # for showing: «Любимое», «Похожее: радио YouTube Music»...
    generators: list[str] = []  # all of them
    familiar: bool  # familiar to the user (their history or a well-known artist) or new
    score: float
    sim: float | None = None  # closeness to the taste by sound (the embedder), None — not known
    explanation: list[str] = Field(default_factory=list)


class MixOut(BaseModel):
    id: int
    title: str
    description: str
    mode: MixMode
    mode_title: str
    created_at: datetime
    nd_playlist_id: str | None = None  # the Navidrome playlist «Микс дня» (the same id every day)
    playlist_status: Literal["pending", "error", "published"] = "pending"
    playlist_error: str | None = None
    ready: int  # songs in the library already (in the playlist)
    total: int
    tracks: list[MixTrackOut] = []
    generators: dict[str, Any] = {}  # generator -> {tracks, used, seconds, error?, off?}


class MixSettingsOut(BaseModel):
    mode: MixMode
    mix_hour: int
    timezone: str
    next_build_at: datetime  # local time
    nd_playlist_id: str | None = None
    artists: list[dict[str, Any]] = []  # picked on «Что вы слушаете?»: [{ref, name}]


class MixTodayOut(BaseModel):
    mix: MixOut | None = None  # None: not built yet (no history and no picked artists, or the first build is on)
    building: bool = False  # a build is queued or running
    settings: MixSettingsOut
    onboarding: bool = False  # a new user without history and picked artists: show «Что вы слушаете?»


class MixSettingsIn(BaseModel):
    mode: MixMode | None = None
    mix_hour: int | None = Field(default=None, ge=0, le=23)


class MixRebuildIn(BaseModel):
    user_id: int | None = None  # admin: another user's mix; default — one's own


class TasteOnboardingOut(BaseModel):
    needed: bool  # no history and no picked artists: the mix can't be made yet
    selected: list[dict[str, Any]] = []  # [{ref, name}]
    suggestions: list[CatalogArtist] = []  # the chart, or the search by `q`


class TasteArtistsIn(BaseModel):
    artist_refs: list[str] = Field(min_length=1, max_length=20)


# --- «Моя волна» (MIXES.md 7) ---------------------------------------------------------------------------------------


class WaveSeedIn(BaseModel):
    """What a wave starts from: a catalog track, a known search file, a library song or an artist."""

    ref: str | None = Field(default=None, max_length=64)  # "dz:…", "nd:<song>" or "file:<provider>:<id>"
    nd_song_id: str | None = Field(default=None, max_length=64)
    artist_ref: str | None = Field(default=None, max_length=64)  # "dz:…"
    nd_artist_id: str | None = Field(default=None, max_length=64)  # a Navidrome artist (the app's «Радиостанции»)

    by_artist: bool = False  # resolve the artist of the supplied track

    @model_validator(mode="after")
    def _names_one(self) -> WaveSeedIn:
        if not (self.ref or self.nd_song_id or self.artist_ref or self.nd_artist_id):
            raise ValueError("ref, nd_song_id, artist_ref or nd_artist_id is required")
        return self


class WavePreferences(BaseModel):
    energy: Literal["any", "calm", "energetic"] = "any"
    vocals: Literal["any", "vocal", "instrumental"] = "any"
    discovery: Literal["balanced", "familiar", "new"] = "balanced"


class WavePreparation(BaseModel):
    ready: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)
    unavailable: int = Field(default=0, ge=0)


class WaveStartIn(BaseModel):
    seed: WaveSeedIn | None = None  # none: the user's own wave, from the taste profile
    mode: MixMode | None = None  # the familiar share, as a mix's; none — the user's mix mode
    preferences: WavePreferences | None = None
    # analytics only (INSIGHTS.md 6.3): where it was started (`home_card`, `track_menu`... `restart`), the client
    # ("pwa", "pwa-app", "app/<version>"), the session the app makes again after a 404
    origin: str | None = Field(default=None, max_length=32, pattern=ORIGIN_PATTERN)
    client: str | None = Field(default=None, min_length=1, max_length=32)
    restart_of: str | None = Field(default=None, max_length=32)


class WaveNextIn(BaseModel):
    # the events since the previous call (`PlayEventIn` each, validated one by one); stored with context wave:<id>
    played: list[Any] = Field(default_factory=list, max_length=500)
    # refs (as `tracks[].ref`) of the wave's tracks the client still has ahead, not started: the answer replaces them
    queued: list[str] = Field(default_factory=list, max_length=50)
    preferences: WavePreferences | None = None


class WaveTrackOut(TrackOut):
    """A track of the wave: the catalog track (`ref` "dz:…"; a song only the library has — `source` "nd", `ref`
    "nd:<song>") with the library state: `owned` + `nd_song_id` — play the library song, else stream `ref` from the
    Hub (`/api/listen/stream`). A search file seed keeps `source="file"` and its exact `file:…` ref."""

    generator: str  # seed/session_radio/ytm_radio/favorites/...
    generator_title: str
    familiar: bool
    sim: float | None = None
    explanation: list[str] = Field(default_factory=list)


class WaveOut(BaseModel):
    session_id: str
    title: str  # «Моя волна», «Волна по треку «…»», «Волна: <артист>»
    mode: MixMode
    seed: dict[str, Any] | None = None  # {ref, artist, title} or {artist_ref, artist, nd_artist_id?}
    tracks: list[WaveTrackOut] = []  # the next ones (up to 5): they replace the client's `queued`
    pending: bool = False  # sources still answer: the pool grows (an empty `tracks` — ask again in a few seconds)
    recorded: PlayEventsOut | None = None  # what became of `played` (next only)
    preferences: WavePreferences = Field(default_factory=WavePreferences)
    preferences_status: str | None = None
    preparation: WavePreparation = Field(default_factory=WavePreparation)


# --- smart shuffle (MIXES.md 8) ------------------------------------------------------------------------------------


class SmartStartIn(BaseModel):
    context: str = Field(max_length=80, pattern=r"^(liked|playlist:\S{1,64})$")  # "liked" or "playlist:<Navidrome id>"
    # the context's songs in its order (a playlist: new ones at the end; «Понравившиеся»: the newest first): a
    # Navidrome song id, or a catalog ref (has a colon) for a ♥ not downloaded yet
    songs: list[str] = Field(min_length=1, max_length=5000)
    current: str | None = Field(default=None, max_length=80)  # the song tapped or playing now: it plays first
    title: str | None = Field(default=None, max_length=256)  # the playlist's name, kept for the analytics
    origin: str | None = Field(default=None, max_length=32, pattern=ORIGIN_PATTERN)
    client: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def _ids(self) -> SmartStartIn:
        if any(not s or len(s) > 80 for s in self.songs):
            raise ValueError("songs: ids up to 80 characters")
        return self


class SmartNextIn(BaseModel):
    played: list[Any] = Field(default_factory=list, max_length=500)  # the events since the last call
    at: str | None = Field(default=None, max_length=80)  # the own song the player is at (or passed last)


class SmartTrackIn(BaseModel):
    ref: str | None = Field(default=None, max_length=64)
    nd_song_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _names_one(self) -> SmartTrackIn:
        if not (self.ref or self.nd_song_id):
            raise ValueError("ref or nd_song_id is required")
        return self


class SmartRecOut(WaveTrackOut):
    """A recommendation: the wave's track plus where it goes — right after the own song `after` (an id from
    `own`); `slot` is its number among the recommendations of the list. `sim` — closeness to the context by sound."""

    after: str
    slot: int


class SmartOut(BaseModel):
    session_id: str
    context: str
    title: str | None = None
    own: list[str] = []  # the own songs in the order to play (start only)
    recs: list[SmartRecOut] = []
    pending: bool = False  # sources still answer: ask again (`next`) in a few seconds for the missing ones
    own_per_rec: int = 3
    recorded: PlayEventsOut | None = None  # what became of `played` (next only)


class SmartAddOut(BaseModel):
    ok: bool = True
    # added — put in the playlist; downloading — not in the library yet, it is downloaded and put in the playlist
    # after; liked — «Понравившиеся»: the Hub only noted it, the client likes the track itself
    status: Literal["added", "downloading", "liked"]
    playlist_id: str | None = None


# --- admin insights (INSIGHTS.md 7; read-only, admin only). Aggregate blocks stay dicts: their keys are in API.md.


class InsightsTrackName(BaseModel):
    key: str | None = None  # nd:<song>, else the catalog ref
    ref: str | None = None
    nd_song_id: str | None = None
    artist: str = ""
    title: str = ""
    owned: bool = False  # the song is in the library


class InsightsUser(BaseModel):
    id: int
    username: str
    is_admin: bool = False
    excluded: bool = False  # `analytics.exclude_users`: out of the totals


class InsightsOverviewRow(BaseModel):
    user: InsightsUser
    counted_listens: int
    heard_events: int
    listened_ms: int
    active_days: int
    mixes_built: int
    mix_launches: int
    wave_launches: int
    wave_restarts: int
    likes: int
    dislikes: int
    new_tracks: int
    new_artists: int
    last_activity_at: datetime | None = None
    app_version: str | None = None
    last_seen_at: datetime | None = None


class InsightsOverviewOut(BaseModel):
    period: dict[str, str]
    users: list[InsightsOverviewRow]
    totals: dict[str, int]


class InsightsListeningOut(BaseModel):
    user_id: int
    period: dict[str, str]  # from, to (local dates, inclusive), timezone
    totals: dict[str, int]
    by_day: list[dict[str, Any]]
    by_source: list[dict[str, Any]]
    heatmap: list[list[int]]  # 7 rows (Monday first) × 24 local hours: counted listens
    clients: list[dict[str, Any]]
    top_tracks: list[dict[str, Any]]
    top_artists: list[dict[str, Any]]
    discoveries: list[dict[str, Any]]
    recommendations: dict[str, Any]  # mix, wave
    quality: dict[str, Any]


class InsightsTasteTrackRow(InsightsTrackName):
    s: float
    p: float  # positive_raw
    sat_p: float  # sat(P); P itself for the local v1
    n: float  # negative
    liked: bool
    disliked: bool
    downloaded: bool
    album_liked: bool
    heard30: bool
    familiar: bool
    seed_eligible: bool
    last_heard: datetime | None = None
    last_positive: datetime | None = None
    positive_age: float | None = None
    plays: int
    early_skips: int
    facet_id: str | None = None


class InsightsTasteOut(BaseModel):
    user_id: int
    model: Literal["v1", "v2"]
    model_note: str | None = None
    now: datetime
    timezone: str
    evidence: float
    maturity: float | None = None
    active_units: float | None = None
    active_days: int | None = None
    counts: dict[str, int]
    facets: list[dict[str, Any]] | None = None  # null: the embedder is off/down (see `embedder`)
    embedder: Any = None  # "off" / "down" / "timeout" / "v1" / {space_id, model}
    seeds_today: list[dict[str, Any]]
    onboarding_reserve: int
    onboarding_artists: list[dict[str, Any]]
    seeds_note: str
    top_artists: list[dict[str, Any]]
    limitations: list[str]


class InsightsTasteTracksOut(BaseModel):
    model: Literal["v1", "v2"]
    now: datetime
    embedder: Any = None
    total: int
    offset: int
    rows: list[InsightsTasteTrackRow]


class InsightsContribution(BaseModel):
    kind: str  # finish/skip/start/like/dislike/dislike_legacy/download/album_like
    ts: datetime | None = None
    event_id: str | None = None
    side: Literal["P", "N", "0"]
    base: float
    age: float | None = None
    w: float
    value: float  # added to P (side P) or to N (side N, a positive amount)
    context: str | None = None
    client: str | None = None
    position_s: float | None = None
    duration_s: float | None = None
    note: str | None = None


class InsightsTasteTrackOut(InsightsTasteTrackRow):
    model: Literal["v1", "v2"]
    now: datetime
    starts: int
    finishes: int
    late_skips: int
    contributions: list[InsightsContribution]
    formula: str
    artist: dict[str, Any] | None = None
    rest: dict[str, datetime | None]
    facet_cos: dict[str, float] | None = None  # facet id -> cosine; None without the embedder
    mixes: list[dict[str, Any]]
    waves: list[dict[str, Any]]


class InsightsMapFacet(BaseModel):
    id: str
    weight: float
    artists: list[str]
    x: float  # the facet center on the PCA axes
    y: float
    fx: float  # the anchor of the «Грани» layout
    fy: float
    color_index: int


class InsightsMapPoint(BaseModel):
    key: str
    artist: str = ""
    title: str = ""
    layers: list[Literal["profile", "mix", "wave"]]
    x: float
    y: float
    fx: float
    fy: float
    s: float | None = None
    p: float | None = None
    n: float | None = None
    facet_id: str | None = None
    facet_cos: list[float]  # in the order of `facets`
    seed: bool = False
    liked: bool = False
    disliked: bool = False
    negative: bool = False
    familiar: bool | None = None
    generator: str | None = None
    generator_title: str | None = None
    mix_position: int | None = None
    wave_position: int | None = None
    outcome: str | None = None


class InsightsMapMissing(BaseModel):
    key: str
    artist: str = ""
    title: str = ""
    layers: list[str]


class InsightsMapOut(BaseModel):
    available: bool
    reason: str | None = None  # model_v1 / embedder_off / embedder_down / embedder_timeout / embedder_stale_space
    space_id: str | None = None
    basis: Literal["global", "user"]
    explained: list[float]  # share of the variance of the two axes
    facets: list[InsightsMapFacet]
    points: list[InsightsMapPoint]
    missing: list[InsightsMapMissing]  # no sound vector: listed, not drawn
    truncated: bool
    mix_id: int | None = None
    wave_id: str | None = None
    layout_note: str


class InsightsMatrixOut(BaseModel):
    model: Literal["v1", "v2"]
    by: Literal["track", "artist"]
    now: datetime
    users: list[dict[str, Any]]
    rows: list[dict[str, Any]]  # key, artist, title, owned, cells{user id: {s, p, n, liked, disliked, heard}}...
    total: int
    offset: int


class InsightsMixRow(BaseModel):
    id: int
    created_at: datetime
    closed_at: datetime | None = None
    status: str
    title: str
    mode: str
    mode_title: str
    taste_model: str | None = None
    size: int
    downloaded: int
    launches: int
    started: int
    heard: int
    finished: int
    early_skips: int
    likes: int
    dislikes: int
    top_generators: list[dict[str, Any]]


class InsightsMixOut(InsightsMixRow):
    description: str
    nd_playlist_id: str | None = None
    funnel: dict[str, Any]
    seeds: list[dict[str, Any]]
    seed_labels: list[str] | None = None
    facets: list[dict[str, Any]] | None = None
    maturity: float | None = None
    embedder: Any = None
    build_seconds: float | None = None
    items: list[dict[str, Any]]
    launch_list: list[dict[str, Any]]
    artists: int


class InsightsWaveRow(BaseModel):
    session_id: str
    created_at: datetime
    last_activity_at: datetime
    origin: str | None = None  # null: not recorded (old clients)
    client: str | None = None
    restart_of: str | None = None
    restart: bool
    seed_kind: str  # own/track/track_artist/artist/library_artist/file/unknown
    seed: dict[str, Any] | None = None
    mode: str | None = None
    taste_model: str | None = None
    next_calls: int | None = None
    handed: int | None = None
    tracks_started: int
    started: int
    heard: int
    finished: int
    early_skips: int
    late_skips: int
    likes: int
    dislikes: int
    listened_ms: int
    downloads: int | None = None
    cleared: bool
    reconstructed: bool  # known only by its events: seed and origin unknown


class InsightsWavesOut(BaseModel):
    total: int
    offset: int
    rows: list[InsightsWaveRow]
    launches: int
    restarts: int
    by_origin: dict[str, int]
    by_seed_kind: dict[str, int]


class InsightsWaveOut(InsightsWaveRow):
    gens: dict[str, Any]
    seeds_used: list[str] | None = None
    items: list[dict[str, Any]]
    events: list[dict[str, Any]]
