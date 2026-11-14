"""SQLite schema of the Hub (PLAN.md section 7)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    nd_username: Mapped[str] = mapped_column(String(128), unique=True)
    nd_password_enc: Mapped[str] = mapped_column(Text)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Google account linked to this user ("sub" of its ID token): sign-in with Google in the app
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True)
    google_email: Mapped[str | None] = mapped_column(String(320))
    # the confirmed email the account registered with (lower case): sign-in by it, too
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)


class SearchHistory(Base):
    """The account's recent queries, shared by browsers and the embedded app."""

    __tablename__ = "search_history"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    query_key: Mapped[str] = mapped_column(String(1024), primary_key=True)
    query: Mapped[str] = mapped_column(String(512))
    searched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AssistantDailyUsage(Base):
    """Persistent message allowance, independent of saved chats and LLM token usage."""

    __tablename__ = "assistant_daily_usage"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    messages: Mapped[int] = mapped_column(Integer)


class Invite(Base):
    """One-time registration code the admin hands to a person (FR-1: accounts only by invitation)."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # normalized: upper case, no dashes
    note: Mapped[str] = mapped_column(String(128), default="")  # who it is for
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class EmailCode(Base):
    """A code sent to an email for registration without an invite: whoever reads the letter owns the address."""

    __tablename__ = "email_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)  # lower case
    code_hash: Mapped[str] = mapped_column(String(64))  # HMAC of the email and the code
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # wrong codes typed
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class AiUsage(Base):
    """Every paid AI call of a user (chat turn, screenshot/link/text recognition): the admin's spending report."""

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # chat/images/link/text
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie token
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Album(Base):
    """Album as the Hub manages it. `uid` is written into files as HOMEMUSIC_ALBUM_ID (Navidrome album PID)."""

    __tablename__ = "albums"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(36), unique=True)
    title: Mapped[str] = mapped_column(String(512))
    album_artist: Mapped[str] = mapped_column(String(512))
    year: Mapped[int | None] = mapped_column(Integer)
    release_type: Mapped[str | None] = mapped_column(String(32))  # album/single/ep/compilation
    deezer_album_id: Mapped[str | None] = mapped_column(String(32), index=True)
    upc: Mapped[str | None] = mapped_column(String(32))
    genre: Mapped[str | None] = mapped_column(String(128))
    cover_path: Mapped[str | None] = mapped_column(String(1024))  # relative to library/, cover.jpg in album dir
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Track(Base):
    __tablename__ = "tracks"
    __table_args__ = (Index("ix_tracks_fingerprint", "fingerprint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(36), unique=True)  # HOMEMUSIC_TRACK_ID (Navidrome track PID)
    album_id: Mapped[int | None] = mapped_column(ForeignKey("albums.id", ondelete="SET NULL"), index=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True)  # relative to library/, forward slashes
    nd_song_id: Mapped[str | None] = mapped_column(String(64), index=True)
    artist: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(512))
    album: Mapped[str | None] = mapped_column(String(512))
    album_artist: Mapped[str | None] = mapped_column(String(512))
    track_no: Mapped[int | None] = mapped_column(Integer)
    disc_no: Mapped[int | None] = mapped_column(Integer)
    year: Mapped[int | None] = mapped_column(Integer)
    genre: Mapped[str | None] = mapped_column(String(128))
    duration_s: Mapped[int | None] = mapped_column(Integer)
    bitrate_kbps: Mapped[int | None] = mapped_column(Integer)
    codec: Mapped[str | None] = mapped_column(String(16))
    isrc: Mapped[str | None] = mapped_column(String(16), index=True)
    deezer_track_id: Mapped[str | None] = mapped_column(String(32), index=True)
    explicit: Mapped[bool | None] = mapped_column(Boolean)
    source: Mapped[str | None] = mapped_column(String(32))  # hitmoz/hitmos_fm/ytdlp/upload/manual
    source_url: Mapped[str | None] = mapped_column(String(2048))
    source_upload_date: Mapped[date | None] = mapped_column(Date)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    fingerprint: Mapped[str] = mapped_column(String(1024))  # normalized "artist|title"
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    added_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class CatalogCache(Base):
    __tablename__ = "catalog_cache"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ttl_s: Mapped[int] = mapped_column(Integer)


class DownloadBatch(Base):
    __tablename__ = "download_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    origin: Mapped[str] = mapped_column(String(16))  # search/catalog/assistant
    title: Mapped[str | None] = mapped_column(String(512))
    target_playlist_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


ITEM_STATUSES = (
    "queued", "searching", "downloading", "tagging", "importing", "done", "failed", "needs_review", "cancelled",
)


class DownloadItem(Base):
    __tablename__ = "download_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("download_batches.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    query_json: Mapped[dict[str, Any]] = mapped_column(JSON)  # TrackQuery + catalog refs (deezer ids, album info)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0)
    provider: Mapped[str | None] = mapped_column(String(32))
    chosen_candidate_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    candidates_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"))
    force: Mapped[bool] = mapped_column(Boolean, default=False)  # explicit re-download (new file next to locked one)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_pick", "status", "run_after"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/running/done/failed
    # higher first; imports run below 0 (`queue.PRIORITY_IMPORT`) and never take every worker
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    staging_path: Mapped[str] = mapped_column(String(1024))  # relative to staging/
    orig_name: Mapped[str] = mapped_column(String(512))
    size: Mapped[int] = mapped_column(Integer, default=0)
    probed_tags_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="uploaded")  # uploaded/imported/failed/discarded
    error: Mapped[str | None] = mapped_column(Text)
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AssistantTask(Base):
    __tablename__ = "assistant_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    input_kind: Mapped[str] = mapped_column(String(16))  # images/link/text
    input_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # image paths / url / text
    extraction_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    resolution_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    # queued/extracting/resolving/awaiting_confirmation/confirmed/done/failed
    confirm_token: Mapped[str | None] = mapped_column(String(64))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("download_batches.id", ondelete="SET NULL"))
    error: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AssistantChat(Base):
    """A saved conversation with the assistant: the messages as the PWA shows them (no images, only their count)."""

    __tablename__ = "assistant_chats"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    # [{"role", "content", "images"?, "cost"?, "done"?, "proposal"?: {task_id, token, summary, actions, state}}]
    messages: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PendingPlaylistAdd(Base):
    __tablename__ = "pending_playlist_adds"

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    download_item_id: Mapped[int] = mapped_column(ForeignKey("download_items.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    applied_song_id: Mapped[str | None] = mapped_column(String(64))  # anchor while an earlier item needs review
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Import(Base):
    """Moving a user's collection over from a music service (Yandex Music now; Spotify, YouTube Music later)."""

    __tablename__ = "imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16))  # yandex
    method: Mapped[str] = mapped_column(String(16))  # device/token/profile
    # auth/fetching/preview/running/done/failed/cancelled
    status: Mapped[str] = mapped_column(String(16), default="fetching")
    mode: Mapped[str] = mapped_column(String(16), default="economy")  # economy/full
    account: Mapped[str | None] = mapped_column(String(256))  # login or profile shown to the user
    token_enc: Mapped[str | None] = mapped_column(Text)  # service token (Fernet)
    keep_token: Mapped[bool] = mapped_column(Boolean, default=False)  # kept after the import for «Обновить»
    auth_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # sign-in in progress (device code)
    options_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # download_albums...
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ImportCollection(Base):
    """«Мне нравится», a playlist, liked albums / artists, dislikes of an import."""

    __tablename__ = "import_collections"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # likes/playlist/albums/artists/dislikes
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    position: Mapped[int] = mapped_column(Integer, default=0)
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    cover_url: Mapped[str | None] = mapped_column(String(1024))
    nd_playlist_id: Mapped[str | None] = mapped_column(String(64))
    nd_song_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)  # what the Navidrome playlist holds now


class ImportItem(Base):
    """A track (or an album / artist) of an import collection, in the service's order."""

    __tablename__ = "import_items"
    __table_args__ = (Index("ix_import_items_ref", "ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), index=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("import_collections.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    external_id: Mapped[str] = mapped_column(String(128))
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # artist, title, album, duration_s...
    ref: Mapped[str | None] = mapped_column(String(64))  # catalog track / album / artist
    match: Mapped[str | None] = mapped_column(String(16))  # owned/exact/probable/not_found
    score: Mapped[float | None] = mapped_column(Float)
    # pending/waiting/queued/done/failed/not_found/skipped
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"))
    download_item_id: Mapped[int | None] = mapped_column(ForeignKey("download_items.id", ondelete="SET NULL"),
                                                         index=True)


class SourceMap(Base):
    """What an id of a music service turned out to be in the catalog: shared by every import of every user."""

    __tablename__ = "source_map"

    source: Mapped[str] = mapped_column(String(16), primary_key=True)  # yandex
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)  # track/album/artist
    external_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    ref: Mapped[str | None] = mapped_column(String(64))
    match: Mapped[str] = mapped_column(String(16))  # exact/probable/not_found
    score: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PlayEvent(Base):
    """One listening event of a user (MIXES.md 3.2): start/skip/finish of a track, ♥, «Не нравится». From the app,
    the PWA's own player, or Navidrome's scrobble history (`client` "navidrome")."""

    __tablename__ = "play_events"
    __table_args__ = (
        UniqueConstraint("user_id", "event_id", name="uq_play_events_user_event"),
        Index("ix_play_events_user_ts", "user_id", "ts"),
        Index("ix_play_events_song", "nd_song_id"),
        Index("ix_play_events_user_target_ts", "user_id", "target_type", "ts", "id"),
        Index("ix_play_events_playback", "user_id", "device_id", "playback_id"),
        Index("ix_play_events_user_launch", "user_id", "launch_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    event_id: Mapped[str] = mapped_column(String(64))  # the client's id: a resent event is not stored twice
    ts: Mapped[datetime] = mapped_column(DateTime)  # UTC
    kind: Mapped[str] = mapped_column(String(16))  # start/skip/finish/like/unlike/dislike/undislike
    ref: Mapped[str | None] = mapped_column(String(64))  # catalog track
    nd_song_id: Mapped[str | None] = mapped_column(String(64))  # Navidrome song
    target_type: Mapped[str] = mapped_column(String(16), default="track", server_default="track")
    nd_album_id: Mapped[str | None] = mapped_column(String(64), index=True)
    nd_artist_id: Mapped[str | None] = mapped_column(String(64), index=True)
    target_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reverts_event_id: Mapped[str | None] = mapped_column(String(64))
    mirror_pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    playback_id: Mapped[str | None] = mapped_column(String(64))
    device_id: Mapped[str | None] = mapped_column(String(64))
    listened_ms: Mapped[int | None] = mapped_column()
    track_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    superseded_scrobble: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    position_s: Mapped[float | None] = mapped_column(Float)
    duration_s: Mapped[float | None] = mapped_column(Float)
    context: Mapped[str | None] = mapped_column(String(128))  # mix:<id>, wave:<session>, album, playlist, search...
    client: Mapped[str] = mapped_column(String(32))  # app/<version>, pwa, navidrome
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # analytics (INSIGHTS.md 6.5): every event of one launched list shares `launch_id` ("wave:<sid>" for a wave);
    # `launch_origin` — where it was launched. The taste profile never reads them.
    launch_id: Mapped[str | None] = mapped_column(String(64))
    launch_origin: Mapped[str | None] = mapped_column(String(32))


class FavoriteSubject(Base):
    """Verified aliases and metadata, never the user's favorite state."""

    __tablename__ = "favorite_subjects"
    __table_args__ = (UniqueConstraint("target_type", "canonical_key", name="uq_favorite_subject_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    target_type: Mapped[str] = mapped_column(String(16))
    canonical_key: Mapped[str] = mapped_column(String(128))
    ref: Mapped[str | None] = mapped_column(String(64), index=True)
    nd_id: Mapped[str | None] = mapped_column(String(64), index=True)
    aliases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enriched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Dislike(Base):
    """«Не нравится»: a track (`ref` and/or `nd_song_id`) or an artist (`artist_ref`) the user doesn't want to hear."""

    __tablename__ = "dislikes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ref: Mapped[str | None] = mapped_column(String(64))
    nd_song_id: Mapped[str | None] = mapped_column(String(64))
    artist_ref: Mapped[str | None] = mapped_column(String(64))
    artist: Mapped[str | None] = mapped_column(String(512))  # for showing the list
    title: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(16))  # app/pwa/yandex_import
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NavidromeSync(Base):
    """How far the Hub has read a user's Navidrome scrobble history (`app.plays.import_scrobbles`)."""

    __tablename__ = "navidrome_sync"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    last_scrobble_id: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    favorites_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    favorites_bootstrapped_at: Mapped[datetime | None] = mapped_column(DateTime)



class MixSettings(Base):
    """A user's «Микс дня» settings (MIXES.md 6): mode, the hour of the build, the playlist, onboarding artists."""

    __tablename__ = "mix_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default="normal")  # normal/favorites/discover
    mix_hour: Mapped[int] = mapped_column(Integer, default=5)  # local time (`Settings.timezone`)
    nd_playlist_id: Mapped[str | None] = mapped_column(String(64))  # «Микс дня» in Navidrome: one id for good
    # onboarding: [{ref, name}] the user picked on «Что вы слушаете?» (a new user without history)
    artists_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    last_build_on: Mapped[date | None] = mapped_column(Date)  # local date of the last scheduled build attempt
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Mix(Base):
    """One «Микс дня» of a user: lives until the next one (MIXES.md 6)."""

    __tablename__ = "mixes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16))
    nd_playlist_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/closed
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("download_batches.id", ondelete="SET NULL"))
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # generators, timings (the report)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)


class MixItem(Base):
    """A song of a mix: one of the 50 (`state` "active") or the reserve that replaces one that did not download."""

    __tablename__ = "mix_items"
    __table_args__ = (Index("ix_mix_items_mix", "mix_id", "state", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mix_id: Mapped[int] = mapped_column(ForeignKey("mixes.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)  # order in the mix; the reserve after the 50, best first
    state: Mapped[str] = mapped_column(String(16), default="active")  # active/reserve/failed
    familiar: Mapped[bool] = mapped_column(Boolean, default=False)
    ref: Mapped[str | None] = mapped_column(String(64), index=True)  # catalog track
    nd_song_id: Mapped[str | None] = mapped_column(String(64), index=True)
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"), index=True)
    generator: Mapped[str] = mapped_column(String(32))  # the strongest one: favorites/stash/ytm_radio/...
    score: Mapped[float] = mapped_column(Float, default=0)
    # the catalog track (`CatalogTrack`) and what scored it: generators, sim, popularity
    features_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    downloaded_for_mix: Mapped[bool] = mapped_column(Boolean, default=False)
    download_item_id: Mapped[int | None] = mapped_column(ForeignKey("download_items.id", ondelete="SET NULL"),
                                                         index=True)


class WaveSession(Base):
    """One «Моя волна» of a user (MIXES.md 7): the endless stream the Hub picks 5 tracks at a time. `state_json` —
    the pool of candidates, what was handed out and played, the session's skips and likes (`app.mixes.wave`)."""

    __tablename__ = "wave_sessions"
    __table_args__ = (Index("ix_wave_sessions_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    mode: Mapped[str] = mapped_column(String(16), default="normal")  # normal/favorites/discover, as a mix's
    seed_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # {ref|nd_song_id|artist_ref, title}: «по треку»
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("download_batches.id", ondelete="SET NULL"))
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # analytics (INSIGHTS.md 6.2–6.3): where the wave was started, by what client, the raw request; a session the
    # app made again after a 404 names the old one (`origin` "restart"). Never read by the wave itself.
    origin: Mapped[str | None] = mapped_column(String(32))
    client: Mapped[str | None] = mapped_column(String(32))
    restart_of: Mapped[str | None] = mapped_column(String(32))
    taste_model: Mapped[str | None] = mapped_column(String(8))
    request_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    next_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # an old session's heavy state (pool, queue, played) was replaced by a summary: the row itself is kept
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime)


class WaveItem(Base):
    """A track a wave handed out (INSIGHTS.md 6.4), once per session: analytics only, the wave never reads it."""

    __tablename__ = "wave_items"
    __table_args__ = (UniqueConstraint("session_id", "ref", name="uq_wave_items_session_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("wave_sessions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    first_handed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_handed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    handed_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    position: Mapped[int] = mapped_column(Integer)  # the order of first hand-outs in the session, from 0
    ref: Mapped[str] = mapped_column(String(64))
    nd_song_id: Mapped[str | None] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(64))  # the vector key (nd:<song> or the ref)
    artist: Mapped[str] = mapped_column(String(256), default="", server_default="")
    title: Mapped[str] = mapped_column(String(256), default="", server_default="")
    generator: Mapped[str] = mapped_column(String(32))  # `wave.top_generator`
    gens: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # generator -> best position in its list
    familiar: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    score: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    sim: Mapped[float | None] = mapped_column(Float)
    value: Mapped[float | None] = mapped_column(Float)  # `wave.value` the pick ranked it by (none: the seed)
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class SmartSession(Base):
    """One run of the smart shuffle of a playlist or of «Понравившиеся» (MIXES.md 8): the order of the own songs
    and the recommendations the Hub put between them. `state_json` — what the session needs to go on after a
    restart (`app.mixes.smart`); the rest is for the analytics."""

    __tablename__ = "smart_sessions"
    __table_args__ = (Index("ix_smart_sessions_user_context", "user_id", "context", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    context: Mapped[str] = mapped_column(String(80))  # "liked" or "playlist:<Navidrome id>"
    title: Mapped[str | None] = mapped_column(String(256))  # the playlist's name as the client sent it
    own_count: Mapped[int] = mapped_column(Integer, default=0)
    per_rec: Mapped[int] = mapped_column(Integer, default=3)  # own songs per recommendation
    first_id: Mapped[str | None] = mapped_column(String(80))  # the song it started from (tapped or playing)
    taste_model: Mapped[str | None] = mapped_column(String(8))
    origin: Mapped[str | None] = mapped_column(String(32))
    client: Mapped[str | None] = mapped_column(String(32))
    next_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SmartItem(Base):
    """A recommendation a smart shuffle handed out, once per session (analytics; `state` follows the user)."""

    __tablename__ = "smart_items"
    __table_args__ = (UniqueConstraint("session_id", "ref", name="uq_smart_items_session_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("smart_sessions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    handed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    position: Mapped[int] = mapped_column(Integer)  # the order of hand-outs in the session, from 0
    slot: Mapped[int] = mapped_column(Integer)  # which recommendation place of the list it took
    ref: Mapped[str] = mapped_column(String(64))
    nd_song_id: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(1024), default="", server_default="")
    artist: Mapped[str] = mapped_column(String(256), default="", server_default="")
    title: Mapped[str] = mapped_column(String(256), default="", server_default="")
    generator: Mapped[str] = mapped_column(String(32))
    gens: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    familiar: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    score: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    sim: Mapped[float | None] = mapped_column(Float)  # closeness by sound to the playlist's center
    value: Mapped[float | None] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(12), default="handed", server_default="handed")  # handed/added/hidden
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class SmartHidden(Base):
    """«Не рекомендовать в этом плейлисте»: a track the user hid from the recommendations of one context. Not the
    global «Не нравится» (`Dislike`): the track stays everywhere else."""

    __tablename__ = "smart_hidden"
    __table_args__ = (UniqueConstraint("user_id", "context", "fingerprint", name="uq_smart_hidden"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    context: Mapped[str] = mapped_column(String(80))
    fingerprint: Mapped[str] = mapped_column(String(1024))
    ref: Mapped[str | None] = mapped_column(String(64))
    nd_song_id: Mapped[str | None] = mapped_column(String(64))
    artist: Mapped[str] = mapped_column(String(256), default="", server_default="")
    title: Mapped[str] = mapped_column(String(256), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
