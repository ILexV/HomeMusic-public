//! Hub session for likes, dislikes, catalog search, and downloads.
//! The Navidrome password is sent only to `/api/auth/login` and is never put in an error.

use std::collections::HashSet;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use serde_json::Value;

const CLIENT: &str = "desktop";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Marks {
    pub songs: HashSet<String>,
    pub albums: HashSet<String>,
    pub disliked: HashSet<String>,
}

impl Default for Marks {
    fn default() -> Self {
        Self {
            songs: HashSet::new(),
            albums: HashSet::new(),
            disliked: HashSet::new(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HitKind {
    Track,
    Album,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CatalogHit {
    pub kind: HitKind,
    pub reference: String,
    pub nd_song_id: String,
    pub title: String,
    pub artist: String,
    pub album: String,
    pub detail: String,
    pub duration_s: u32,
    pub owned: bool,
    pub downloading: bool,
}

impl CatalogHit {
    pub fn action(&self) -> &'static str {
        match self.kind {
            HitKind::Album if self.owned => "открыть",
            HitKind::Album if self.downloading => "качается",
            HitKind::Album => "скачать",
            HitKind::Track if self.owned => "играть",
            HitKind::Track if self.downloading => "качается",
            HitKind::Track => "скачать",
        }
    }
}

pub struct Hub {
    base: String,
    user: String,
    password: String,
    cookie: Mutex<String>,
    is_admin: AtomicBool,
}

impl Hub {
    pub fn from_config(config: &crate::navidrome::ServerConfig) -> Self {
        Self {
            base: hub_base(&config.server, &config.hub),
            user: config.user.clone(),
            password: config.password.clone(),
            cookie: Mutex::new(String::new()),
            is_admin: AtomicBool::new(false),
        }
    }

    pub fn load_marks(&self) -> Result<Marks, String> {
        let starred = self.send("GET", "/api/starred", None)?;
        let mut marks = parse_starred(&starred);
        if let Ok(dislikes) = self.send("GET", "/api/dislikes", None) {
            marks.disliked = parse_dislike_ids(&dislikes);
        }
        Ok(marks)
    }

    pub fn star_song(&self, id: &str, unstar: bool) -> Result<(), String> {
        self.send("POST", "/api/star", Some(star_body(&[id], &[], unstar)))?;
        Ok(())
    }

    pub fn star_album(&self, id: &str, unstar: bool) -> Result<(), String> {
        self.send("POST", "/api/star", Some(star_body(&[], &[id], unstar)))?;
        Ok(())
    }

    pub fn dislike_song(&self, id: &str, artist: &str, title: &str) -> Result<(), String> {
        self.send(
            "POST",
            "/api/dislikes",
            Some(serde_json::json!({
                "nd_song_id": id,
                "artist": artist,
                "title": title,
                "client": CLIENT,
            })),
        )?;
        Ok(())
    }

    pub fn undislike_song(&self, id: &str) -> Result<(), String> {
        let path = format!("/api/dislikes?nd_song_id={}", encode(id));
        self.send("DELETE", &path, None)?;
        Ok(())
    }

    pub fn search(&self, query: &str) -> Result<Vec<CatalogHit>, String> {
        let path = format!("/api/search?q={}", encode(query.trim()));
        let body = self.send("GET", &path, None)?;
        Ok(parse_search(&body))
    }

    pub fn download_track(&self, reference: &str) -> Result<String, String> {
        let body = self.send(
            "POST",
            "/api/downloads",
            Some(serde_json::json!({ "track_refs": [reference] })),
        )?;
        Ok(download_summary(&body))
    }

    pub fn download_album(&self, reference: &str) -> Result<String, String> {
        let body = self.send(
            "POST",
            "/api/downloads",
            Some(serde_json::json!({ "album_ref": reference })),
        )?;
        Ok(download_summary(&body))
    }

    pub fn home(&self) -> Result<HomeFeed, String> {
        Ok(parse_home(&self.send("GET", "/api/nd/home", None)?))
    }

    pub fn mix_today(&self) -> Result<MixSnapshot, String> {
        let mut mix = parse_mix_today(&self.send("GET", "/api/mixes/today", None)?);
        mix.can_rebuild = self.is_admin.load(Ordering::Relaxed);
        Ok(mix)
    }

    pub fn set_mix_mode(&self, mode: &str) -> Result<MixSettings, String> {
        let value = self.send(
            "PUT",
            "/api/mixes/settings",
            Some(serde_json::json!({ "mode": mode })),
        )?;
        Ok(parse_mix_settings(&value))
    }

    pub fn rebuild_mix(&self) -> Result<bool, String> {
        let value = self.send("POST", "/api/mixes/rebuild", Some(serde_json::json!({})))?;
        Ok(value.get("queued").and_then(Value::as_bool).unwrap_or(false))
    }


    pub fn playlists(&self) -> Result<Vec<PlaylistLine>, String> {
        Ok(parse_playlists(&self.send("GET", "/api/playlists", None)?))
    }

    pub fn liked(&self) -> Result<LikedPage, String> {
        Ok(parse_liked(&self.send("GET", "/api/nd/liked", None)?))
    }

    pub fn taste(&self, query: &str) -> Result<TastePage, String> {
        let path = format!("/api/taste/onboarding?q={}", encode(query.trim()));
        Ok(parse_taste(&self.send("GET", &path, None)?))
    }

    pub fn pick_taste(&self, refs: &[String]) -> Result<MixSettings, String> {
        let value = self.send(
            "POST",
            "/api/taste/artists",
            Some(serde_json::json!({ "artist_refs": refs })),
        )?;
        Ok(parse_mix_settings(&value))
    }

    /// The user's own wave when `artist_id` is empty. `origin` is `home_card` or `radio`.
    pub fn wave_start(&self, artist_id: &str, origin: &str) -> Result<WaveBatch, String> {
        let seed = if artist_id.is_empty() {
            Value::Null
        } else {
            serde_json::json!({ "nd_artist_id": artist_id })
        };
        let body = self.send(
            "POST",
            "/api/wave/start",
            Some(serde_json::json!({
                "seed": seed,
                "origin": origin,
                "client": CLIENT,
            })),
        )?;
        Ok(parse_wave(&body))
    }

    pub fn wave_next(
        &self,
        session_id: &str,
        played: &[Value],
        queued: &[String],
    ) -> Result<WaveBatch, String> {
        let path = format!("/api/wave/{}/next", encode(session_id));
        let body = self.send(
            "POST",
            &path,
            Some(serde_json::json!({
                "played": played,
                "queued": queued,
            })),
        )?;
        Ok(parse_wave(&body))
    }

    /// Stream URL for a catalog ref. The session cookie is not part of the URL.
    pub fn listen_url(&self, reference: &str) -> String {
        if self.base.is_empty() || reference.is_empty() {
            return String::new();
        }
        format!("{}/api/listen/stream?ref={}", self.base, encode(reference))
    }

    pub fn session_token(&self) -> String {
        self.cookie_value()
    }

    fn send(&self, method: &str, path: &str, body: Option<Value>) -> Result<Value, String> {
        if self.base.is_empty() {
            return Err("Хаб: нет адреса".into());
        }
        let mut relogin = false;
        loop {
            if self.cookie_value().is_empty() {
                self.login()?;
            }
            let url = format!("{}{path}", self.base);
            let request = match method {
                "POST" => agent().post(&url),
                "PUT" => agent().put(&url),
                "DELETE" => agent().delete(&url),
                _ => agent().get(&url),
            };
            let request = request.set("Cookie", &format!("hm_session={}", self.cookie_value()));
            let result = if let Some(body) = &body {
                request.send_json(body.clone())
            } else {
                request.call()
            };
            match result {
                Ok(response) => {
                    let text = response.into_string().unwrap_or_default();
                    if text.trim().is_empty() {
                        return Ok(Value::Null);
                    }
                    return serde_json::from_str(&text).map_err(|_| "Хаб: ответ не json".into());
                }
                Err(ureq::Error::Status(401, response)) if !relogin => {
                    let _ = response.into_string();
                    self.set_cookie(String::new());
                    relogin = true;
                }
                Err(err) => return Err(http_err(err)),
            }
        }
    }

    fn login(&self) -> Result<(), String> {
        if self.user.is_empty() || self.password.is_empty() {
            return Err("Хаб: нет логина".into());
        }
        let response = agent()
            .post(&format!("{}/api/auth/login", self.base))
            .send_json(serde_json::json!({
                "username": self.user,
                "password": self.password,
            }))
            .map_err(http_err)?;
        let header = response.header("set-cookie").unwrap_or("").to_string();
        let body: Value = response.into_json().map_err(|_| "Хаб: ответ не json".to_string())?;
        self.is_admin.store(body.get("is_admin").and_then(Value::as_bool).unwrap_or(false), Ordering::Relaxed);
        let cookie = session_cookie(&header).ok_or_else(|| "Хаб: нет сессии".to_string())?;
        self.set_cookie(cookie);
        Ok(())
    }

    fn cookie_value(&self) -> String {
        self.cookie
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
            .clone()
    }

    fn set_cookie(&self, value: String) {
        *self
            .cookie
            .lock()
            .unwrap_or_else(|poison| poison.into_inner()) = value;
    }
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(5))
        .timeout_read(Duration::from_secs(25))
        .build()
}

pub fn hub_base(server: &str, hub: &str) -> String {
    let hub = hub.trim().trim_end_matches('/');
    if !hub.is_empty() {
        return hub.to_string();
    }
    let server = server.trim().trim_end_matches('/');
    let (scheme, rest) = if let Some(rest) = server.strip_prefix("https://") {
        ("https", rest)
    } else if let Some(rest) = server.strip_prefix("http://") {
        ("http", rest)
    } else {
        return String::new();
    };
    let host = rest.split('/').next().unwrap_or(rest);
    let host = host.split(':').next().unwrap_or(host);
    if host.is_empty() {
        return String::new();
    }
    format!("{scheme}://{host}:8080")
}

pub fn session_cookie(header: &str) -> Option<String> {
    for part in header.split(';') {
        let part = part.trim();
        let (name, value) = part.split_once('=')?;
        if name.eq_ignore_ascii_case("hm_session") && !value.is_empty() {
            return Some(value.to_string());
        }
    }
    None
}

pub fn star_body(song_ids: &[&str], album_ids: &[&str], unstar: bool) -> Value {
    serde_json::json!({
        "song_ids": song_ids,
        "album_ids": album_ids,
        "unstar": unstar,
        "client": CLIENT,
    })
}

pub fn parse_starred(value: &Value) -> Marks {
    let ids = |name: &str| {
        value
            .get(name)
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter_map(Value::as_str)
                    .filter(|id| !id.is_empty())
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default()
    };
    Marks {
        songs: ids("song_ids"),
        albums: ids("album_ids"),
        disliked: HashSet::new(),
    }
}

pub fn parse_dislike_ids(value: &Value) -> HashSet<String> {
    value
        .as_array()
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row.get("nd_song_id").and_then(Value::as_str))
                .filter(|id| !id.is_empty())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

pub fn parse_search(value: &Value) -> Vec<CatalogHit> {
    let mut hits = Vec::new();
    let mut seen = HashSet::new();
    if let Some(albums) = value.get("albums").and_then(Value::as_array) {
        for album in albums.iter().take(8) {
            let Some(hit) = album_hit(album) else {
                continue;
            };
            if seen.insert(hit.reference.clone()) {
                hits.push(hit);
            }
        }
    }
    let mut tracks = Vec::new();
    if let Some(rows) = value.get("tracks").and_then(Value::as_array) {
        tracks.extend(rows.iter().take(25));
    }
    if tracks.is_empty() {
        if let Some(rows) = value.get("library").and_then(Value::as_array) {
            tracks.extend(rows.iter().take(25));
        }
    }
    for track in tracks {
        let Some(hit) = track_hit(track) else {
            continue;
        };
        let key = if hit.nd_song_id.is_empty() {
            hit.reference.clone()
        } else {
            hit.nd_song_id.clone()
        };
        if seen.insert(key) {
            hits.push(hit);
        }
    }
    hits
}

fn album_hit(album: &Value) -> Option<CatalogHit> {
    let reference = text(album, "ref");
    if reference.is_empty() {
        return None;
    }
    let count = album.get("nb_tracks").and_then(Value::as_u64).unwrap_or(0);
    let owned = album.get("owned_status").and_then(Value::as_str) == Some("full");
    Some(CatalogHit {
        kind: HitKind::Album,
        reference,
        nd_song_id: String::new(),
        title: text(album, "title"),
        artist: text(album, "artist"),
        album: String::new(),
        detail: if count == 0 {
            "альбом".into()
        } else {
            format!("{count} треков")
        },
        duration_s: 0,
        owned,
        downloading: false,
    })
}

fn track_hit(track: &Value) -> Option<CatalogHit> {
    let reference = text(track, "ref");
    let nd_song_id = text(track, "nd_song_id");
    if reference.is_empty() && nd_song_id.is_empty() {
        return None;
    }
    let duration_s = track
        .get("duration_s")
        .and_then(Value::as_u64)
        .unwrap_or(0)
        .min(u64::from(u32::MAX)) as u32;
    let album = text(track, "album_title");
    let album = if album.is_empty() {
        text(track, "album")
    } else {
        album
    };
    let detail = match (duration_s, album.is_empty()) {
        (0, true) => String::new(),
        (0, false) => album.clone(),
        (seconds, true) => clock(seconds),
        (seconds, false) => format!("{} · {album}", clock(seconds)),
    };
    Some(CatalogHit {
        kind: HitKind::Track,
        reference,
        nd_song_id,
        title: {
            let title = text(track, "title");
            if title.is_empty() {
                "Без названия".into()
            } else {
                title
            }
        },
        artist: text(track, "artist"),
        album,
        detail,
        duration_s,
        owned: track.get("owned").and_then(Value::as_bool).unwrap_or(false)
            || !text(track, "nd_song_id").is_empty() && track.get("owned").is_none(),
        downloading: track
            .get("downloading")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    })
}

pub fn download_summary(value: &Value) -> String {
    let queued = value.get("queued").and_then(Value::as_u64).unwrap_or(0);
    let owned = value
        .get("already_owned")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    if queued == 0 && owned > 0 {
        "Уже в библиотеке".into()
    } else if queued == 0 {
        "Загрузка не запустилась".into()
    } else if queued == 1 {
        "В загрузке: 1".into()
    } else {
        format!("В загрузке: {queued}")
    }
}

fn text(value: &Value, name: &str) -> String {
    value
        .get(name)
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string()
}

fn clock(seconds: u32) -> String {
    format!("{}:{:02}", seconds / 60, seconds % 60)
}

fn encode(text: &str) -> String {
    let mut out = String::new();
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

fn http_err(err: ureq::Error) -> String {
    match err {
        ureq::Error::Status(code, response) => {
            let body = response.into_string().unwrap_or_default();
            let detail = detail_message(&body);
            if detail.is_empty() {
                format!("Хаб: {code}")
            } else {
                format!("Хаб: {detail}")
            }
        }
        ureq::Error::Transport(_) => "Хаб: нет связи".into(),
    }
}

fn detail_message(body: &str) -> String {
    let detail = serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|value| {
            value
                .get("detail")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .unwrap_or_default();
    if detail.len() > 180
        || detail.contains("://")
        || detail.to_ascii_lowercase().contains("password")
    {
        return String::new();
    }
    detail
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HomeAlbum {
    pub id: String,
    pub cover_id: String,
    pub title: String,
    pub artist: String,
    pub year: Option<u32>,
    pub starred: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HomeArtist {
    pub id: String,
    pub cover_id: String,
    pub name: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct HomeFeed {
    pub recent: Vec<HomeAlbum>,
    pub frequent: Vec<HomeAlbum>,
    pub newest: Vec<HomeAlbum>,
    pub starred: Vec<HomeAlbum>,
    pub radio: Vec<HomeArtist>,
}

/// A song the Hub named. `reference` is the catalog ref. No cover and no stream URL.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Playable {
    pub nd_song_id: String,
    pub reference: String,
    pub title: String,
    pub artist: String,
    pub album: String,
    pub duration_s: u32,
}

impl Playable {
    pub fn can_play(&self) -> bool {
        !self.nd_song_id.is_empty() || !self.reference.is_empty()
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WaveBatch {
    pub session_id: String,
    pub title: String,
    pub tracks: Vec<Playable>,
    pub pending: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MixPhase {
    Ready,
    Building,
    Onboarding,
    Waiting,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MixSnapshot {
    pub phase: MixPhase,
    pub id: i64,
    pub title: String,
    pub description: String,
    pub total: u32,
    pub ready: u32,
    pub hour: u32,
    pub mode: String,
    pub mix_mode: String,
    pub building: bool,
    pub can_rebuild: bool,
    pub playlist_id: String,
    pub tracks: Vec<Playable>,
    pub poll: bool,
}

impl Default for MixSnapshot {
    fn default() -> Self {
        Self {
            phase: MixPhase::Waiting,
            id: 0,
            title: String::new(),
            description: String::new(),
            total: 0,
            ready: 0,
            hour: 0,
            mode: "normal".into(),
            mix_mode: "normal".into(),
            building: false,
            can_rebuild: false,
            playlist_id: String::new(),
            tracks: Vec::new(),
            poll: false,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlaylistLine {
    pub id: String,
    pub name: String,
    pub songs: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LikedPage {
    pub songs: Vec<Playable>,
    pub albums: Vec<HomeAlbum>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TasteArtist {
    pub reference: String,
    pub name: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TastePage {
    pub selected: Vec<TasteArtist>,
    pub suggestions: Vec<TasteArtist>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MixSettings {
    pub mode: String,
    pub hour: u32,
}

fn parse_mix_settings(value: &Value) -> MixSettings {
    let mode = text(value, "mode");
    MixSettings {
        mode: if mode.is_empty() { "normal".into() } else { mode },
        hour: number(value, "mix_hour").clamp(0, 23) as u32,
    }
}


#[derive(Clone, Debug, Default)]
pub struct WaveLive {
    pub id: String,
    pub personal: bool,
    pub origin: String,
    pub pending: Vec<Value>,
    pub asking: bool,
}

impl WaveLive {
    pub fn on(&self) -> bool {
        !self.id.is_empty()
    }
}

pub fn parse_home(value: &Value) -> HomeFeed {
    HomeFeed {
        recent: albums_of(value, "recent"),
        frequent: albums_of(value, "frequent"),
        newest: albums_of(value, "newest"),
        starred: albums_of(value, "starred"),
        radio: value
            .get("radio")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter_map(|row| {
                        let id = text(row, "id");
                        if id.is_empty() {
                            return None;
                        }
                        let name = text(row, "name");
                        let name = if name.is_empty() { text(row, "title") } else { name };
                        Some(HomeArtist { id, name, cover_id: home_cover_id(row) })
                    })
                    .collect()
            })
            .unwrap_or_default(),
    }
}

pub fn parse_mix_today(value: &Value) -> MixSnapshot {
    let settings = parse_mix_settings(value.get("settings").unwrap_or(value));
    let building = value
        .get("building")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let onboarding = value
        .get("onboarding")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let Some(mix) = value.get("mix").filter(|mix| mix.is_object()) else {
        return MixSnapshot {
            phase: if building {
                MixPhase::Building
            } else if onboarding {
                MixPhase::Onboarding
            } else {
                MixPhase::Waiting
            },
            hour: settings.hour,
            mode: settings.mode,
            building,
            poll: building,
            ..MixSnapshot::default()
        };
    };
    let total = number(mix, "total") as u32;
    let ready = number(mix, "ready") as u32;
    let mix_mode = text(mix, "mode");
    MixSnapshot {
        phase: MixPhase::Ready,
        id: number(mix, "id"),
        title: text(mix, "title"),
        description: text(mix, "description"),
        total,
        ready,
        hour: settings.hour,
        mode: settings.mode,
        mix_mode: if mix_mode.is_empty() { "normal".into() } else { mix_mode },
        building,
        can_rebuild: false,
        playlist_id: text(mix, "nd_playlist_id"),
        tracks: playables(mix.get("tracks")),
        poll: building || ready < total,
    }
}

pub fn parse_wave(value: &Value) -> WaveBatch {
    WaveBatch {
        session_id: text(value, "session_id"),
        title: text(value, "title"),
        tracks: playables(value.get("tracks")),
        pending: value
            .get("pending")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    }
}

pub fn parse_playlists(value: &Value) -> Vec<PlaylistLine> {
    value
        .as_array()
        .map(|rows| {
            rows.iter()
                .filter_map(|row| {
                    let id = text(row, "id");
                    if id.is_empty() {
                        return None;
                    }
                    Some(PlaylistLine {
                        id,
                        name: text(row, "name"),
                        songs: number(row, "song_count") as u32,
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn parse_liked(value: &Value) -> LikedPage {
    LikedPage {
        songs: playables(value.get("songs")),
        albums: albums_of(value, "albums"),
    }
}

pub fn parse_taste(value: &Value) -> TastePage {
    TastePage {
        selected: taste_artists(value.get("selected")),
        suggestions: taste_artists(value.get("suggestions")),
    }
}

fn taste_artists(value: Option<&Value>) -> Vec<TasteArtist> {
    value
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| {
                    let reference = text(row, "ref");
                    if reference.is_empty() {
                        return None;
                    }
                    Some(TasteArtist {
                        reference,
                        name: text(row, "name"),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

/// One wave event. `None` when the track has neither a library id nor a catalog ref.
pub fn wave_event(
    kind: &str,
    nd_song_id: &str,
    reference: &str,
    position_s: f32,
    duration_s: f32,
    session_id: &str,
    origin: &str,
) -> Option<Value> {
    if nd_song_id.is_empty() && reference.is_empty() {
        return None;
    }
    let mut event = serde_json::json!({
        "event_id": event_id(),
        "ts": rfc3339_now(),
        "kind": kind,
        "target_type": "track",
        "client": CLIENT,
        "context": format!("wave:{session_id}"),
        "launch_id": format!("wave:{session_id}"),
        "launch_origin": origin,
    });
    if !reference.is_empty() {
        event["ref"] = Value::String(reference.to_string());
    }
    if !nd_song_id.is_empty() {
        event["nd_song_id"] = Value::String(nd_song_id.to_string());
    }
    if kind == "skip" || kind == "finish" {
        event["position_s"] = json_tenth(position_s);
        event["duration_s"] = json_tenth(duration_s);
    }
    Some(event)
}

// Keep the Subsonic identity, not a URL or an arbitrary remote image endpoint.
fn home_cover_id(row: &Value) -> String {
    let url = text(row, "cover_url");
    let Some(path) = url.strip_prefix("/api/nd/cover/") else {
        return String::new();
    };
    let encoded = path.split('?').next().unwrap_or_default();
    let mut bytes = Vec::with_capacity(encoded.len());
    let mut chars = encoded.as_bytes().iter().copied();
    while let Some(byte) = chars.next() {
        if byte == b'%' {
            let Some(high) = chars.next().and_then(|c| (c as char).to_digit(16)) else {
                return String::new();
            };
            let Some(low) = chars.next().and_then(|c| (c as char).to_digit(16)) else {
                return String::new();
            };
            bytes.push((high * 16 + low) as u8);
        } else {
            bytes.push(byte);
        }
    }
    String::from_utf8(bytes).unwrap_or_default()
}

fn albums_of(value: &Value, key: &str) -> Vec<HomeAlbum> {
    value
        .get(key)
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| {
                    let id = text(row, "id");
                    if id.is_empty() {
                        return None;
                    }
                    let title = {
                        let title = text(row, "title");
                        if title.is_empty() { text(row, "name") } else { title }
                    };
                    Some(HomeAlbum {
                        id,
                        cover_id: home_cover_id(row),
                        title,
                        artist: text(row, "artist"),
                        year: row.get("year").and_then(Value::as_u64).map(|year| year as u32),
                        starred: row.get("starred").and_then(Value::as_bool).unwrap_or(false),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

fn playables(value: Option<&Value>) -> Vec<Playable> {
    value
        .and_then(Value::as_array)
        .map(|rows| rows.iter().filter_map(playable).collect())
        .unwrap_or_default()
}

fn playable(row: &Value) -> Option<Playable> {
    let mut nd_song_id = text(row, "nd_song_id");
    let mut reference = text(row, "ref");
    if nd_song_id.is_empty() && reference.starts_with("nd:") {
        nd_song_id = reference.trim_start_matches("nd:").to_string();
    }
    if reference.is_empty() && !nd_song_id.is_empty() {
        reference = format!("nd:{nd_song_id}");
    }
    if nd_song_id.is_empty() && reference.is_empty() {
        return None;
    }
    let artist = {
        let artist = text(row, "artist");
        if !artist.is_empty() {
            artist
        } else {
            row.get("artists")
                .and_then(Value::as_array)
                .map(|names| {
                    names
                        .iter()
                        .filter_map(Value::as_str)
                        .collect::<Vec<_>>()
                        .join(", ")
                })
                .unwrap_or_default()
        }
    };
    let title = text(row, "title");
    if title.is_empty() && artist.is_empty() {
        return None;
    }
    Some(Playable {
        nd_song_id,
        reference,
        title,
        artist,
        album: text(row, "album_title"),
        duration_s: number(row, "duration_s") as u32,
    })
}

fn number(value: &Value, name: &str) -> i64 {
    value.get(name).and_then(Value::as_i64).unwrap_or(0)
}

fn json_tenth(value: f32) -> Value {
    let tenths = (value * 10.0).round() as i64;
    serde_json::json!(tenths as f64 / 10.0)
}

fn event_id() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(1);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    format!("d{secs:x}{n:x}")
}

fn rfc3339_now() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    let days = (secs / 86_400) as i64;
    let tod = secs % 86_400;
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        tod / 3600,
        (tod / 60) % 60,
        tod % 60
    )
}

/// Howard Hinnant's civil_from_days, days since 1970-01-01.
fn civil_from_days(days: i64) -> (i32, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { y + 1 } else { y };
    (year as i32, month as u32, day as u32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hub_address_follows_the_navidrome_host() {
        assert_eq!(
            hub_base("http://192.168.1.51:4533", ""),
            "http://192.168.1.51:8080"
        );
        assert_eq!(
            hub_base("http://192.168.1.51:4533/", "http://127.0.0.1:8080/"),
            "http://127.0.0.1:8080"
        );
        assert_eq!(hub_base("not a url", ""), "");
    }

    #[test]
    fn session_cookie_is_the_token_only() {
        assert_eq!(
            session_cookie("hm_session=abc.def; HttpOnly; Path=/; SameSite=lax"),
            Some("abc.def".into())
        );
        assert_eq!(session_cookie("Path=/; HttpOnly"), None);
    }

    #[test]
    fn starred_and_dislikes_keep_ids_only() {
        let marks = parse_starred(&serde_json::json!({
            "song_ids": ["s1", ""],
            "album_ids": ["a1"],
            "refs": ["dz:1"]
        }));
        assert_eq!(marks.songs, HashSet::from(["s1".into()]));
        assert_eq!(marks.albums, HashSet::from(["a1".into()]));
        let disliked = parse_dislike_ids(&serde_json::json!([
            {"id": 1, "nd_song_id": "s1", "title": "V"},
            {"id": 2, "ref": "dz:9"}
        ]));
        assert_eq!(disliked, HashSet::from(["s1".into()]));
    }

    #[test]
    fn search_keeps_albums_and_owned_tracks_without_urls() {
        let hits = parse_search(&serde_json::json!({
            "albums": [{
                "ref": "dz:a",
                "title": "Альбом",
                "artist": "V",
                "nb_tracks": 10,
                "owned_status": "none",
                "cover_url": "https://example.test/secret"
            }],
            "tracks": [{
                "ref": "dz:t",
                "title": "Трек",
                "artist": "V",
                "album_title": "Альбом",
                "duration_s": 75,
                "owned": true,
                "nd_song_id": "nd1",
                "cover_url": "https://example.test/secret"
            }]
        }));
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].action(), "скачать");
        assert_eq!(hits[1].action(), "играть");
        assert_eq!(hits[1].detail, "1:15 · Альбом");
        let dumped = format!("{hits:?}");
        assert!(!dumped.contains("example.test"));
        assert!(!dumped.contains("secret"));
    }

    #[test]
    fn star_and_download_bodies_have_no_password() {
        let star = star_body(&["song"], &["album"], false).to_string();
        assert!(star.contains("\"client\":\"desktop\""));
        assert!(!star.contains("password"));
        let summary = download_summary(&serde_json::json!({"queued": 1, "already_owned": 0}));
        assert_eq!(summary, "В загрузке: 1");
        assert_eq!(
            download_summary(&serde_json::json!({"queued": 0, "already_owned": 2})),
            "Уже в библиотеке"
        );
    }

    #[test]
    fn a_secret_detail_is_dropped() {
        assert_eq!(detail_message(r#"{"detail":"Нет трека"}"#), "Нет трека");
        assert_eq!(
            detail_message(r#"{"detail":"see https://example.test/?password=secret"}"#),
            ""
        );
    }

    #[test]
    fn home_keeps_cover_identity_and_radio_order_without_remote_urls() {
        let feed = parse_home(&serde_json::json!({
            "recent": [{
                "id": "a1",
                "title": "Ночной",
                "artist": "V",
                "year": 2024,
                "starred": true,
                "cover_url": "/api/nd/cover/al-a1%2Fart?size=300"
            }],
            "frequent": [],
            "newest": [{"id": "", "title": "Пустой"}],
            "starred": [],
            "radio": [
                {"id": "r1", "name": "Первый", "cover_url": "https://example.test/a"},
                {"id": "r2", "name": "Второй"}
            ]
        }));
        assert_eq!(feed.recent.len(), 1);
        assert_eq!(feed.recent[0].year, Some(2024));
        assert_eq!(feed.recent[0].cover_id, "al-a1/art");
        assert!(feed.recent[0].starred);
        assert!(feed.newest.is_empty());
        assert_eq!(feed.radio[0].name, "Первый");
        assert_eq!(feed.radio[1].id, "r2");
        let dumped = format!("{feed:?}");
        assert!(!dumped.contains("example.test"));
        assert!(!dumped.contains("secret"));
        assert!(feed.radio[0].cover_id.is_empty());
        assert_eq!(home_cover_id(&serde_json::json!({"cover_url": "/api/nd/cover/a%ZZ"})), "");
    }

    #[test]
    fn mix_card_states_and_wave_tracks_keep_no_stream() {
        let waiting = parse_mix_today(&serde_json::json!({
            "mix": null,
            "building": false,
            "onboarding": false,
            "settings": {"mode": "favorites", "mix_hour": 7}
        }));
        assert_eq!(waiting.phase, MixPhase::Waiting);
        assert_eq!(waiting.hour, 7);
        assert_eq!(waiting.mode, "favorites");
        assert!(!waiting.poll);

        let building = parse_mix_today(&serde_json::json!({
            "mix": null,
            "building": true,
            "onboarding": false,
            "settings": {"mix_hour": 9}
        }));
        assert_eq!(building.phase, MixPhase::Building);
        assert!(building.poll);

        let ready = parse_mix_today(&serde_json::json!({
            "mix": {
                "id": 4,
                "title": "Четверг",
                "description": "Тихий",
                "mode": "discover",
                "ready": 3,
                "total": 5,
                "tracks": [{
                    "ref": "dz:1",
                    "title": "Песня",
                    "artist": "V",
                    "album_title": "Альбом",
                    "duration_s": 80,
                    "owned": true,
                    "nd_song_id": "s1",
                    "cover_url": "https://example.test/secret"
                }]
            },
            "building": false,
            "settings": {"mix_hour": 6, "mode": "normal"}
        }));
        assert_eq!(ready.phase, MixPhase::Ready);
        assert_eq!(ready.tracks[0].nd_song_id, "s1");
        assert_eq!(ready.tracks[0].reference, "dz:1");
        assert!(ready.poll);
        let dumped = format!("{ready:?}");
        assert!(!dumped.contains("example.test"));
        assert!(!dumped.contains("http"));

        let wave = parse_wave(&serde_json::json!({
            "session_id": "w1",
            "title": "Моя волна",
            "pending": false,
            "tracks": [{
                "ref": "nd:s2",
                "title": "Только библиотека",
                "artists": ["A", "B"],
                "duration_s": 10,
                "stream_url": "https://example.test/stream?token=secret"
            }]
        }));
        assert_eq!(wave.tracks[0].nd_song_id, "s2");
        assert_eq!(wave.tracks[0].artist, "A, B");
        assert!(!format!("{wave:?}").contains("stream"));
    }

    #[test]
    fn mix_settings_are_not_the_previous_mix_mode_and_rebuild_keeps_tracks() {
        let mut payload = serde_json::json!({
            "settings": {"mode": "favorites", "mix_hour": 8},
            "building": true,
            "onboarding": false,
            "mix": {
                "id": 70, "mode": "normal", "title": "Yesterday",
                "nd_playlist_id": "daily-playlist", "ready": 2, "total": 3,
                "tracks": [
                    {"position": 0, "ref": "dz:55", "nd_song_id": "local-55",
                     "title": "Owned catalog", "artist": "One", "duration_s": 101},
                    {"position": 1, "ref": "dz:77", "nd_song_id": null,
                     "title": "Downloading", "artist": "Two", "downloading": true},
                    {"position": 2, "ref": "nd:local-only", "nd_song_id": null,
                     "title": "Library only", "artist": "Three", "owned": true}
                ]
            }
        });
        let old = parse_mix_today(&payload);
        assert_eq!(old.phase, MixPhase::Ready);
        assert_eq!(old.mode, "favorites");
        assert_eq!(old.mix_mode, "normal");
        assert_eq!(old.playlist_id, "daily-playlist");
        assert!(old.building && old.poll);
        assert_eq!(old.tracks.len(), 3);
        assert!(old.tracks.iter().all(Playable::can_play));
        assert_eq!(old.tracks[0].nd_song_id, "local-55");
        assert_eq!(old.tracks[0].reference, "dz:55");
        assert!(old.tracks[1].nd_song_id.is_empty());
        assert_eq!(old.tracks[1].reference, "dz:77");
        assert_eq!(old.tracks[2].nd_song_id, "local-only");

        payload["building"] = Value::Bool(false);
        payload["mix"]["id"] = serde_json::json!(71);
        payload["mix"]["mode"] = serde_json::json!("favorites");
        payload["mix"]["ready"] = serde_json::json!(3);
        let replacement = parse_mix_today(&payload);
        assert_eq!(replacement.id, 71);
        assert_eq!(replacement.mix_mode, replacement.mode);
        assert!(!replacement.building && !replacement.poll);
        assert_eq!(replacement.playlist_id, old.playlist_id);
    }

    #[test]
    fn taste_returns_saved_selection_separately_from_search_results() {
        let page = parse_taste(&serde_json::json!({
            "selected": [{"ref": "dz:1", "name": "Saved"}, {"ref": "", "name": "Invalid"}],
            "suggestions": [{"ref": "dz:2", "name": "Search result", "source": "dz", "id": "2"}]
        }));
        assert_eq!(page.selected, vec![TasteArtist { reference: "dz:1".into(), name: "Saved".into() }]);
        assert_eq!(page.suggestions, vec![TasteArtist { reference: "dz:2".into(), name: "Search result".into() }]);
        assert!(parse_taste(&serde_json::json!({})).selected.is_empty());
    }

    #[test]
    fn a_wave_event_names_the_track_and_hides_nothing_secret() {
        let event = wave_event("skip", "s1", "dz:1", 12.34, 40.0, "w1", "home_card").unwrap();
        assert_eq!(event["kind"], "skip");
        assert_eq!(event["context"], "wave:w1");
        assert_eq!(event["launch_origin"], "home_card");
        assert_eq!(event["position_s"], 12.3);
        assert!(wave_event("finish", "", "", 0.0, 0.0, "w1", "radio").is_none());
        let dumped = event.to_string();
        assert!(!dumped.contains("password"));
        assert!(!dumped.contains("://"));
        let _ = rfc3339_now();
        assert_eq!(civil_from_days(0), (1970, 1, 1));
    }
}
