//! Desktop-only, account-scoped full recordings. Network and mutations run on one worker.
//! Explicit Save pins recordings; Cache queue downloads evictable recordings. Playback does
//! not silently download anything. Only canonical metadata is persisted, never stream URLs.
use std::cell::{Cell, RefCell};
use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use md5::{Digest, Md5};
use serde_json::{json, Value};
use slint::{ComponentHandle, ModelRc, VecModel};
use crate::{hub::Hub, navidrome::ServerConfig, queue::{Item, Queue}};

const DEFAULT_QUOTA: u64 = 2 * 1024 * 1024 * 1024;
const MIB: u64 = 1024 * 1024;
const QUOTA_ERROR: &str = "Недостаточно места: снимите закрепление, удалите записи или увеличьте лимит";

#[derive(Clone)]
struct Entry { key: String, item: Item, bytes: u64, pinned: bool, touched: u64 }
struct Index { entries: Vec<Entry>, quota: u64, writable: bool }
#[derive(Clone)]
struct View { entries: Vec<Entry>, quota: u64, status: String, busy: bool, progress: i32, revision: u64, list_revision: u64 }

enum Command {
    Save(Vec<Item>, bool, u64), Pin(String, bool), Remove(String), Clear, Quota(u64), Refresh,
}

pub(crate) struct Cache {
    root: PathBuf,
    view: Mutex<View>,
    protected: Mutex<Option<String>>,
    epoch: AtomicU64,
    tx: mpsc::SyncSender<Command>,
    receiver: Mutex<Option<mpsc::Receiver<Command>>>,
    initial: Mutex<Option<Index>>,
}

fn key(item: &Item) -> Option<String> {
    // Library ids and online refs occupy different namespaces. Local paths are not catalog ids.
    let identity = if !item.id.is_empty() { format!("nd:{}", item.id) }
        else if !item.listen_ref.is_empty() { format!("ref:{}", item.listen_ref) }
        else { return None };
    Some(format!("{:x}", Md5::digest(identity.as_bytes())))
}
fn stamp() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64 }
fn account(config: &ServerConfig) -> String {
    let identity = json!([config.server.trim_end_matches('/'), crate::hub::hub_base(&config.server, &config.hub).trim_end_matches('/'), config.user]);
    format!("{:x}", Md5::digest(identity.to_string().as_bytes()))
}
fn storage_root() -> PathBuf {
    if let Some(path) = std::env::var_os("LOCALAPPDATA").filter(|_| cfg!(windows)) { return PathBuf::from(path).join("HomeMusic").join("offline") }
    if let Some(path) = std::env::var_os("XDG_DATA_HOME").filter(|p| !p.is_empty()) { return PathBuf::from(path).join("homemusic/offline") }
    if let Some(home) = std::env::var_os("HOME") { return PathBuf::from(home).join(".local/share/homemusic/offline") }
    crate::paths::dir().join("offline")
}
fn audio_path(root: &Path, key: &str) -> PathBuf { root.join(format!("{key}.audio")) }
fn canonical(item: &Item) -> Item { let mut item = item.clone(); item.path.clear(); item }
fn disk_error() -> String { "Не удалось записать хранилище: проверьте свободное место и права доступа".into() }
fn item_json(item: &Item) -> Value {
    json!({"id": item.id, "ref": item.listen_ref, "title": item.title, "artist": item.artist,
        "album": item.album, "meta": item.meta, "duration_s": item.duration_s})
}
fn parse_item(value: &Value) -> Option<Item> {
    let text = |name| value.get(name).and_then(Value::as_str).unwrap_or("").to_owned();
    let item = Item { id: text("id"), listen_ref: text("ref"), path: String::new(), title: text("title"),
        artist: text("artist"), album: text("album"), meta: text("meta"),
        duration_s: value.get("duration_s").and_then(Value::as_u64).unwrap_or(0).min(u32::MAX as u64) as u32 };
    key(&item)?;
    Some(item)
}
fn persist(root: &Path, index: &Index) -> Result<(), String> {
    if !index.writable { return Err("Хранилище недоступно; очистите его для восстановления".into()) }
    let entries: Vec<Value> = index.entries.iter().map(|e| json!({"item": item_json(&e.item), "bytes": e.bytes,
        "pinned": e.pinned, "touched": e.touched})).collect();
    let bytes = serde_json::to_vec(&json!({"version": 1, "quota": index.quota, "entries": entries})).map_err(|_| disk_error())?;
    let temp = root.join("index.pending");
    let mut file = File::create(&temp).map_err(|_| disk_error())?;
    file.write_all(&bytes).and_then(|_| file.sync_all()).map_err(|_| disk_error())?;
    drop(file);
    fs::rename(temp, root.join("index.json")).map_err(|_| disk_error())?;
    #[cfg(unix)]
    File::open(root).and_then(|f| f.sync_all()).map_err(|_| disk_error())?;
    Ok(())
}
fn load(root: &Path) -> Result<Index, String> {
    fs::create_dir_all(root).map_err(|_| disk_error())?;
    let path = root.join("index.json");
    let mut index = Index { entries: Vec::new(), quota: DEFAULT_QUOTA, writable: true };
    if path.exists() {
        let bytes = fs::read(path).map_err(|_| disk_error())?;
        let body: Value = serde_json::from_slice(&bytes).map_err(|_| "Индекс офлайн повреждён; очистите хранилище для восстановления".to_string())?;
        if body["version"].as_u64() != Some(1) { return Err("Неизвестная версия офлайн-хранилища".into()) }
        index.quota = body["quota"].as_u64().unwrap_or(DEFAULT_QUOTA).max(1);
        let mut seen = HashSet::new();
        for row in body["entries"].as_array().into_iter().flatten() {
            let Some(item) = parse_item(&row["item"]) else { continue };
            let key = key(&item).unwrap();
            let bytes = row["bytes"].as_u64().unwrap_or(0);
            if bytes == 0 || !seen.insert(key.clone()) { continue }
            if fs::symlink_metadata(audio_path(root, &key)).map(|m| m.file_type().is_file() && m.len() == bytes).unwrap_or(false) {
                index.entries.push(Entry { key, item, bytes, pinned: row["pinned"].as_bool().unwrap_or(false), touched: row["touched"].as_u64().unwrap_or(0) });
            }
        }
    }
    // Crash remnants are never exposed as playable files or allowed to escape the quota.
    let known: HashSet<String> = index.entries.iter().map(|e| format!("{}.audio", e.key)).collect();
    for entry in fs::read_dir(root).map_err(|_| disk_error())? {
        let entry = entry.map_err(|_| disk_error())?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.ends_with(".part") || name == "index.pending" || (name.ends_with(".audio") && !known.contains(&name)) {
            fs::remove_file(entry.path()).map_err(|_| disk_error())?;
        }
    }
    persist(root, &index)?;
    Ok(index)
}

impl Cache {
    pub(crate) fn open(config: &ServerConfig) -> Arc<Self> { Self::at(storage_root().join(account(config))) }
    fn at(root: PathBuf) -> Arc<Self> {
        let (index, status) = match load(&root) {
            Ok(index) => (index, "Сохраняйте трек или очередь; кэш очереди можно вытеснять автоматически".into()),
            Err(error) => (Index { entries: Vec::new(), quota: DEFAULT_QUOTA, writable: false }, error),
        };
        let (tx, rx) = mpsc::sync_channel(16);
        Arc::new(Self { root, view: Mutex::new(View { entries: index.entries.clone(), quota: index.quota, status,
            busy: false, progress: 0, revision: 1, list_revision: 1 }), protected: Mutex::new(None), epoch: AtomicU64::new(1),
            tx, receiver: Mutex::new(Some(rx)), initial: Mutex::new(Some(index)) })
    }
    pub(crate) fn local_path(&self, item: &Item) -> Option<String> {
        let key = key(item)?;
        let bytes = self.view.lock().unwrap().entries.iter().find(|e| e.key == key)?.bytes;
        let path = audio_path(&self.root, &key);
        let metadata = fs::symlink_metadata(&path).ok()?;
        (metadata.file_type().is_file() && metadata.len() == bytes).then(|| path.to_string_lossy().into_owned())
    }
    pub(crate) fn protect_current(&self, item: Option<&Item>) { *self.protected.lock().unwrap() = item.and_then(key); }
    fn publish(&self, index: &Index) {
        let mut view = self.view.lock().unwrap();
        if view.entries != index.entries {
            view.entries = index.entries.clone();
            view.list_revision = view.list_revision.wrapping_add(1);
        }
        view.quota = index.quota;
        view.revision = view.revision.wrapping_add(1);
    }
    fn status(&self, status: impl Into<String>, busy: bool, progress: i32) {
        let mut view = self.view.lock().unwrap();
        view.status = status.into(); view.busy = busy; view.progress = progress;
        view.revision = view.revision.wrapping_add(1);
    }
    fn send(&self, command: Command) {
        if self.tx.try_send(command).is_err() {
            let busy = self.view.lock().unwrap().busy;
            self.status("Слишком много операций; дождитесь завершения или отмените загрузку", busy, 0);
        }
    }
    fn cancel(&self) {
        self.epoch.fetch_add(1, Ordering::SeqCst);
        let busy = self.view.lock().unwrap().busy;
        if busy { self.status("Отмена загрузки…", true, 0); }
    }
    fn start(self: &Arc<Self>, config: ServerConfig, hub: Arc<Hub>) {
        let Some(rx) = self.receiver.lock().unwrap().take() else { return };
        let mut index = self.initial.lock().unwrap().take().unwrap();
        let cache = self.clone();
        std::thread::spawn(move || {
            while let Ok(command) = rx.recv() {
                let result = match command {
                    Command::Save(items, pinned, epoch) => cache.save_batch(&mut index, items, pinned, epoch, &config, &hub),
                    Command::Pin(key, pinned) => {
                        let previous = index.entries.iter().find(|e| e.key == key).map(|e| e.pinned);
                        if let Some(entry) = index.entries.iter_mut().find(|e| e.key == key) { entry.pinned = pinned; }
                        let result = persist(&cache.root, &index);
                        if result.is_err() {
                            if let (Some(old), Some(entry)) = (previous, index.entries.iter_mut().find(|e| e.key == key)) { entry.pinned = old; }
                        }
                        result.map(|_| if pinned { "Запись закреплена".into() } else { "Закрепление снято; запись остаётся доступной до вытеснения".into() })
                    }
                    Command::Remove(key) => cache.remove(&mut index, &key).map(|_| "Локальная копия удалена; очередь не изменена".into()),
                    Command::Clear => cache.clear(&mut index),
                    Command::Quota(quota) => cache.set_quota(&mut index, quota).map(|_| "Лимит сохранён".into()),
                    Command::Refresh => cache.reconcile(&mut index).map(|_| String::new()),
                };
                cache.publish(&index);
                match result { Ok(status) if !status.is_empty() => cache.status(status, false, 100), Err(error) => cache.status(error, false, 0), _ => {} }
            }
        });
    }
    fn clear(&self, index: &mut Index) -> Result<String, String> {
        if !index.writable {
            fs::create_dir_all(&self.root).map_err(|_| disk_error())?;
            for entry in fs::read_dir(&self.root).map_err(|_| disk_error())? {
                let entry = entry.map_err(|_| disk_error())?;
                let name = entry.file_name().to_string_lossy().into_owned();
                if name.ends_with(".audio") || name.ends_with(".part") || name == "index.pending" {
                    fs::remove_file(entry.path()).map_err(|_| disk_error())?;
                }
            }
            index.writable = true;
            if let Err(error) = persist(&self.root, index) { index.writable = false; return Err(error) }
        }
        let keys: Vec<String> = index.entries.iter().map(|e| e.key.clone()).collect();
        let mut result = Ok("Хранилище очищено".into());
        for key in keys { if let Err(error) = self.remove(index, &key) { result = Err(error); } }
        result
    }
    fn reconcile(&self, index: &mut Index) -> Result<(), String> {
        let before = index.entries.len();
        index.entries.retain(|e| fs::symlink_metadata(audio_path(&self.root, &e.key)).map(|m| m.file_type().is_file() && m.len() == e.bytes).unwrap_or(false));
        if before != index.entries.len() { persist(&self.root, index)?; }
        Ok(())
    }
    fn remove(&self, index: &mut Index, key: &str) -> Result<(), String> {
        // Keep this lock through deletion so playback protection cannot race eviction.
        let protected = self.protected.lock().unwrap();
        if protected.as_deref() == Some(key) { return Err("Текущий трек сохранён: переключите трек перед удалением его локальной копии".into()) }
        let path = audio_path(&self.root, key);
        match fs::remove_file(path) { Ok(()) => {}, Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}, Err(_) => return Err(disk_error()) }
        index.entries.retain(|e| e.key != key);
        drop(protected);
        persist(&self.root, index)
    }
    fn make_room(&self, index: &mut Index, incoming: u64) -> Result<(), String> {
        let protected = self.protected.lock().unwrap().clone();
        let required: u64 = index.entries.iter().filter(|e| e.pinned || protected.as_deref() == Some(&e.key)).map(|e| e.bytes).sum();
        if required.saturating_add(incoming) > index.quota { return Err(QUOTA_ERROR.into()) }
        loop {
            let used: u64 = index.entries.iter().map(|e| e.bytes).sum();
            if used.saturating_add(incoming) <= index.quota { return Ok(()) }
            let protected = self.protected.lock().unwrap().clone();
            let candidate = index.entries.iter().filter(|e| !e.pinned && protected.as_deref() != Some(&e.key))
                .min_by_key(|e| e.touched).map(|e| e.key.clone()).ok_or_else(|| QUOTA_ERROR.to_string())?;
            self.remove(index, &candidate)?;
            self.publish(index);
        }
    }
    fn set_quota(&self, index: &mut Index, quota: u64) -> Result<(), String> {
        if !index.writable { return Err("Хранилище недоступно; очистите его для восстановления".into()) }
        let protected = self.protected.lock().unwrap().clone();
        let required: u64 = index.entries.iter().filter(|e| e.pinned || protected.as_deref() == Some(&e.key)).map(|e| e.bytes).sum();
        if quota < required || quota == 0 { return Err(QUOTA_ERROR.into()) }
        let old = index.quota;
        index.quota = quota;
        if let Err(error) = self.make_room(index, 0).and_then(|_| persist(&self.root, index)) { index.quota = old; return Err(error) }
        Ok(())
    }
    fn check_cancel(&self, epoch: u64) -> Result<(), String> {
        if self.epoch.load(Ordering::SeqCst) != epoch { Err("Загрузка отменена; завершённые записи сохранены".into()) } else { Ok(()) }
    }
    fn save_batch(&self, index: &mut Index, items: Vec<Item>, pinned: bool, epoch: u64, config: &ServerConfig, hub: &Hub) -> Result<String, String> {
        if !index.writable { return Err("Хранилище недоступно; очистите его для восстановления".into()) }
        self.reconcile(index)?;
        let mut seen = HashSet::new();
        let mut saved = 0;
        let mut errors = 0;
        let mut last_error = String::new();
        for item in items {
            self.check_cancel(epoch)?;
            let Some(key) = key(&item) else { continue };
            if !seen.insert(key.clone()) { continue }
            self.status(format!("Загрузка: {}", item.title), true, 0);
            match self.save_one(index, &item, pinned, epoch, config, hub) {
                Ok(()) => saved += 1,
                Err(error) => { errors += 1; last_error = error; }
            }
            self.publish(index);
        }
        self.check_cancel(epoch)?;
        if errors > 0 { Err(format!("Сохранено: {saved}; ошибок: {errors}. {last_error}")) }
        else if saved == 0 { Err("В очереди нет библиотечных или сетевых треков для сохранения".into()) }
        else { Ok(format!("Доступно без сети: {saved} записей")) }
    }
    fn save_one(&self, index: &mut Index, item: &Item, pinned: bool, epoch: u64, config: &ServerConfig, hub: &Hub) -> Result<(), String> {
        let key = key(item).ok_or_else(|| "Этот трек уже является локальным файлом".to_string())?;
        if let Some(position) = index.entries.iter().position(|e| e.key == key) {
            let previous = index.entries[position].pinned;
            index.entries[position].pinned |= pinned;
            index.entries[position].touched = stamp();
            let result = persist(&self.root, index);
            if result.is_err() { index.entries[position].pinned = previous; }
            return result;
        }
        let response = if !item.id.is_empty() {
            ureq::AgentBuilder::new().timeout_connect(Duration::from_secs(8)).timeout_read(Duration::from_secs(20))
                .timeout(Duration::from_secs(600)).build().get(&crate::navidrome::stream_url(config, &item.id)).call()
                .map_err(|_| "Не удалось загрузить полный трек из библиотеки".to_string())?
        } else { hub.full_stream(&item.listen_ref)? };
        self.check_cancel(epoch)?;
        let content_type = response.header("Content-Type").unwrap_or("").to_ascii_lowercase();
        if content_type.contains("json") || content_type.contains("html") || content_type.starts_with("text/") {
            return Err("Сервер вернул не аудиозапись".into());
        }
        let expected = response.header("Content-Length").and_then(|s| s.parse::<u64>().ok());
        self.store_reader(index, item, pinned, epoch, response.into_reader(), expected)
    }
    fn store_reader(&self, index: &mut Index, item: &Item, pinned: bool, epoch: u64, mut reader: impl Read, expected: Option<u64>) -> Result<(), String> {
        self.check_cancel(epoch)?;
        let key = key(item).unwrap();
        if let Some(bytes) = expected { self.make_room(index, bytes)?; }
        let temp = self.root.join(format!("{key}.part"));
        let result = (|| {
            let mut file = File::create(&temp).map_err(|_| disk_error())?;
            let mut bytes = 0u64;
            let mut last_progress = std::time::Instant::now();
            let mut buffer = [0u8; 64 * 1024];
            loop {
                self.check_cancel(epoch)?;
                let count = reader.read(&mut buffer).map_err(|_| "Загрузка оборвалась; незавершённая запись удалена".to_string())?;
                if count == 0 { break }
                bytes = bytes.saturating_add(count as u64);
                if expected.is_some_and(|n| bytes > n) { return Err("Неверный размер загрузки; запись не сохранена".into()) }
                if expected.is_none() { self.make_room(index, bytes)?; }
                file.write_all(&buffer[..count]).map_err(|_| disk_error())?;
                if last_progress.elapsed() >= Duration::from_millis(250) {
                    let progress = expected.filter(|n| *n > 0).map(|n| ((bytes.saturating_mul(100) / n).min(99)) as i32).unwrap_or(0);
                    self.status(format!("{} — {:.1} МиБ", item.title, bytes as f64 / MIB as f64), true, progress);
                    last_progress = std::time::Instant::now();
                }
            }
            if bytes == 0 || expected.is_some_and(|n| n != bytes) { return Err("Неполная загрузка; запись не сохранена".into()) }
            file.sync_all().map_err(|_| disk_error())?;
            drop(file);
            self.status(format!("Проверка полной записи: {}", item.title), true, 99);
            validate_audio(&temp, item.duration_s, || self.check_cancel(epoch))?;
            self.check_cancel(epoch)?;
            fs::rename(&temp, audio_path(&self.root, &key)).map_err(|_| disk_error())?;
            index.entries.push(Entry { key: key.clone(), item: canonical(item), bytes, pinned, touched: stamp() });
            if let Err(error) = persist(&self.root, index) {
                index.entries.retain(|e| e.key != key);
                let _ = fs::remove_file(audio_path(&self.root, &key));
                return Err(error);
            }
            Ok(())
        })();
        if result.is_err() { let _ = fs::remove_file(temp); }
        result
    }
}

// Decode the entire recording with the same codec stack as audible playback. A
// fixed-rate sample counter proves decoded length; container duration alone can
// describe a full song even when only its first packets were downloaded.
fn validate_audio(path: &Path, expected_seconds: u32, check: impl Fn() -> Result<(), String>) -> Result<(), String> {
    use libmpv2::{events::Event, mpv_end_file_reason as reason, Mpv};
    use crate::player::analysis::Metadata;
    let invalid = || "Неполная или повреждённая аудиозапись; локальная копия не сохранена".to_string();
    check()?;
    let mpv = Mpv::with_initializer(|init| {
        init.set_option("config", "no")?;
        init.set_option("load-scripts", "no")?;
        init.set_option("vid", "no")?;
        init.set_option("audio-display", "no")?;
        init.set_option("ytdl", "no")?;
        init.set_option("access-references", "no")?;
        init.set_option("msg-level", "all=no")?;
        init.set_option("ao", "null")?;
        init.set_option("ao-null-untimed", "yes")?;
        init.set_option("keep-open", "always")?;
        init.set_option("network-timeout", "5")?;
        init.set_option("af", "@offline:lavfi=[aformat=sample_rates=48000,astats=metadata=1:reset=0:measure_overall=Number_of_samples:measure_perchannel=none]")?;
        Ok(())
    }).map_err(|_| "Не удалось запустить проверку аудио через libmpv".to_string())?;
    // SAFETY: the live Mpv owns this handle. mpv copies the static C string;
    // requesting log events does not transfer ownership or retain Rust storage.
    let logs = unsafe { libmpv2_sys::mpv_request_log_messages(mpv.ctx.as_ptr(), c"error".as_ptr()) };
    if logs < 0 { return Err(invalid()) }
    mpv.command("loadfile", &[&path.to_string_lossy(), "replace"]).map_err(|_| invalid())?;
    let deadline = std::time::Instant::now() + Duration::from_secs(180);
    let mut loaded = false;
    let mut declared_seconds = 0.0f64;
    loop {
        check()?;
        if std::time::Instant::now() >= deadline {
            return Err("Проверка аудио превысила 180 секунд; локальная копия не сохранена".into());
        }
        match mpv.wait_event(0.025) {
            Some(Ok(Event::FileLoaded)) => loaded = true,
            Some(Err(_)) | Some(Ok(Event::QueueOverflow | Event::Shutdown))
                | Some(Ok(Event::EndFile(reason::Error | reason::Redirect | reason::Stop | reason::Quit)))
                | Some(Ok(Event::LogMessage { .. })) => return Err(invalid()),
            _ => {}
        }
        if !loaded { continue }
        if let Ok(duration) = mpv.get_property::<f64>("duration") {
            if duration.is_finite() && duration > 0.0 { declared_seconds = declared_seconds.max(duration); }
        }
        // libmpv2's bool getter allocates a Rust bool for an mpv int flag.
        // Match Player's string conversion instead of risking a native overwrite.
        if mpv.get_property::<String>("eof-reached").is_ok_and(|value| value == "yes" || value == "true") {
            // keep-open retains the final filter metadata after decoding EOF.
            // Read its complete map: nested af-metadata properties crash mpv 0.37.
            let metadata = mpv.get_property::<Metadata>("af-metadata/offline").map_err(|_| invalid())?;
            let samples = metadata.number("lavfi.astats.Overall.Number_of_samples").ok_or_else(invalid)?;
            let seconds = samples / 48000.0;
            let too_short = |expected: f64, minimum_tolerance: f64| expected > 0.0
                && seconds + (expected * 0.02).clamp(minimum_tolerance, 2.0) < expected;
            // Catalog duration is an integer and may round 1.5 seconds to 2.
            // Allow that rounding plus one audio-frame margin, without relaxing
            // the more precise container-duration or actual decoded-byte checks.
            if !seconds.is_finite() || seconds <= 0.0
                || too_short(declared_seconds, 0.1) || too_short(expected_seconds as f64, 0.55)
            { return Err(invalid()) }
            // Errors already queued before EOF must not be lost behind an
            // otherwise successful sample-count check.
            while let Some(event) = mpv.wait_event(0.0) {
                if matches!(event, Err(_) | Ok(Event::LogMessage { .. } | Event::QueueOverflow | Event::EndFile(reason::Error))) {
                    return Err(invalid());
                }
            }
            check()?;
            return Ok(());
        }
    }
}

pub(crate) struct State {
    timer: slint::Timer,
    ctx: crate::feature_context::Context,
    tools: slint::Weak<crate::ToolsWindow>,
    rows: RefCell<Vec<Entry>>,
    revision: Cell<u64>,
    list_revision: Cell<u64>,
    ticks: Cell<u32>,
}
impl State {
    fn cache(&self) -> &Arc<Cache> { &self.ctx.hub.offline }
    fn render(&self) {
        self.cache().protect_current(self.ctx.queue.borrow().current());
        let tick = self.ticks.get().wrapping_add(1); self.ticks.set(tick);
        if tick % 20 == 0 && !self.cache().view.lock().unwrap().busy {
            let _ = self.cache().tx.try_send(Command::Refresh);
        }
        let Some(tools) = self.tools.upgrade() else { return };
        let view = self.cache().view.lock().unwrap();
        if view.revision == self.revision.get() { return }
        self.revision.set(view.revision);
        let used: u64 = view.entries.iter().map(|e| e.bytes).sum();
        tools.set_offline_status(view.status.clone().into());
        tools.set_offline_busy(view.busy);
        tools.set_offline_progress(view.progress);
        tools.set_offline_usage(format!("{:.1} / {:.0} МиБ · {} записей", used as f64 / MIB as f64, view.quota as f64 / MIB as f64, view.entries.len()).into());
        let quota = (view.quota / MIB).min(i32::MAX as u64) as i32;
        if tools.get_offline_quota_mib() != quota { tools.set_offline_quota_mib(quota); }
        if self.list_revision.get() == view.list_revision { return }
        self.list_revision.set(view.list_revision);
        let mut entries = view.entries.clone();
        drop(view);
        entries.sort_by(|a, b| a.item.artist.cmp(&b.item.artist).then(a.item.title.cmp(&b.item.title)).then(a.key.cmp(&b.key)));
        if *self.rows.borrow() != entries {
            let rows: Vec<crate::OfflineRow> = entries.iter().map(|entry| crate::OfflineRow {
                key: entry.key.clone().into(), title: entry.item.title.clone().into(),
                detail: format!("{} · {} · {:.1} МиБ", entry.item.artist, entry.item.album, entry.bytes as f64 / MIB as f64).into(), pinned: entry.pinned,
            }).collect();
            tools.set_offline_rows(ModelRc::new(VecModel::from(rows)));
            *self.rows.borrow_mut() = entries;
        }
    }
    fn save(&self, current: bool, pinned: bool) {
        let queue = self.ctx.queue.borrow();
        let items = if current { queue.current().cloned().into_iter().collect() } else { queue.items().to_vec() };
        self.cache().send(Command::Save(items, pinned, self.cache().epoch.load(Ordering::SeqCst)));
    }
    fn play(&self, selected: &str) {
        let entries = self.rows.borrow();
        let mut items = Vec::new();
        let mut selected_index = None;
        for entry in entries.iter() {
            if self.cache().local_path(&entry.item).is_none() { continue }
            if entry.key == selected { selected_index = Some(items.len()); }
            // Keep canonical metadata; current_target resolves the managed copy before network.
            items.push(entry.item.clone());
        }
        let Some(index) = selected_index else { self.cache().status("Локальный файл удалён; обновите список", false, 0); self.cache().send(Command::Refresh); return };
        let mut queue = Queue::from_tracks("Офлайн", items);
        queue.select(index);
        drop(entries);
        self.ctx.launch_queue(queue);
    }
}
impl PartialEq for Entry {
    fn eq(&self, other: &Self) -> bool { self.key == other.key && self.item == other.item && self.bytes == other.bytes && self.pinned == other.pinned }
}

pub(crate) fn bind(ctx: &crate::feature_context::Context, tools: &crate::ToolsWindow) -> Rc<State> {
    ctx.hub.offline.start((*ctx.config).clone(), ctx.hub.hub.clone());
    let state = Rc::new(State { timer: slint::Timer::default(), ctx: ctx.clone(), tools: tools.as_weak(),
        rows: RefCell::new(Vec::new()), revision: Cell::new(0), list_revision: Cell::new(0), ticks: Cell::new(0) });
    let weak = Rc::downgrade(&state);
    tools.on_offline_save_current(move || { if let Some(s) = weak.upgrade() { s.save(true, true); } });
    let weak = Rc::downgrade(&state);
    tools.on_offline_save_queue(move || { if let Some(s) = weak.upgrade() { s.save(false, true); } });
    let weak = Rc::downgrade(&state);
    tools.on_offline_cache_queue(move || { if let Some(s) = weak.upgrade() { s.save(false, false); } });
    let weak = Rc::downgrade(&state);
    tools.on_offline_cancel(move || { if let Some(s) = weak.upgrade() { s.cache().cancel(); } });
    let weak = Rc::downgrade(&state);
    tools.on_offline_play(move |key| { if let Some(s) = weak.upgrade() { s.play(key.as_str()); } });
    let weak = Rc::downgrade(&state);
    tools.on_offline_pin(move |key, pinned| { if let Some(s) = weak.upgrade() { s.cache().send(Command::Pin(key.to_string(), pinned)); } });
    let weak = Rc::downgrade(&state);
    tools.on_offline_remove(move |key| { if let Some(s) = weak.upgrade() {
        s.cache().protect_current(s.ctx.queue.borrow().current()); s.cache().cancel(); s.cache().send(Command::Remove(key.to_string()));
    } });
    let weak = Rc::downgrade(&state);
    tools.on_offline_clear(move || { if let Some(s) = weak.upgrade() {
        s.cache().protect_current(s.ctx.queue.borrow().current()); s.cache().cancel(); s.cache().send(Command::Clear);
    } });
    let weak = Rc::downgrade(&state);
    tools.on_offline_set_quota(move |mib| { if let Some(s) = weak.upgrade() {
        if mib > 0 { s.cache().send(Command::Quota(mib as u64 * MIB)); }
    } });
    let weak = Rc::downgrade(&state);
    tools.on_offline_refresh(move || { if let Some(s) = weak.upgrade() { s.cache().send(Command::Refresh); } });
    let weak = Rc::downgrade(&state);
    state.timer.start(slint::TimerMode::Repeated, Duration::from_millis(250), move || { if let Some(s) = weak.upgrade() { s.render(); } });
    state.render();
    state
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Temp(PathBuf);
    impl Temp { fn new() -> Self { static N: AtomicU64 = AtomicU64::new(0); let p = std::env::temp_dir().join(format!("homemusic-offline-{}-{}-{}", std::process::id(), stamp(), N.fetch_add(1, Ordering::Relaxed))); fs::create_dir_all(&p).unwrap(); Self(p) } }
    impl Drop for Temp { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); } }
    fn item(id: &str) -> Item { Item { id: id.into(), path: "https://secret.invalid/?token=secret".into(), listen_ref: String::new(), title: id.into(), artist: "artist".into(), album: "album".into(), meta: String::new(), duration_s: 1 } }
    fn wav() -> Vec<u8> {
        let len = 8000u32 * 2;
        let mut out = Vec::new(); out.extend(b"RIFF"); out.extend((36 + len).to_le_bytes()); out.extend(b"WAVEfmt "); out.extend(16u32.to_le_bytes());
        out.extend(1u16.to_le_bytes()); out.extend(1u16.to_le_bytes()); out.extend(8000u32.to_le_bytes()); out.extend(16000u32.to_le_bytes());
        out.extend(2u16.to_le_bytes()); out.extend(16u16.to_le_bytes()); out.extend(b"data"); out.extend(len.to_le_bytes()); out.resize(44 + len as usize, 0); out
    }
    fn save(cache: &Cache, index: &mut Index, id: &str, pinned: bool) -> Result<(), String> { let data = wav(); cache.store_reader(index, &item(id), pinned, cache.epoch.load(Ordering::SeqCst), data.as_slice(), Some(data.len() as u64)) }
    #[test]
    fn saved_bytes_survive_restart_without_persisted_credentials() {
        let dir = Temp::new(); let cache = Cache::at(dir.0.clone()); let mut index = cache.initial.lock().unwrap().take().unwrap();
        save(&cache, &mut index, "one", true).unwrap(); cache.publish(&index);
        let reopened = Cache::at(dir.0.clone()); assert!(reopened.local_path(&item("one")).is_some());
        let persisted = fs::read_to_string(dir.0.join("index.json")).unwrap(); assert!(!persisted.contains("secret"));
        fs::remove_file(reopened.local_path(&item("one")).unwrap()).unwrap(); assert!(reopened.local_path(&item("one")).is_none());
    }
    #[test]
    fn quota_evicts_only_unpinned_and_not_current() {
        let dir = Temp::new(); let cache = Cache::at(dir.0.clone()); let mut index = cache.initial.lock().unwrap().take().unwrap();
        index.quota = wav().len() as u64 * 2;
        save(&cache, &mut index, "pinned", true).unwrap(); save(&cache, &mut index, "evictable", false).unwrap();
        save(&cache, &mut index, "replacement", false).unwrap(); assert!(!index.entries.iter().any(|e| e.item.id == "evictable"));
        cache.protect_current(Some(&item("replacement")));
        assert!(save(&cache, &mut index, "blocked", true).is_err()); assert_eq!(index.entries.len(), 2);
        assert!(cache.remove(&mut index, &key(&item("replacement")).unwrap()).is_err());
        assert!(cache.set_quota(&mut index, wav().len() as u64).is_err());
    }
    #[test]
    fn incomplete_or_cancelled_download_never_becomes_available() {
        let dir = Temp::new(); let cache = Cache::at(dir.0.clone()); let mut index = cache.initial.lock().unwrap().take().unwrap(); let data = wav();
        assert!(cache.store_reader(&mut index, &item("short"), true, 1, &data[..100], Some(data.len() as u64)).is_err());
        assert!(cache.store_reader(&mut index, &item("chunked-short"), true, 1, &data[..100], None).is_err());
        cache.cancel(); assert!(cache.store_reader(&mut index, &item("cancel"), true, 1, data.as_slice(), Some(data.len() as u64)).is_err());
        let mut preview = item("preview"); preview.duration_s = 180;
        assert!(cache.store_reader(&mut index, &preview, true, 2, data.as_slice(), None).is_err());
        assert!(index.entries.is_empty()); assert!(!fs::read_dir(&dir.0).unwrap().any(|e| e.unwrap().path().extension().is_some_and(|e| e == "part")));
    }
    #[test]
    fn native_library_codecs_save_and_reopen_without_network() {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("testdata");
        for name in ["tone.opus", "tone.mp3", "tone.m4a"] {
            let dir = Temp::new(); let cache = Cache::at(dir.0.clone());
            let mut index = cache.initial.lock().unwrap().take().unwrap();
            let data = fs::read(root.join(name)).unwrap();
            let mut track = item(name); track.duration_s = 2;
            cache.store_reader(&mut index, &track, true, 1, data.as_slice(), Some(data.len() as u64))
                .unwrap_or_else(|error| panic!("{name}: {error}"));
            let reopened = Cache::at(dir.0.clone());
            let path = reopened.local_path(&track).expect("complete recording survives restart");
            assert_eq!(fs::read(path).unwrap(), data);
        }
    }
    #[test]
    fn native_opus_decode_rejects_preview_and_obeys_cancellation() {
        let dir = Temp::new();
        let data = include_bytes!("../testdata/tone.opus");
        let path = dir.0.join("recording.audio");
        fs::write(&path, data).unwrap();
        assert!(validate_audio(&path, 180, || Ok(())).is_err());
        let checks = Cell::new(0);
        let result = validate_audio(&path, 2, || {
            checks.set(checks.get() + 1);
            if checks.get() >= 2 { Err("cancelled".into()) } else { Ok(()) }
        });
        assert_eq!(result.unwrap_err(), "cancelled");
        fs::write(&path, &data[..data.len() / 2]).unwrap();
        assert!(validate_audio(&path, 2, || Ok(())).is_err());
    }
    #[test]
    fn cancellation_during_read_cannot_publish_or_delete_an_existing_pin() {
        struct Cancelling<'a> { data: &'a [u8], cache: &'a Cache }
        impl Read for Cancelling<'_> {
            fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
                self.cache.cancel();
                self.data.read(out)
            }
        }
        let dir = Temp::new(); let cache = Cache::at(dir.0.clone());
        let mut index = cache.initial.lock().unwrap().take().unwrap();
        save(&cache, &mut index, "keep", true).unwrap();
        let data = wav(); let reader = Cancelling { data: data.as_slice(), cache: &cache };
        assert!(cache.store_reader(&mut index, &item("cancel-race"), true, 1, reader, Some(data.len() as u64)).is_err());
        assert_eq!(index.entries.len(), 1);
        assert_eq!(index.entries[0].item.id, "keep");
        let reopened = Cache::at(dir.0.clone());
        assert!(reopened.local_path(&item("keep")).is_some());
        assert!(reopened.local_path(&item("cancel-race")).is_none());
    }
    #[test]
    fn corrupt_index_cannot_bypass_quota_before_explicit_clear() {
        let dir = Temp::new(); fs::write(dir.0.join("index.json"), b"invalid").unwrap();
        fs::write(dir.0.join("orphan.audio"), b"preserve until confirmed clear").unwrap();
        let cache = Cache::at(dir.0.clone()); let mut index = cache.initial.lock().unwrap().take().unwrap();
        assert!(!index.writable); assert!(cache.set_quota(&mut index, DEFAULT_QUOTA).is_err());
        assert!(dir.0.join("orphan.audio").exists());
        cache.clear(&mut index).unwrap(); assert!(index.writable);
        assert!(!dir.0.join("orphan.audio").exists()); save(&cache, &mut index, "recovered", true).unwrap();
    }
    #[test]
    fn restart_discards_partial_and_orphan_files_and_accounts_are_isolated() {
        let dir = Temp::new(); fs::write(dir.0.join("orphan.audio"), b"x").unwrap(); fs::write(dir.0.join("unfinished.part"), b"x").unwrap();
        let cache = Cache::at(dir.0.clone()); assert!(cache.view.lock().unwrap().entries.is_empty()); assert!(!dir.0.join("unfinished.part").exists()); assert!(!dir.0.join("orphan.audio").exists());
        let mut first = ServerConfig::default(); first.user = "alice".into(); let mut second = first.clone(); second.user = "bob".into(); assert_ne!(account(&first), account(&second));
        second = first.clone(); second.server = "https://different.invalid".into(); assert_ne!(account(&first), account(&second));
    }
}
