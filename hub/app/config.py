"""Settings: secrets and paths from env (.env), tunables from config.yaml."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SourcesCfg(BaseModel):
    # Russian tracks: official YouTube Music copies are often edited since the 2026 law, so they come last,
    # after SoundCloud and re-uploads by small channels
    ru: list[str] = ["hitmoz", "hitmos_fm", "soundcloud", "yt_reupload", "ytdlp"]
    # hitmoz first: a YouTube file opens only after the ad before the video (`ytdlp.MAX_AD_WAIT_S`)
    default: list[str] = ["hitmoz", "ytdlp"]
    # Tried only by downloads, after regular sources fail; never part of page prefetch.
    download_fallback: list[str] = ["soundcloud"]
    # re-uploads by small channels: an explicit Russian track takes one when no copy above is known to be uncensored
    reuploads: list[str] = ["yt_reupload", "soundcloud"]
    # a source's files uploaded on or after the date are not taken by listening and downloads (the Files tab shows them)
    uploaded_before: dict[str, date] = {"soundcloud": date(2026, 5, 1)}


class HitmozCfg(BaseModel):
    domains: list[str] = ["ru.hitmoz.org", "rus.hitmotop.com", "hitmos.me"]
    accept_language: str = "ru-RU,ru;q=0.9"
    rate_per_sec: float = 1.0
    max_parallel_downloads: int = 2


class HitmosFmCfg(BaseModel):
    domains: list[str] = ["rus.hitmos.fm"]
    rate_per_sec: float = 1.0


class YtdlpCfg(BaseModel):
    prefer_original_codec: bool = True
    search_prefix: str = "ytsearch5:"
    max_parallel: int = Field(default=2, ge=1, le=4)
    sleep_requests_s: float = Field(default=1.0, ge=0)
    sleep_downloads_s: float = Field(default=5.0, ge=0)
    refusal_cooldown_s: float = Field(default=900, ge=0)


class MatchingCfg(BaseModel):
    artist_min_ratio: float = 0.90
    duration_tolerance_s: int = 3
    auto_threshold: float = 0.90
    auto_margin: float = 0.05
    confirm_threshold: float = 0.65
    enrich_top_n: int = 3
    original_before: date = date(2026, 3, 1)
    reject_words: list[str] = [
        "караоке", "минус", "минусовка", "instrumental", "инструментал", "кавер", "cover",
        "remix", "ремикс", "slowed", "speed up", "sped up", "nightcore", "8d",
    ]


class PostcheckCfg(BaseModel):
    silence_warn_s: float = 0.7


class RateCfg(BaseModel):
    requests: int = 45
    per_seconds: float = 5


class CatalogCfg(BaseModel):
    deezer_rate: RateCfg = RateCfg()
    ttl_artist_s: int = 7 * 86400
    ttl_album_s: int = 7 * 86400
    ttl_search_s: int = 86400
    musicbrainz_user_agent: str = "HomeMusic/0.1 (personal music server)"


class JobsCfg(BaseModel):
    workers: int = 3
    max_attempts: int = 5


class UploadsCfg(BaseModel):
    max_file_mb: int = Field(default=500, ge=1, le=500)
    max_total_mb: int = Field(default=500, ge=1, le=500)
    max_files: int = Field(default=100, ge=1, le=100)
    extensions: list[str] = ["mp3", "m4a", "aac", "flac", "ogg", "opus", "wav"]


class LibraryCfg(BaseModel):
    trash_days: int = 30
    backup_hour: int = 4


class MixesCfg(BaseModel):
    """«Микс дня» (MIXES.md 6)."""

    size: int = 50
    reserve: int = 20  # taken beyond `size`: a song that did not download is replaced by the next one
    default_hour: int = 5
    seeds: int = 8
    max_per_artist: int = 2
    min_duration_s: int = 90
    max_duration_s: int = 600
    taste_model: Literal["v1", "v2"] = "v1"
    taste_shadow: bool = False
    # generator weights in the score (`app.mixes.build`)
    weights: dict[str, float] = {
        "favorites": 1.0, "stash": 0.8, "ytm_radio": 1.0, "lastfm_similar": 0.8, "deezer_radio": 0.7,
        "yandex_similar": 0.8, "similar_artists": 0.6, "new_releases": 0.7, "yandex_wave": 1.0,
    }


class AnalyticsCfg(BaseModel):
    """The admin's insights (INSIGHTS.md 7)."""

    # Hub user names (`nd_username`) left out of the totals and the taste matrix by default (a QA account)
    exclude_users: list[str] = []


class AppConfig(BaseModel):
    """Contents of config.yaml."""

    sources: SourcesCfg = SourcesCfg()
    hitmoz: HitmozCfg = HitmozCfg()
    hitmos_fm: HitmosFmCfg = HitmosFmCfg()
    ytdlp: YtdlpCfg = YtdlpCfg()
    matching: MatchingCfg = MatchingCfg()
    postcheck: PostcheckCfg = PostcheckCfg()
    catalog: CatalogCfg = CatalogCfg()
    jobs: JobsCfg = JobsCfg()
    uploads: UploadsCfg = UploadsCfg()
    library: LibraryCfg = LibraryCfg()
    mixes: MixesCfg = MixesCfg()
    analytics: AnalyticsCfg = AnalyticsCfg()


class Settings(BaseSettings):
    """Environment (.env). Paths are container paths."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    music_root: Path = Path("/music")
    data_dir: Path = Path("/data")
    config_file: Path = Path("/config/config.yaml")
    static_dir: Path = Path(__file__).parent / "static"

    navidrome_url: str = "http://navidrome:4533"
    navidrome_admin_user: str = "admin"
    navidrome_admin_password: str = ""

    public_music_url: str = ""
    public_hub_url: str = ""

    hub_secret_key: str = ""
    session_days: int = 60
    # OAuth client id ("Web application" type) of the Google Cloud project for "Sign in with Google" in the app;
    # empty — no Google sign-in (README, «Вход через Google»)
    google_client_id: str = ""
    invite_days: int = 7
    # Registration by email, without an invite: a code in a letter, sent over SMTP of any mailbox (Gmail with an
    # app password is free; README, «Регистрация по email»). Empty host — no email registration
    smtp_host: str = ""
    smtp_port: int = 587  # 465 — TLS from the start, otherwise STARTTLS
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""  # the sender's address; empty — SMTP_USER
    # letters a day at most (Gmail sends about 500): past it registration asks to try tomorrow
    email_daily_limit: int = 450
    email_code_minutes: int = 15
    log_level: str = "INFO"

    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "anthropic/claude-haiku-5.5"
    llm_price_in: float = 0.10
    llm_price_out: float = 0.50
    llm_timeout_s: float = 90

    # Last.fm API (similar tracks/artists, tags for mix names): https://www.last.fm/api/account/create
    lastfm_api_key: str = ""
    # the audio vector service (MIXES.md 4.2) in the compose network; empty, or it doesn't answer — mixes are built
    # without the audio term
    embedder_url: str = "http://embedder:8090"
    embedder_token: str = ""
    # how long a mix build waits for the embedder to embed new candidates, and how often it asks (tests set 0)
    mix_embed_wait_s: float = 180
    mix_poll_s: float = 5
    # «Моя волна»: how long a request waits for its sources (the first tracks, the radio of a liked track); what
    # comes later joins the pool in the background
    wave_wait_s: float = 8
    # local time of the users (the hour a mix is built at); the container itself runs in UTC
    timezone: str = "Europe/Moscow"

    outbound_proxy: str = ""
    outbound_proxy_hosts: str = ""

    # set by tests to skip background workers / scheduled tasks
    disable_background: bool = False
    # how often an import polls Navidrome for the end of its scan (tests set 0)
    nd_scan_poll_s: float = 1.0

    cfg: AppConfig = Field(default_factory=AppConfig)

    @property
    def library_dir(self) -> Path:
        return self.music_root / "library"

    @property
    def staging_dir(self) -> Path:
        return self.music_root / "staging"

    @property
    def trash_dir(self) -> Path:
        return self.music_root / ".trash"

    @property
    def backups_dir(self) -> Path:
        return self.music_root / ".backups"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hub.sqlite3"

    @property
    def proxy_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.outbound_proxy_hosts.split(",") if h.strip()}


def load_app_config(path: Path) -> AppConfig:
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return AppConfig.model_validate(data)
    return AppConfig()


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.cfg = load_app_config(s.config_file)
    return s
