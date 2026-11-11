//! Navidrome over the Subsonic API. Token auth matches the Hub: `t = md5(password + salt)`.
//! Errors returned from here are short and never include the password or the stream URL.

use std::fmt;
use std::io::Read;
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
        let path = crate::paths::dir().join("config.toml");
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

/// A library track without its stream URL. The URL is built at play time.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LibraryTrack {
    pub id: String,
    pub title: String,
    pub artist: String,
    pub album: String,
    pub meta: String,
    pub duration_s: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LibraryQueue {
    pub name: String,
    pub tracks: Vec<LibraryTrack>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Artist {
    pub id: String,
    pub name: String,
    pub album_count: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Album {
    pub id: String,
    pub name: String,
    pub artist: String,
    pub artist_id: String,
    pub song_count: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Catalog {
    pub artists: Vec<Artist>,
    pub albums: Vec<Album>,
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

/// `stream.view` for one song. The password stays in the token and is not part of the path.
pub fn stream_url(config: &ServerConfig, song_id: &str) -> String {
    view_url(config, "stream.view", &format!("id={}", encode(song_id)))
}

/// `ping.view`, then one random library track and its `stream.view` URL.
pub fn first_track(config: &ServerConfig) -> Result<Track, String> {
    require_login(config)?;
    let ping = http_json(&view_url(config, "ping.view", ""))?;
    check_body(config, &ping)?;
    let songs = http_json(&view_url(config, "getRandomSongs.view", "size=1"))?;
    check_body(config, &songs)?;
    let Some(song) = one_song(&songs) else {
        return Err("Navidrome: в библиотеке нет треков".into());
    };
    Ok(track_with_url(config, song))
}

/// The first saved playlist, or every library track when there is no playlist.
pub fn load_queue(config: &ServerConfig) -> Result<LibraryQueue, String> {
    require_login(config)?;
    let ping = http_json(&view_url(config, "ping.view", ""))?;
    check_body(config, &ping)?;
    let lists = http_json(&view_url(config, "getPlaylists.view", ""))?;
    check_body(config, &lists)?;
    if let Some((id, name)) = first_playlist(&lists) {
        let extra = format!("id={}", encode(&id));
        let body = http_json(&view_url(config, "getPlaylist.view", &extra))?;
        check_body(config, &body)?;
        let (playlist_name, tracks) = playlist_tracks(&body);
        if !tracks.is_empty() {
            let name = if playlist_name.is_empty() {
                name
            } else {
                playlist_name
            };
            return Ok(LibraryQueue { name, tracks });
        }
    }
    let tracks = all_tracks(config)?;
    if tracks.is_empty() {
        return Err("Navidrome: в библиотеке нет треков".into());
    }
    Ok(LibraryQueue {
        name: "Все треки".into(),
        tracks,
    })
}

fn track_with_url(config: &ServerConfig, song: LibraryTrack) -> Track {
    let stream_url = stream_url(config, &song.id);
    Track {
        id: song.id,
        title: song.title,
        artist: song.artist,
        album: song.album,
        meta: song.meta,
        duration_s: song.duration_s,
        stream_url,
    }
}

fn require_login(config: &ServerConfig) -> Result<(), String> {
    if !config.loaded {
        return Err("Navidrome: нет config.toml".into());
    }
    if config.user.is_empty() || config.password.is_empty() {
        return Err(format!(
            "Navidrome: в config.toml нет логина ({})",
            config.server
        ));
    }
    Ok(())
}

fn check_body(config: &ServerConfig, body: &Value) -> Result<(), String> {
    if auth_failed(body) {
        return Err("Navidrome: неверный логин или пароль".into());
    }
    if !status_ok(body) {
        return Err(format!("Navidrome: ошибка сервера ({})", config.server));
    }
    Ok(())
}

fn all_tracks(config: &ServerConfig) -> Result<Vec<LibraryTrack>, String> {
    const PAGE: u32 = 200;
    const CAP: usize = 5000;
    let mut tracks = Vec::new();
    let mut offset = 0u32;
    loop {
        let extra =
            format!("query=&songCount={PAGE}&songOffset={offset}&albumCount=0&artistCount=0");
        let body = http_json(&view_url(config, "search3.view", &extra))?;
        check_body(config, &body)?;
        let page = search_tracks(&body);
        let count = page.len();
        tracks.extend(page);
        if tracks.len() >= CAP || count < PAGE as usize {
            tracks.truncate(CAP);
            break;
        }
        offset = offset.saturating_add(PAGE);
    }
    Ok(tracks)
}

/// Artists and albums for the large window. An empty library is an error.
pub fn load_catalog(config: &ServerConfig) -> Result<Catalog, String> {
    require_login(config)?;
    let ping = http_json(&view_url(config, "ping.view", ""))?;
    check_body(config, &ping)?;
    let albums = all_albums(config)?;
    let artists_body = http_json(&view_url(config, "getArtists.view", ""))?;
    check_body(config, &artists_body)?;
    let artists = artists_from_body(&artists_body);
    if albums.is_empty() && artists.is_empty() {
        return Err("Navidrome: в библиотеке нет альбомов".into());
    }
    Ok(Catalog { artists, albums })
}

/// Tracks of one album, in the server's order. The name is the album name.
pub fn album_tracks(config: &ServerConfig, album_id: &str) -> Result<LibraryQueue, String> {
    require_login(config)?;
    if album_id.is_empty() {
        return Err("Navidrome: альбом не выбран".into());
    }
    let body = http_json(&view_url(
        config,
        "getAlbum.view",
        &format!("id={}", encode(album_id)),
    ))?;
    check_body(config, &body)?;
    let (name, tracks) = album_songs(&body);
    if tracks.is_empty() {
        return Err("Navidrome: в альбоме нет треков".into());
    }
    Ok(LibraryQueue { name, tracks })
}

/// Albums and artists matching a search box query. An empty query does not call the server.
pub fn search_catalog(config: &ServerConfig, query: &str) -> Result<Catalog, String> {
    require_login(config)?;
    let query = query.trim();
    if query.is_empty() {
        return Ok(Catalog {
            artists: Vec::new(),
            albums: Vec::new(),
        });
    }
    let extra = format!(
        "query={}&songCount=0&albumCount=100&artistCount=80",
        encode(query)
    );
    let body = http_json(&view_url(config, "search3.view", &extra))?;
    check_body(config, &body)?;
    Ok(catalog_from_search(&body))
}

/// Albums of one artist. An empty id keeps the whole list.
pub fn albums_for_artist(albums: &[Album], artist_id: &str, artist_name: &str) -> Vec<Album> {
    if artist_id.is_empty() {
        return albums.to_vec();
    }
    let name = artist_name.to_lowercase();
    albums
        .iter()
        .filter(|album| {
            if !album.artist_id.is_empty() && album.artist_id == artist_id {
                return true;
            }
            !name.is_empty() && album.artist.to_lowercase() == name
        })
        .cloned()
        .collect()
}

fn all_albums(config: &ServerConfig) -> Result<Vec<Album>, String> {
    const PAGE: u32 = 500;
    const CAP: usize = 5000;
    let mut albums = Vec::new();
    let mut offset = 0u32;
    loop {
        let extra = format!("type=alphabeticalByName&size={PAGE}&offset={offset}");
        let body = http_json(&view_url(config, "getAlbumList2.view", &extra))?;
        check_body(config, &body)?;
        let page = albums_from_list(&body);
        let count = page.len();
        albums.extend(page);
        if albums.len() >= CAP || count < PAGE as usize {
            albums.truncate(CAP);
            break;
        }
        offset = offset.saturating_add(PAGE);
    }
    Ok(albums)
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

fn one_song(body: &Value) -> Option<LibraryTrack> {
    let songs = subsonic(body).pointer("/randomSongs/song")?;
    song_list(songs).into_iter().next()
}

fn first_playlist(body: &Value) -> Option<(String, String)> {
    let value = subsonic(body).pointer("/playlists/playlist")?;
    let item = if let Some(rows) = value.as_array() {
        rows.first()?
    } else {
        value
    };
    let id = item
        .get("id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())?;
    let name = text_field(item, "name");
    let name = if name.is_empty() {
        "Плейлист".into()
    } else {
        name
    };
    Some((id.to_string(), name))
}

fn playlist_tracks(body: &Value) -> (String, Vec<LibraryTrack>) {
    let playlist = subsonic(body).pointer("/playlist");
    let name = playlist
        .map(|item| text_field(item, "name"))
        .unwrap_or_default();
    let tracks = playlist
        .and_then(|item| item.get("entry"))
        .map(song_list)
        .unwrap_or_default();
    (name, tracks)
}

fn search_tracks(body: &Value) -> Vec<LibraryTrack> {
    subsonic(body)
        .pointer("/searchResult3/song")
        .map(song_list)
        .unwrap_or_default()
}

fn albums_from_list(body: &Value) -> Vec<Album> {
    subsonic(body)
        .pointer("/albumList2/album")
        .map(album_list)
        .unwrap_or_default()
}

fn catalog_from_search(body: &Value) -> Catalog {
    let root = subsonic(body);
    Catalog {
        artists: root
            .pointer("/searchResult3/artist")
            .map(artist_list)
            .unwrap_or_default(),
        albums: root
            .pointer("/searchResult3/album")
            .map(album_list)
            .unwrap_or_default(),
    }
}

fn album_songs(body: &Value) -> (String, Vec<LibraryTrack>) {
    let album = subsonic(body).pointer("/album");
    let name = album
        .map(album_name)
        .filter(|name| !name.is_empty())
        .unwrap_or_else(|| "Альбом".into());
    let tracks = album
        .and_then(|item| item.get("song"))
        .map(song_list)
        .unwrap_or_default();
    (name, tracks)
}

fn artists_from_body(body: &Value) -> Vec<Artist> {
    let Some(indexes) = subsonic(body).pointer("/artists/index") else {
        return Vec::new();
    };
    let mut artists = Vec::new();
    if let Some(rows) = indexes.as_array() {
        for index in rows {
            push_artists(index, &mut artists);
        }
    } else {
        push_artists(indexes, &mut artists);
    }
    artists
}

fn push_artists(index: &Value, artists: &mut Vec<Artist>) {
    let Some(group) = index.get("artist") else {
        return;
    };
    if let Some(rows) = group.as_array() {
        artists.extend(rows.iter().filter_map(artist_parts));
    } else if let Some(artist) = artist_parts(group) {
        artists.push(artist);
    }
}

fn artist_list(value: &Value) -> Vec<Artist> {
    if let Some(rows) = value.as_array() {
        rows.iter().filter_map(artist_parts).collect()
    } else {
        artist_parts(value).into_iter().collect()
    }
}

fn artist_parts(value: &Value) -> Option<Artist> {
    let id = value
        .get("id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())?;
    let name = text_field(value, "name");
    Some(Artist {
        id: id.to_string(),
        name: if name.is_empty() {
            "Без имени".into()
        } else {
            name
        },
        album_count: number_field(value, "albumCount"),
    })
}

fn album_list(value: &Value) -> Vec<Album> {
    if let Some(rows) = value.as_array() {
        rows.iter().filter_map(album_parts).collect()
    } else {
        album_parts(value).into_iter().collect()
    }
}

fn album_parts(value: &Value) -> Option<Album> {
    let id = value
        .get("id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())?;
    let name = album_name(value);
    Some(Album {
        id: id.to_string(),
        name: if name.is_empty() {
            "Без названия".into()
        } else {
            name
        },
        artist: text_field(value, "artist"),
        artist_id: text_field(value, "artistId"),
        song_count: number_field(value, "songCount"),
    })
}

fn album_name(value: &Value) -> String {
    let name = text_field(value, "name");
    if name.is_empty() {
        text_field(value, "title")
    } else {
        name
    }
}

fn song_list(value: &Value) -> Vec<LibraryTrack> {
    if let Some(rows) = value.as_array() {
        rows.iter().filter_map(song_parts).collect()
    } else {
        song_parts(value).into_iter().collect()
    }
}

fn song_parts(song: &Value) -> Option<LibraryTrack> {
    let id = song
        .get("id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())?;
    let title = text_field(song, "title");
    let suffix = text_field(song, "suffix");
    Some(LibraryTrack {
        id: id.to_string(),
        title: if title.is_empty() {
            "Без названия".into()
        } else {
            title
        },
        artist: text_field(song, "artist"),
        album: text_field(song, "album"),
        meta: if suffix.is_empty() {
            String::new()
        } else {
            suffix.to_ascii_uppercase()
        },
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
    if fill_salt(&mut bytes) {
        return bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    }
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    format!("{nanos:x}")
}

fn fill_salt(bytes: &mut [u8; 8]) -> bool {
    #[cfg(unix)]
    {
        use std::io::Read;
        if let Ok(mut file) = std::fs::File::open("/dev/urandom") {
            return file.read_exact(bytes).is_ok();
        }
        false
    }
    #[cfg(windows)]
    {
        // RtlGenRandom. advapi32 is always present; /dev/urandom is not.
        #[link(name = "advapi32")]
        extern "system" {
            fn SystemFunction036(buffer: *mut u8, length: u32) -> u8;
        }
        unsafe { SystemFunction036(bytes.as_mut_ptr(), bytes.len() as u32) != 0 }
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = bytes;
        false
    }
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
        let err = load_queue(&cfg).unwrap_err();
        assert!(err.contains("нет логина"));
        assert!(!err.contains("secret"));
        let err = load_catalog(&cfg).unwrap_err();
        assert!(err.contains("нет логина"));
        assert!(!err.contains("secret"));
        let err = album_tracks(&cfg, "album").unwrap_err();
        assert!(err.contains("нет логина"));
        let err = search_catalog(&cfg, "аб").unwrap_err();
        assert!(err.contains("нет логина"));
        assert!(!err.contains("аб"));
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

    #[test]
    fn reads_a_playlist_into_tracks_without_a_stream_url() {
        let lists = serde_json::json!({
            "subsonic-response": {
                "status": "ok",
                "playlists": {"playlist": [
                    {"id": "pl1", "name": "Любимое", "songCount": 2},
                    {"id": "pl2", "name": "Другое", "songCount": 1}
                ]}
            }
        });
        let (id, name) = first_playlist(&lists).unwrap();
        assert_eq!(id, "pl1");
        assert_eq!(name, "Любимое");

        let playlist = serde_json::json!({
            "subsonic-response": {
                "status": "ok",
                "playlist": {
                    "id": "pl1",
                    "name": "Любимое",
                    "entry": [
                        {"id": "a", "title": "We Are Rebellion", "artist": "Damned Anthem", "album": "X", "suffix": "mp3", "duration": 129},
                        {"id": "b", "title": "Second", "artist": "Someone", "suffix": "opus", "duration": 10}
                    ]
                }
            }
        });
        let (name, tracks) = playlist_tracks(&playlist);
        assert_eq!(name, "Любимое");
        assert_eq!(tracks.len(), 2);
        assert_eq!(tracks[0].title, "We Are Rebellion");
        assert_eq!(tracks[0].artist, "Damned Anthem");
        assert_eq!(tracks[0].meta, "MP3");
        assert_eq!(tracks[0].duration_s, 129);
        assert_eq!(tracks[1].meta, "OPUS");
        assert!(!format!("{tracks:?}").contains("http"));

        let single = serde_json::json!({
            "subsonic-response": {"status": "ok", "playlist": {"name": "Один", "entry": {
                "id": "z", "title": "Solo", "suffix": "m4a", "duration": 3
            }}}
        });
        let (_, tracks) = playlist_tracks(&single);
        assert_eq!(tracks.len(), 1);
        assert_eq!(tracks[0].meta, "M4A");

        let empty = serde_json::json!({
            "subsonic-response": {"status": "ok", "playlist": {"name": "Пусто", "entry": []}}
        });
        assert!(playlist_tracks(&empty).1.is_empty());
        assert!(
            first_playlist(&serde_json::json!({"subsonic-response": {"status": "ok"}})).is_none()
        );

        let search = serde_json::json!({
            "subsonic-response": {"status": "ok", "searchResult3": {"song": [
                {"id": "s", "title": "All", "duration": 4, "suffix": "mp3"}
            ]}}
        });
        assert_eq!(search_tracks(&search).len(), 1);

        let cfg = ServerConfig {
            server: "http://192.168.1.51:4533".into(),
            user: "lex".into(),
            password: "secret".into(),
            loaded: true,
        };
        let url = stream_url(&cfg, "abc");
        assert!(url.contains("/rest/stream.view?"));
        assert!(url.contains("id=abc"));
        assert!(!url.contains("secret"));
    }

    #[test]
    fn reads_artists_albums_and_a_search() {
        let artists = serde_json::json!({
            "subsonic-response": {"status": "ok", "artists": {"index": [
                {"name": "D", "artist": [
                    {"id": "ar1", "name": "Damned Anthem", "albumCount": 2},
                    {"id": "ar2", "name": "Другой", "albumCount": 1}
                ]},
                {"name": "E", "artist": {"id": "ar3", "name": "Escala", "albumCount": 4}}
            ]}}
        });
        let artists = artists_from_body(&artists);
        assert_eq!(artists.len(), 3);
        assert_eq!(artists[0].name, "Damned Anthem");
        assert_eq!(artists[2].album_count, 4);

        let albums = serde_json::json!({
            "subsonic-response": {"status": "ok", "albumList2": {"album": [
                {"id": "al1", "name": "Visions", "artist": "Damned Anthem", "artistId": "ar1", "songCount": 8},
                {"id": "al2", "title": "Клуб", "artist": "Escala", "artistId": "ar3", "songCount": 1}
            ]}}
        });
        let albums = albums_from_list(&albums);
        assert_eq!(albums[0].name, "Visions");
        assert_eq!(albums[0].song_count, 8);
        assert_eq!(albums[1].name, "Клуб");
        let owned = albums_for_artist(&albums, "ar1", "Damned Anthem");
        assert_eq!(owned.len(), 1);
        assert_eq!(owned[0].id, "al1");
        let by_name = albums_for_artist(
            &[Album {
                id: "al3".into(),
                name: "Без id".into(),
                artist: "Damned Anthem".into(),
                artist_id: String::new(),
                song_count: 1,
            }],
            "ar1",
            "Damned Anthem",
        );
        assert_eq!(by_name.len(), 1);
        assert_eq!(albums_for_artist(&albums, "", "").len(), 2);

        let album = serde_json::json!({
            "subsonic-response": {"status": "ok", "album": {
                "id": "al1",
                "name": "Visions",
                "song": [
                    {"id": "s1", "title": "One", "artist": "Damned Anthem", "suffix": "mp3", "duration": 200},
                    {"id": "s2", "title": "Two", "suffix": "flac", "duration": 180}
                ]
            }}
        });
        let (name, tracks) = album_songs(&album);
        assert_eq!(name, "Visions");
        assert_eq!(tracks.len(), 2);
        assert_eq!(tracks[1].meta, "FLAC");

        let found = catalog_from_search(&serde_json::json!({
            "subsonic-response": {"status": "ok", "searchResult3": {
                "artist": [{"id": "ar1", "name": "Damned Anthem", "albumCount": 2}],
                "album": {"id": "al1", "name": "Visions", "artist": "Damned Anthem", "artistId": "ar1"}
            }}
        }));
        assert_eq!(found.artists.len(), 1);
        assert_eq!(found.albums.len(), 1);
        assert_eq!(found.albums[0].artist_id, "ar1");
        assert!(!format!("{found:?}").contains("secret"));
    }

    #[test]
    #[ignore = "needs desktop/config.toml and a running Navidrome"]
    fn loads_the_library_and_one_album() {
        let config = ServerConfig::load();
        let catalog = load_catalog(&config).unwrap_or_else(|err| panic!("{err}"));
        assert!(!catalog.albums.is_empty(), "no albums");
        eprintln!(
            "library: {} artists, {} albums",
            catalog.artists.len(),
            catalog.albums.len()
        );
        let album = &catalog.albums[0];
        if !album.artist_id.is_empty() {
            let owned = albums_for_artist(&catalog.albums, &album.artist_id, &album.artist);
            assert!(owned.iter().any(|item| item.id == album.id));
        }
        let tracks = album_tracks(&config, &album.id).unwrap_or_else(|err| panic!("{err}"));
        assert!(!tracks.tracks.is_empty(), "album has no tracks");
        eprintln!("album: {} ({} tracks)", tracks.name, tracks.tracks.len());
        let query: String = tracks
            .name
            .chars()
            .filter(|ch| !ch.is_whitespace())
            .take(3)
            .collect();
        if query.chars().count() >= 2 {
            let found = search_catalog(&config, &query).unwrap_or_else(|err| panic!("{err}"));
            eprintln!(
                "search: {} albums, {} artists",
                found.albums.len(),
                found.artists.len()
            );
        }
    }
}
