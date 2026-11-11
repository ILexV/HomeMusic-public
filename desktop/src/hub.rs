//! Hub session for likes, dislikes, catalog search, and downloads.
//! The Navidrome password is sent only to `/api/auth/login` and is never put in an error.

use std::collections::HashSet;
use std::sync::Mutex;
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
            HitKind::Album if self.owned => "в библиотеке",
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
}

impl Hub {
    pub fn from_config(config: &crate::navidrome::ServerConfig) -> Self {
        Self {
            base: hub_base(&config.server, &config.hub),
            user: config.user.clone(),
            password: config.password.clone(),
            cookie: Mutex::new(String::new()),
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
        let _ = response.into_string();
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
}
