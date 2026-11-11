//! Navidrome over the Subsonic API. Token auth matches the Hub: `t = md5(password + salt)`.
//! Errors returned from here are short and never include the password or the stream URL.

use std::fmt;
use std::fs::File;
use std::io::Read;
use std::path::PathBuf;
use std::time::Duration;

use serde_json::Value;

const API_VERSION: &str = "1.16.1";
const CLIENT_NAME: &str = "homemusic-desktop";

#[derive(Clone, PartialEq, Eq)]
pub struct ServerConfig {
    pub server: String,
    pub user: String,
    pub password: String,
    pub loaded: bool,
}

impl fmt::Debug for ServerConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ServerConfig")
            .field("server", &self.server)
            .field("user", &self.user)
            .field("password", &"<скрыт>")
            .field("loaded", &self.loaded)
            .finish()
    }
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            server: "http://127.0.0.1:4533".into(),
            user: String::new(),
            password: String::new(),
            loaded: false,
        }
    }
}

impl ServerConfig {
    pub fn load() -> Self {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("config.toml");
        match std::fs::read_to_string(&path) {
            Ok(text) => parse_config(&text),
            Err(_) => Self::default(),
        }
    }

    /// Short status for the window. Never includes the password.
    pub fn status_line(&self) -> String {
        if !self.loaded {
            return "Navidrome: нет config.toml".into();
        }
        if self.user.is_empty() || self.password.is_empty() {
            return format!("Navidrome: в config.toml нет логина ({})", self.server);
        }
        format!("Navidrome: ждёт подключения к {}", self.server)
    }
}

/// One library track ready for mpv. `stream_url` carries the Subsonic token.
pub struct Track {
    pub id: String,
    pub title: String,
    pub artist: String,
    pub album: String,
    pub meta: String,
    pub duration_s: u32,
    pub stream_url: String,
}

impl fmt::Debug for Track {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Track")
            .field("id", &self.id)
            .field("title", &self.title)
            .field("artist", &self.artist)
            .field("album", &self.album)
            .field("meta", &self.meta)
            .field("duration_s", &self.duration_s)
            .field("stream_url", &"<скрыт>")
            .finish()
    }
}

pub fn parse_config(text: &str) -> ServerConfig {
    let mut cfg = ServerConfig {
        loaded: true,
        ..ServerConfig::default()
    };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let value = value.trim().trim_matches('"').to_string();
        match key.trim() {
            "server" => cfg.server = value,
            "user" => cfg.user = value,
            "password" => cfg.password = value,
            _ => {}
        }
    }
    cfg
}

pub fn subsonic_token(password: &str, salt: &str) -> String {
    use md5::{Digest, Md5};
    let digest = Md5::digest(format!("{password}{salt}"));
    format!("{digest:x}")
}

/// `ping.view`, then one random library track and its `stream.view` URL.
pub fn first_track(config: &ServerConfig) -> Result<Track, String> {
    if !config.loaded {
        return Err("Navidrome: нет config.toml".into());
    }
    if config.user.is_empty() || config.password.is_empty() {
        return Err(format!(
            "Navidrome: в config.toml нет логина ({})",
            config.server
        ));
    }
    let ping = http_json(&view_url(config, "ping.view", ""))?;
    if auth_failed(&ping) {
        return Err("Navidrome: неверный логин или пароль".into());
    }
    if !status_ok(&ping) {
        return Err(format!("Navidrome: ошибка сервера ({})", config.server));
    }
    let songs = http_json(&view_url(config, "getRandomSongs.view", "size=1"))?;
    if auth_failed(&songs) {
        return Err("Navidrome: неверный логин или пароль".into());
    }
    if !status_ok(&songs) {
        return Err(format!("Navidrome: ошибка сервера ({})", config.server));
    }
    let Some(song) = one_song(&songs) else {
        return Err("Navidrome: в библиотеке нет треков".into());
    };
    let stream_url = view_url(config, "stream.view", &format!("id={}", encode(&song.id)));
    Ok(Track {
        id: song.id,
        title: song.title,
        artist: song.artist,
        album: song.album,
        meta: song.meta,
        duration_s: song.duration_s,
        stream_url,
    })
}

/// Album art for a song id. `None` when the server has no picture or cannot be reached.
/// The returned bytes are a JPEG or a PNG, never an error page.
pub fn cover_art(config: &ServerConfig, song_id: &str) -> Option<Vec<u8>> {
    if !config.loaded || config.password.is_empty() || song_id.is_empty() {
        return None;
    }
    let url = view_url(
        config,
        "getCoverArt.view",
        &format!("id={}&size=256", encode(song_id)),
    );
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(3))
        .timeout_read(Duration::from_secs(8))
        .build();
    let response = agent.get(&url).call().ok()?;
    if response.status() != 200 {
        return None;
    }
    let mut bytes = Vec::new();
    response
        .into_reader()
        .take(2_000_000)
        .read_to_end(&mut bytes)
        .ok()?;
    is_cover_image(&bytes).then_some(bytes)
}

pub fn is_cover_image(bytes: &[u8]) -> bool {
    bytes.starts_with(&[0xFF, 0xD8, 0xFF]) || bytes.starts_with(b"\x89PNG\r\n\x1a\n")
}

struct SongParts {
    id: String,
    title: String,
    artist: String,
    album: String,
    meta: String,
    duration_s: u32,
}

fn view_url(config: &ServerConfig, view: &str, extra: &str) -> String {
    let server = config.server.trim_end_matches('/');
    let salt = random_salt();
    let token = subsonic_token(&config.password, &salt);
    let mut url = format!(
        "{server}/rest/{view}?u={}&t={token}&s={salt}&v={API_VERSION}&c={CLIENT_NAME}&f=json",
        encode(&config.user),
    );
    if !extra.is_empty() {
        url.push('&');
        url.push_str(extra);
    }
    url
}

fn http_json(url: &str) -> Result<Value, String> {
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(3))
        .timeout_read(Duration::from_secs(8))
        .build();
    // The URL contains the token. Keep ureq's own error text off the screen and the console.
    let response = agent
        .get(url)
        .call()
        .map_err(|_| unreachable_message(url))?;
    response.into_json().map_err(|_| unreachable_message(url))
}

fn unreachable_message(url: &str) -> String {
    let server = url.split("/rest/").next().unwrap_or(url);
    format!("Navidrome: нет связи с {server}")
}

fn status_ok(body: &Value) -> bool {
    subsonic(body).get("status").and_then(Value::as_str) == Some("ok")
}

fn auth_failed(body: &Value) -> bool {
    matches!(
        subsonic(body)
            .pointer("/error/code")
            .and_then(Value::as_i64),
        Some(40 | 41)
    )
}

fn subsonic(body: &Value) -> &Value {
    body.get("subsonic-response").unwrap_or(body)
}

fn one_song(body: &Value) -> Option<SongParts> {
    let songs = subsonic(body).pointer("/randomSongs/song")?;
    let song = songs
        .as_array()
        .and_then(|rows| rows.first())
        .unwrap_or(songs);
    let id = song
        .get("id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())?;
    let title = text_field(song, "title");
    let artist = text_field(song, "artist");
    let album = text_field(song, "album");
    let suffix = text_field(song, "suffix");
    let meta = if suffix.is_empty() {
        String::new()
    } else {
        suffix.to_ascii_uppercase()
    };
    Some(SongParts {
        id: id.to_string(),
        title: if title.is_empty() {
            "Без названия".into()
        } else {
            title
        },
        artist,
        album,
        meta,
        duration_s: number_field(song, "duration"),
    })
}

fn number_field(song: &Value, name: &str) -> u32 {
    match song.get(name) {
        Some(Value::Number(value)) => value.as_u64().unwrap_or(0).min(u64::from(u32::MAX)) as u32,
        _ => 0,
    }
}

fn text_field(song: &Value, name: &str) -> String {
    song.get(name)
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string()
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

fn random_salt() -> String {
    let mut bytes = [0u8; 8];
    if let Ok(mut file) = File::open("/dev/urandom") {
        if file.read_exact(&mut bytes).is_ok() {
            return bytes.iter().map(|byte| format!("{byte:02x}")).collect();
        }
    }
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    format!("{nanos:x}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_server_user_and_password() {
        let cfg = parse_config(
            "server = \"http://192.168.1.51:4533\"\nuser = \"lex\"\npassword = \"secret\"\n",
        );
        assert!(cfg.loaded);
        assert_eq!(cfg.server, "http://192.168.1.51:4533");
        assert_eq!(cfg.user, "lex");
        assert_eq!(cfg.password, "secret");
        assert!(!cfg.status_line().contains("secret"));
        assert!(!format!("{cfg:?}").contains("secret"));
    }

    #[test]
    fn missing_login_stays_local_and_hides_the_password() {
        let cfg = ServerConfig {
            server: "http://127.0.0.1:1".into(),
            user: String::new(),
            password: "secret".into(),
            loaded: true,
        };
        let err = first_track(&cfg).unwrap_err();
        assert!(err.contains("нет логина"));
        assert!(!err.contains("secret"));
    }

    #[test]
    fn token_is_md5_of_password_plus_salt() {
        assert_eq!(
            subsonic_token("secret", "abcd"),
            "42d635acbe6c440ab8f6e5ca0859f45c"
        );
    }

    #[test]
    fn ping_url_keeps_the_password_out() {
        let cfg = ServerConfig {
            server: "http://192.168.1.51:4533".into(),
            user: "lex".into(),
            password: "secret".into(),
            loaded: true,
        };
        let url = view_url(&cfg, "ping.view", "");
        assert!(url.starts_with("http://192.168.1.51:4533/rest/ping.view?"));
        assert!(url.contains("u=lex"));
        assert!(url.contains("c=homemusic-desktop"));
        assert!(url.contains("v=1.16.1"));
        assert!(!url.contains("secret"));
    }

    #[test]
    fn reads_a_song_from_a_list_or_an_object() {
        let listed = serde_json::json!({
            "subsonic-response": {
                "status": "ok",
                "randomSongs": {"song": [{
                    "id": "abc",
                    "title": "За речкою",
                    "artist": "FOLKBEAT",
                    "album": "За речкою",
                    "suffix": "mp3",
                    "duration": 197
                }]}
            }
        });
        let song = one_song(&listed).unwrap();
        assert_eq!(song.id, "abc");
        assert_eq!(song.title, "За речкою");
        assert_eq!(song.artist, "FOLKBEAT");
        assert_eq!(song.meta, "MP3");
        assert_eq!(song.duration_s, 197);
        assert!(is_cover_image(&[0xFF, 0xD8, 0xFF, 0xD9]));
        assert!(!is_cover_image(br#"{"error":"no"}"#));

        let single = serde_json::json!({
            "subsonic-response": {"status": "ok", "randomSongs": {"song": {
                "id": "one", "title": "Solo", "artist": "", "suffix": "opus"
            }}}
        });
        let song = one_song(&single).unwrap();
        assert_eq!(song.meta, "OPUS");
        assert!(auth_failed(&serde_json::json!({
            "subsonic-response": {"status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}
        })));
    }
}
