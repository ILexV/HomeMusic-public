//! Account-bound listening state. Resolved stream URLs and wave telemetry never reach disk.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::hub::WaveLive;
use crate::navidrome::ServerConfig;
use crate::player::{PlaybackPhase, PlaybackState};
use crate::queue::{Item, Queue};

const SCHEMA: u64 = 1;
const POSITION_INTERVAL: Duration = Duration::from_secs(5);

pub struct SavedPlayback {
    pub queue: Queue,
    pub position_s: f32,
    pub album_id: String,
    pub wave: Option<WaveLive>,
}

pub struct Store {
    path: PathBuf,
    account: String,
    saved_revision: Option<u64>,
    current_token: Option<u64>,
    position_s: f32,
    saved_position_s: f32,
    album_id: String,
    wave: Option<WaveLive>,
    saved_phase: PlaybackPhase,
    last_write: Option<Instant>,
    failed_at: Option<Instant>,
}

impl Store {
    pub fn new(config: &ServerConfig) -> Self {
        Self::at(crate::paths::dir().join("playback-state.json"), config)
    }

    fn at(path: PathBuf, config: &ServerConfig) -> Self {
        Self {
            path,
            account: account_key(config),
            saved_revision: None,
            current_token: None,
            position_s: 0.0,
            saved_position_s: 0.0,
            album_id: String::new(),
            wave: None,
            saved_phase: PlaybackPhase::Idle,
            last_write: None,
            failed_at: None,
        }
    }

    /// Missing, foreign-account, corrupt, and future-schema state is not a session.
    /// `Some` with an empty queue is authoritative, not a library fallback request.
    pub fn load(&mut self) -> Result<Option<SavedPlayback>, String> {
        let bytes = match fs::read(&self.path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err("Не удалось прочитать сохранённую очередь".into()),
        };
        let Some(saved) = decode(&bytes, &self.account) else {
            return Ok(None);
        };
        self.saved_revision = Some(saved.queue.revision());
        self.current_token = saved.queue.current_token();
        self.position_s = saved.position_s;
        self.saved_position_s = saved.position_s;
        self.album_id.clone_from(&saved.album_id);
        self.wave = saved.wave.as_ref().map(safe_wave);
        self.saved_phase = PlaybackPhase::Paused;
        self.last_write = Some(Instant::now());
        Ok(Some(saved))
    }

    /// Call with a snapshot belonging to this queue's current occurrence. Observe
    /// once before stop to capture its final timing; inactive/loading zeroes retain it.
    /// Selection/reorder/context edits save immediately; moving timing is throttled.
    /// `force` flushes dirty timing at shutdown, without rewriting unchanged state.
    pub fn record(
        &mut self,
        queue: &Queue,
        state: &PlaybackState,
        album_id: &str,
        wave: Option<&WaveLive>,
        force: bool,
    ) -> Result<bool, String> {
        self.record_at(queue, state, album_id, wave, force, Instant::now())
    }

    fn record_at(
        &mut self,
        queue: &Queue,
        state: &PlaybackState,
        album_id: &str,
        wave: Option<&WaveLive>,
        force: bool,
        now: Instant,
    ) -> Result<bool, String> {
        let token = queue.current_token();
        if self.current_token != token {
            self.current_token = token;
            self.position_s = 0.0;
        }
        if token.is_some()
            && matches!(state.phase, PlaybackPhase::Playing | PlaybackPhase::Paused | PlaybackPhase::Buffering)
            && state.position_s.is_finite()
            && state.position_s >= 0.0
        {
            self.position_s = usable_position(state.position_s, queue.current());
        }
        let wave = wave.filter(|wave| wave.on());
        let edited = self.saved_revision != Some(queue.revision())
            || self.album_id != album_id
            || !same_wave(self.wave.as_ref(), wave);
        let moved = self.position_s != self.saved_position_s;
        let settled = state.phase != self.saved_phase
            && matches!(state.phase, PlaybackPhase::Paused | PlaybackPhase::Stopped | PlaybackPhase::Ended | PlaybackPhase::Error);
        let due = self.last_write.is_none_or(|last| now.duration_since(last) >= POSITION_INTERVAL);
        if !edited && !(moved && (force || settled || due)) {
            // Remember transitions even when timing has not moved.
            self.saved_phase = state.phase;
            return Ok(false);
        }
        if !force && self.failed_at.is_some_and(|at| now.duration_since(at) < POSITION_INTERVAL) {
            return Ok(false);
        }
        self.failed_at = Some(now);
        let body = encode(&self.account, queue, self.position_s, album_id, wave)?;
        atomic_write(&self.path, &body)
            .map_err(|_| "Не удалось сохранить очередь воспроизведения".to_string())?;
        self.saved_revision = Some(queue.revision());
        self.saved_position_s = self.position_s;
        self.album_id.clear();
        self.album_id.push_str(album_id);
        self.wave = wave.map(safe_wave);
        self.saved_phase = state.phase;
        self.last_write = Some(now);
        self.failed_at = None;
        Ok(true)
    }
}

fn account_key(config: &ServerConfig) -> String {
    use md5::{Digest, Md5};
    let mut hash = Md5::new();
    // Length prefixes make the tuple unambiguous. Never include password or tokens.
    // Hashing also keeps any URL user-info supplied in configuration out of the file.
    for value in [&config.server, &config.hub, &config.user] {
        hash.update((value.len() as u64).to_le_bytes());
        hash.update(value.as_bytes());
    }
    format!("{:x}", hash.finalize())
}

fn safe_wave(wave: &WaveLive) -> WaveLive {
    WaveLive {
        id: wave.id.clone(),
        personal: wave.personal,
        origin: wave.origin.clone(),
        ..WaveLive::default()
    }
}

fn same_wave(left: Option<&WaveLive>, right: Option<&WaveLive>) -> bool {
    match (left, right) {
        (None, None) => true,
        (Some(left), Some(right)) => left.id == right.id && left.personal == right.personal && left.origin == right.origin,
        _ => false,
    }
}

fn usable_position(position: f32, item: Option<&Item>) -> f32 {
    let Some(item) = item else { return 0.0 };
    if !position.is_finite() || position < 0.0 {
        return 0.0;
    }
    if item.duration_s > 0 {
        position.min(item.duration_s as f32)
    } else {
        position
    }
}

fn safe_local_path(path: &str) -> bool {
    // Direct network URLs may carry Navidrome tokens or Hub credentials. Library
    // and catalog tracks already have id/listen_ref and do not need these URLs.
    !path.starts_with("//") && (!path.contains("://") || path.starts_with("file://"))
}

fn encode(
    account: &str,
    queue: &Queue,
    position_s: f32,
    album_id: &str,
    wave: Option<&WaveLive>,
) -> Result<Vec<u8>, String> {
    let mut items = Vec::with_capacity(queue.items().len());
    for item in queue.items() {
        let path = if safe_local_path(&item.path) {
            item.path.as_str()
        } else if !item.id.is_empty() || !item.listen_ref.is_empty() {
            ""
        } else {
            return Err("Прямой сетевой адрес нельзя сохранить в очереди".into());
        };
        items.push(json!({
            "id": item.id, "path": path, "listen_ref": item.listen_ref,
            "title": item.title, "artist": item.artist, "album": item.album,
            "meta": item.meta, "duration_s": item.duration_s,
        }));
    }
    serde_json::to_vec(&json!({
        "schema": SCHEMA, "account": account, "name": queue.name(),
        "items": items, "index": queue.index(),
        "position_s": usable_position(position_s, queue.current()), "album_id": album_id,
        "wave": wave.map(|wave| json!({"id": wave.id, "personal": wave.personal, "origin": wave.origin})),
    })).map_err(|_| "Не удалось подготовить очередь к сохранению".into())
}

fn decode(bytes: &[u8], account: &str) -> Option<SavedPlayback> {
    let value: Value = serde_json::from_slice(bytes).ok()?;
    if value.get("schema")?.as_u64()? != SCHEMA || value.get("account")?.as_str()? != account {
        return None;
    }
    let rows = value.get("items")?.as_array()?;
    let mut items = Vec::with_capacity(rows.len());
    for row in rows {
        let text = |key: &str| row.get(key)?.as_str().map(str::to_owned);
        let item = Item {
            id: text("id")?, path: text("path")?, listen_ref: text("listen_ref")?,
            title: text("title")?, artist: text("artist")?, album: text("album")?,
            meta: text("meta")?, duration_s: u32::try_from(row.get("duration_s")?.as_u64()?).ok()?,
        };
        if !safe_local_path(&item.path) {
            return None;
        }
        items.push(item);
    }
    let mut queue = Queue::from_tracks(value.get("name")?.as_str()?, items);
    let index = value.get("index")?;
    if !index.is_null() {
        let index = usize::try_from(index.as_u64()?).ok()?;
        // A corrupt selection must not restart some other occurrence of a duplicate.
        if !queue.select(index) {
            return None;
        }
    }
    let position_s = usable_position(value.get("position_s")?.as_f64()? as f32, queue.current());
    let wave = match value.get("wave")? {
        Value::Null => None,
        wave => {
            let id = wave.get("id")?.as_str()?.to_owned();
            if id.is_empty() {
                None
            } else {
                Some(WaveLive {
                    id,
                    personal: wave.get("personal")?.as_bool()?,
                    origin: wave.get("origin")?.as_str()?.to_owned(),
                    ..WaveLive::default()
                })
            }
        }
    };
    Some(SavedPlayback {
        queue,
        position_s,
        album_id: value.get("album_id")?.as_str()?.to_owned(),
        wave,
    })
}

fn atomic_write(path: &Path, body: &[u8]) -> std::io::Result<()> {
    static SERIAL: AtomicU64 = AtomicU64::new(1);
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(
        ".playback-state-{}-{}.tmp", std::process::id(), SERIAL.fetch_add(1, Ordering::Relaxed)
    ));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temporary)?;
    let result = (|| {
        file.write_all(body)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, path)?;
        #[cfg(unix)]
        fs::File::open(parent)?.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            static SERIAL: AtomicU64 = AtomicU64::new(1);
            let path = std::env::temp_dir().join(format!(
                "home-music-session-{}-{}", std::process::id(), SERIAL.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        fn store(&self, config: &ServerConfig) -> Store {
            Store::at(self.0.join("playback-state.json"), config)
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn config() -> ServerConfig {
        ServerConfig {
            server: "https://music.example".into(), hub: "https://hub.example".into(),
            user: "listener".into(), password: "never-persist-this".into(), loaded: true,
        }
    }

    fn state(phase: PlaybackPhase, position_s: f32) -> PlaybackState {
        PlaybackState {
            phase, position_s, duration_s: 120.0, volume: 0.8,
            seekable: true, playing: phase == PlaybackPhase::Playing,
        }
    }

    fn queue() -> Queue {
        let mut local = Item::file("/music/one song.flac", "One");
        local.duration_s = 120;
        let mut external = Item::file("", "External");
        external.listen_ref = "youtube:external-ref".into();
        external.duration_s = 80;
        let mut queue = Queue::from_tracks("Daily mix", vec![local.clone(), external, local]);
        queue.select(2);
        queue
    }

    #[test]
    fn round_trip_retains_duplicates_selection_local_paths_refs_and_safe_wave() {
        let fixture = Fixture::new();
        let config = config();
        let mut store = fixture.store(&config);
        let queue = queue();
        let wave = WaveLive {
            id: "wave-1".into(), personal: true, origin: "home".into(),
            pending: vec![json!({"private": "telemetry"})], asking: true,
        };
        assert!(store.record(&queue, &state(PlaybackPhase::Playing, 37.5), "album-1", Some(&wave), false).unwrap());
        let mut reopened = fixture.store(&config);
        let saved = reopened.load().unwrap().unwrap();
        assert_eq!(saved.queue.items(), queue.items());
        assert_eq!(saved.queue.index(), Some(2));
        assert_eq!(saved.position_s, 37.5);
        assert_eq!(saved.album_id, "album-1");
        let restored_wave = saved.wave.unwrap();
        assert!(same_wave(Some(&wave), Some(&restored_wave)));
        assert!(restored_wave.pending.is_empty());
        assert!(!restored_wave.asking);
        assert!(!reopened.record(&saved.queue, &state(PlaybackPhase::Loading, 0.0), "album-1", Some(&restored_wave), true).unwrap());
        assert_eq!(fixture.store(&config).load().unwrap().unwrap().position_s, 37.5);
        let bytes = fs::read_to_string(&store.path).unwrap();
        assert!(!bytes.contains(&config.password));
        assert!(!bytes.contains("telemetry"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(&store.path).unwrap().permissions().mode() & 0o777, 0o600);
        }
    }

    #[test]
    fn empty_queue_is_a_saved_session_and_accounts_do_not_cross_restore() {
        let fixture = Fixture::new();
        let original = config();
        let queue = Queue::from_tracks("Empty", Vec::new());
        fixture.store(&original).record(&queue, &state(PlaybackPhase::Stopped, 0.0), "", None, true).unwrap();
        let saved = fixture.store(&original).load().unwrap().unwrap();
        assert!(saved.queue.is_empty());
        assert_eq!(saved.queue.index(), None);
        for field in 0..3 {
            let mut foreign = original.clone();
            match field {
                0 => foreign.server.push_str("/other"),
                1 => foreign.hub.push_str("/other"),
                _ => foreign.user.push_str("-other"),
            }
            assert!(fixture.store(&foreign).load().unwrap().is_none());
        }
        let mut password_change = original.clone();
        password_change.password = "new-password".into();
        assert!(fixture.store(&password_change).load().unwrap().is_some());
    }

    #[test]
    fn stop_and_reorder_preserve_occurrence_but_duplicate_selection_resets_position() {
        let fixture = Fixture::new();
        let config = config();
        let mut store = fixture.store(&config);
        let mut queue = queue();
        let now = Instant::now();
        assert!(store.record_at(&queue, &state(PlaybackPhase::Playing, 20.0), "", None, false, now).unwrap());
        assert!(!store.record_at(&queue, &state(PlaybackPhase::Playing, 22.0), "", None, false, now + Duration::from_secs(1)).unwrap());
        assert!(store.record_at(&queue, &state(PlaybackPhase::Stopped, 0.0), "", None, false, now + Duration::from_secs(1)).unwrap());
        assert_eq!(fixture.store(&config).load().unwrap().unwrap().position_s, 22.0);
        let token = queue.current_token();
        queue.move_item(2, 0);
        assert_eq!(queue.current_token(), token);
        assert!(store.record_at(&queue, &state(PlaybackPhase::Stopped, 0.0), "", None, false, now + Duration::from_secs(2)).unwrap());
        let saved = fixture.store(&config).load().unwrap().unwrap();
        assert_eq!(saved.queue.index(), Some(0));
        assert_eq!(saved.position_s, 22.0);
        queue.select(1); // Same file, distinct occurrence.
        assert_ne!(queue.current_token(), token);
        assert!(store.record_at(&queue, &state(PlaybackPhase::Loading, 0.0), "", None, false, now + Duration::from_secs(2)).unwrap());
        assert_eq!(fixture.store(&config).load().unwrap().unwrap().position_s, 0.0);
        queue.remove(1); // Current removal switches to its successor.
        assert!(store.record_at(&queue, &state(PlaybackPhase::Stopped, 0.0), "", None, false, now + Duration::from_secs(2)).unwrap());
        assert_eq!(fixture.store(&config).load().unwrap().unwrap().queue.current().unwrap().title, "External");
    }

    #[test]
    fn position_writes_are_throttled_but_pause_and_shutdown_flush() {
        let fixture = Fixture::new();
        let config = config();
        let mut store = fixture.store(&config);
        let queue = queue();
        let now = Instant::now();
        assert!(store.record_at(&queue, &state(PlaybackPhase::Playing, 1.0), "", None, false, now).unwrap());
        for second in 1..5 {
            assert!(!store.record_at(&queue, &state(PlaybackPhase::Playing, second as f32 + 1.0), "", None, false, now + Duration::from_secs(second)).unwrap());
        }
        assert!(store.record_at(&queue, &state(PlaybackPhase::Playing, 6.0), "", None, false, now + Duration::from_secs(5)).unwrap());
        assert!(store.record_at(&queue, &state(PlaybackPhase::Paused, 6.5), "", None, false, now + Duration::from_secs(5)).unwrap());
        assert!(store.record_at(&queue, &state(PlaybackPhase::Playing, 7.0), "", None, true, now + Duration::from_secs(6)).unwrap());
        assert_eq!(fixture.store(&config).load().unwrap().unwrap().position_s, 7.0);
    }

    #[test]
    fn corrupt_truncated_unknown_schema_and_bad_selection_are_ignored() {
        let account = account_key(&config());
        let bytes = encode(&account, &queue(), 25.0, "", None).unwrap();
        for end in 0..bytes.len() {
            assert!(decode(&bytes[..end], &account).is_none());
        }
        let mut value: Value = serde_json::from_slice(&bytes).unwrap();
        value["schema"] = json!(SCHEMA + 1);
        assert!(decode(&serde_json::to_vec(&value).unwrap(), &account).is_none());
        value["schema"] = json!(SCHEMA);
        value["index"] = json!(99);
        assert!(decode(&serde_json::to_vec(&value).unwrap(), &account).is_none());
        value["index"] = json!(2);
        value["position_s"] = json!(-5.0);
        assert_eq!(decode(&serde_json::to_vec(&value).unwrap(), &account).unwrap().position_s, 0.0);
        value["position_s"] = json!(900.0);
        assert_eq!(decode(&serde_json::to_vec(&value).unwrap(), &account).unwrap().position_s, 120.0);
        value["items"][0]["duration_s"] = json!("broken");
        assert!(decode(&serde_json::to_vec(&value).unwrap(), &account).is_none());
    }

    #[test]
    fn resolved_urls_are_not_saved_and_failed_save_keeps_previous_state() {
        let fixture = Fixture::new();
        let config = config();
        let mut store = fixture.store(&config);
        let mut item = Item::file("https://music.example/rest/stream?token=secret", "Library");
        item.id = "song-1".into();
        let mut queue = Queue::from_tracks("Library", vec![item]);
        queue.select(0);
        store.record(&queue, &state(PlaybackPhase::Paused, 12.0), "", None, true).unwrap();
        let previous = fs::read(&store.path).unwrap();
        assert!(!String::from_utf8_lossy(&previous).contains("secret"));
        let restored = fixture.store(&config).load().unwrap().unwrap();
        assert_eq!(restored.queue.current().unwrap().id, "song-1");
        assert!(restored.queue.current().unwrap().path.is_empty());
        let mut unsafe_queue = Queue::from_path("https://music.example/rest/stream?token=secret", "URL", "");
        unsafe_queue.select(0);
        assert!(store.record(&unsafe_queue, &state(PlaybackPhase::Paused, 0.0), "", None, true).is_err());
        assert_eq!(fs::read(&store.path).unwrap(), previous);
        // A rename failure cannot truncate or partially overwrite its destination.
        let directory = fixture.0.join("occupied");
        fs::create_dir(&directory).unwrap();
        fs::write(directory.join("keep"), b"original").unwrap();
        assert!(atomic_write(&directory, b"replacement").is_err());
        assert_eq!(fs::read(directory.join("keep")).unwrap(), b"original");
        assert!(!fs::read_dir(&fixture.0).unwrap().any(|entry| entry.unwrap().file_name().to_string_lossy().ends_with(".tmp")));
    }
}
