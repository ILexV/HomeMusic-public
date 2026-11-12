//! Connection changes are authenticated before commit and applied only by a clean restart.
//! Windows stores secrets in Credential Manager; Unix config files are private, not encrypted.
use std::cell::Cell;
use std::fs::{self, OpenOptions};
#[cfg(unix)]
use std::fs::File;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use md5::{Digest, Md5};
use slint::{ComponentHandle, Timer, TimerMode};
use url::Url;

use crate::feature_context::Context;
use crate::navidrome::ServerConfig;
use crate::ToolsWindow;

pub(crate) struct State {
    ctx: Context,
    tools: slint::Weak<ToolsWindow>,
    revision: Cell<u64>,
    busy: Cell<bool>,
    tx: SyncSender<Completed>,
    rx: Receiver<Completed>,
    timer: Timer,
}

struct Completed {
    revision: u64,
    submitted: ServerConfig,
    save: bool,
    result: Result<ServerConfig, String>,
}

fn form(ui: &ToolsWindow) -> ServerConfig {
    ServerConfig {
        server: ui.get_connection_server().to_string(),
        hub: ui.get_connection_hub().to_string(),
        user: ui.get_connection_user().to_string(),
        password: ui.get_connection_password().to_string(),
        loaded: true,
    }
}

fn endpoint(value: &str, label: &str) -> Result<Url, String> {
    let value = value.trim();
    let bad = || format!("{label}: нужен адрес http:// или https:// без логина, пароля, параметров и #");
    // Reject ambiguous URL spellings before URL normalization can silently repair them.
    if value.chars().any(|c| c.is_control() || c.is_whitespace()) || value.contains('\\') {
        return Err(bad());
    }
    let url = Url::parse(value).map_err(|_| bad())?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none()
        || !url.username().is_empty() || url.password().is_some()
        || url.query().is_some() || url.fragment().is_some()
        || value.split("://").nth(1).is_none_or(|rest| rest.split('/').next().unwrap_or("").contains('@'))
    {
        return Err(bad());
    }
    Ok(url)
}

fn candidate(mut config: ServerConfig) -> Result<ServerConfig, String> {
    let server = endpoint(&config.server, "Navidrome")?;
    let hub = if config.hub.trim().is_empty() {
        let mut hub = server.clone();
        hub.set_port(Some(8080)).map_err(|_| "Hub: укажите адрес сервера".to_string())?;
        hub.set_path("");
        hub
    } else {
        endpoint(&config.hub, "Hub")?
    };
    config.server = server.as_str().trim_end_matches('/').to_string();
    config.hub = hub.as_str().trim_end_matches('/').to_string();
    config.user = config.user.trim().to_string();
    if config.user.is_empty() || config.user.chars().any(char::is_control) {
        return Err("Введите имя пользователя Navidrome".into());
    }
    if config.password.is_empty() {
        return Err("Введите пароль учётной записи".into());
    }
    #[cfg(windows)]
    if config.password.len() > windows_sys::Win32::Security::Credentials::CRED_MAX_CREDENTIAL_BLOB_SIZE as usize {
        return Err("Пароль слишком длинный для Диспетчера учётных данных Windows".into());
    }
    config.loaded = true;
    Ok(config)
}

fn authenticate(mut config: ServerConfig) -> Result<ServerConfig, String> {
    let hub = crate::hub::Hub::from_config(&config);
    // Never display server-provided error text: it may echo credentials or request URLs.
    let me = hub.send("GET", "/api/auth/me", None)
        .map_err(|_| "Hub: вход не выполнен. Проверьте адрес, логин, пароль и доступность сервера.".to_string())?;
    let username = me.get("username").and_then(serde_json::Value::as_str)
        .filter(|name| !name.is_empty() && !name.chars().any(char::is_control))
        .ok_or_else(|| "Hub: сервер не подтвердил учётную запись".to_string())?;
    // Hub accepts an email alias; Navidrome and account-scoped data need the actual username.
    config.user = username.to_string();
    crate::navidrome::check_connection(&config)?;
    Ok(config)
}

fn request_current(revision: u64, current_revision: u64, submitted: &ServerConfig, current: &ServerConfig) -> bool {
    revision == current_revision && submitted == current
}

impl State {
    fn changed(&self) {
        self.revision.set(self.revision.get().wrapping_add(1));
        if let Some(ui) = self.tools.upgrade() {
            self.transport_warning(&ui);
            ui.set_connection_status(if self.busy.get() {
                "Поля изменены. Результат текущей проверки будет отброшен; затем проверьте вход снова."
            } else {
                "Изменения ещё не сохранены. Проверьте вход или сохраните с переподключением."
            }.into());
        }
    }

    fn transport_warning(&self, ui: &ToolsWindow) {
        let server = ui.get_connection_server().trim().to_ascii_lowercase();
        let hub = ui.get_connection_hub().trim().to_ascii_lowercase();
        ui.set_connection_insecure(server.starts_with("http://") || hub.starts_with("http://"));
    }

    fn request(&self, save: bool) {
        if self.busy.get() { return; }
        let Some(ui) = self.tools.upgrade() else { return; };
        let submitted = form(&ui);
        let config = match candidate(submitted.clone()) {
            Ok(config) => config,
            Err(error) => { ui.set_connection_status(error.into()); return; }
        };
        self.busy.set(true);
        ui.set_connection_busy(true);
        ui.set_connection_status("Проверяем вход в Hub и Navidrome…".into());
        let revision = self.revision.get();
        let tx = self.tx.clone();
        let spawned = std::thread::Builder::new().name("connection-check".into()).spawn(move || {
            let result = authenticate(config);
            let _ = tx.send(Completed { revision, submitted, save, result });
        });
        if spawned.is_err() {
            self.busy.set(false);
            ui.set_connection_busy(false);
            ui.set_connection_status("Не удалось начать проверку подключения".into());
        }
    }

    fn apply(&self, event: Completed) {
        self.busy.set(false);
        let Some(ui) = self.tools.upgrade() else { return; };
        ui.set_connection_busy(false);
        if !request_current(event.revision, self.revision.get(), &event.submitted, &form(&ui)) {
            ui.set_connection_status("Поля изменены: прежний результат отброшен. Проверьте вход снова.".into());
            return;
        }
        let config = match event.result {
            Ok(config) => config,
            Err(error) => { ui.set_connection_status(error.into()); return; }
        };
        if !event.save {
            ui.set_connection_status("Вход в Hub и Navidrome подтверждён. Настройки ещё не сохранены.".into());
            return;
        }
        if let Err(error) = save_config(&crate::paths::dir().join("config.toml"), &config) {
            ui.set_connection_status(error.into());
            return;
        }
        ui.set_connection_server(config.server.into());
        ui.set_connection_hub(config.hub.into());
        ui.set_connection_user(config.user.into());
        ui.set_connection_status("Настройки сохранены. Перезапускаем плеер…".into());
        // No in-process mutation of old account handles, workers, queues or caches.
        if self.ctx.restart().is_err() {
            ui.set_connection_status("Настройки сохранены, но перезапуск не удался. Закройте и откройте приложение; до этого работает прежнее подключение.".into());
        }
    }
}

pub(crate) fn bind(ctx: &Context, tools: &ToolsWindow) -> Rc<State> {
    let (tx, rx) = mpsc::sync_channel(1);
    let state = Rc::new(State { ctx: ctx.clone(), tools: tools.as_weak(), revision: Cell::new(0),
        busy: Cell::new(false), tx, rx, timer: Timer::default() });
    tools.set_connection_server(ctx.config.server.clone().into());
    tools.set_connection_hub(ctx.config.hub.clone().into());
    tools.set_connection_user(ctx.config.user.clone().into());
    tools.set_connection_password(ctx.config.password.clone().into());
    tools.set_connection_status(if ctx.config.user.is_empty() || ctx.config.password.is_empty() {
        "Укажите адреса серверов и данные учётной записи. Если сохранённый пароль недоступен, введите его снова."
    } else {
        "Сохранённые настройки загружены. Проверка входа выполняется по кнопке."
    }.into());
    tools.set_connection_storage(if cfg!(windows) {
        "Пароль сохраняется в Диспетчере учётных данных Windows для текущего пользователя, а не в config.toml."
    } else {
        "Пароль хранится в config.toml с правами 0600 (только владелец). Это не шифрование; защищайте свою учётную запись ОС."
    }.into());
    state.transport_warning(tools);
    let weak = Rc::downgrade(&state);
    tools.on_connection_changed(move || { if let Some(state) = weak.upgrade() { state.changed(); } });
    let weak = Rc::downgrade(&state);
    tools.on_connection_test(move || { if let Some(state) = weak.upgrade() { state.request(false); } });
    let weak = Rc::downgrade(&state);
    tools.on_connection_save(move || { if let Some(state) = weak.upgrade() { state.request(true); } });
    let weak = Rc::downgrade(&state);
    state.timer.start(TimerMode::Repeated, Duration::from_millis(100), move || {
        if let Some(state) = weak.upgrade() {
            while let Ok(event) = state.rx.try_recv() { state.apply(event); }
        }
    });
    state
}

// Config loading is shared with ServerConfig::load; the legacy parser still accepts old files.
pub(crate) fn read_config(path: &Path) -> io::Result<String> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options.open(path)?;
    if !file.metadata()?.is_file() { return Err(io::Error::from(io::ErrorKind::InvalidInput)); }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
    }
    let mut text = String::new();
    file.take(64 * 1024).read_to_string(&mut text)?;
    Ok(text)
}

pub(crate) fn restore_password(text: &str, config: &mut ServerConfig) {
    if let Some(target) = stored_target(text, config) {
        // A reference takes precedence over a legacy field: never fall back to an old password.
        config.password = secret::read(&target).unwrap_or_default();
    } else if text.parse::<toml::Table>().ok().is_some_and(|table| table.contains_key("credential")) {
        config.password.clear();
    }
}

fn credential_prefix(config: &ServerConfig) -> String {
    let mut hash = Md5::new();
    for part in [&config.server, &config.hub, &config.user] {
        hash.update((part.len() as u64).to_le_bytes());
        hash.update(part.as_bytes());
    }
    format!("HomeMusic/{:x}/", hash.finalize())
}

fn stored_target(text: &str, config: &ServerConfig) -> Option<String> {
    let table = text.parse::<toml::Table>().ok()?;
    let target = table.get("credential")?.as_str()?;
    let prefix = credential_prefix(config);
    let suffix = target.strip_prefix(&prefix)?;
    if suffix.is_empty() || !suffix.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-') { return None; }
    Some(target.to_string())
}

fn serialize_config(config: &ServerConfig, credential: Option<&str>) -> String {
    let mut table = toml::Table::new();
    for (key, value) in [("server", &config.server), ("hub", &config.hub), ("user", &config.user)] {
        table.insert(key.into(), toml::Value::String(value.clone()));
    }
    if let Some(target) = credential {
        table.insert("credential".into(), toml::Value::String(target.into()));
    } else {
        table.insert("password".into(), toml::Value::String(config.password.clone()));
    }
    table.to_string()
}

fn unique_suffix() -> String {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    format!("{}-{}-{}", std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos(),
        NEXT.fetch_add(1, Ordering::Relaxed))
}

struct PendingFile(PathBuf);
impl Drop for PendingFile {
    fn drop(&mut self) { let _ = fs::remove_file(&self.0); }
}

fn write_config(path: &Path, text: &str) -> io::Result<()> {
    let parent = path.parent().ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
    let temporary = parent.join(format!(".homemusic-config-{}.tmp", unique_suffix()));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temporary)?;
    let pending = PendingFile(temporary);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
    }
    file.write_all(text.as_bytes())?;
    file.sync_all()?;
    drop(file);
    atomic_replace(&pending.0, path)?;
    // Rename is the commit point. A directory fsync failure must not report a rollback.
    #[cfg(unix)]
    if let Ok(directory) = File::open(parent) { let _ = directory.sync_all(); }
    Ok(())
}

#[cfg(not(windows))]
fn atomic_replace(from: &Path, to: &Path) -> io::Result<()> { fs::rename(from, to) }

#[cfg(windows)]
fn atomic_replace(from: &Path, to: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH};
    let from: Vec<u16> = from.as_os_str().encode_wide().chain(Some(0)).collect();
    let to: Vec<u16> = to.as_os_str().encode_wide().chain(Some(0)).collect();
    // Both buffers are NUL-terminated and live for the duration of the call.
    if unsafe { MoveFileExW(from.as_ptr(), to.as_ptr(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) } == 0 {
        Err(io::Error::last_os_error())
    } else { Ok(()) }
}

fn save_config(path: &Path, config: &ServerConfig) -> Result<(), String> {
    #[cfg(windows)]
    {
        let previous = read_config(path).ok().and_then(|text| {
            let old = crate::navidrome::parse_config(&text);
            stored_target(&text, &old)
        });
        // A fresh target makes the two-resource transaction non-destructive on failure.
        let target = format!("{}{}", credential_prefix(config), unique_suffix());
        secret::write(&target, &config.user, &config.password)?;
        if write_config(path, &serialize_config(config, Some(&target))).is_err() {
            secret::delete(&target);
            return Err("Не удалось сохранить config.toml. Прежние настройки не изменены.".into());
        }
        if let Some(previous) = previous { secret::delete(&previous); }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        write_config(path, &serialize_config(config, None))
            .map_err(|_| "Не удалось сохранить config.toml с правами 0600. Прежние настройки не изменены.".into())
    }
}

#[cfg(not(windows))]
mod secret {
    pub(super) fn read(_: &str) -> Option<String> { None }
}

#[cfg(windows)]
mod secret {
    use windows_sys::Win32::Security::Credentials::{
        CredDeleteW, CredFree, CredReadW, CredWriteW, CREDENTIALW,
        CRED_MAX_CREDENTIAL_BLOB_SIZE, CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC,
    };

    pub(super) fn read(target: &str) -> Option<String> {
        let target: Vec<u16> = target.encode_utf16().chain(Some(0)).collect();
        let mut credential = std::ptr::null_mut();
        // CredReadW allocates the credential and blob; CredFree releases both after copying.
        if unsafe { CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut credential) } == 0 { return None; }
        if credential.is_null() { return None; }
        let result = unsafe {
            let value = &*credential;
            let size = value.CredentialBlobSize as usize;
            if size == 0 || size > CRED_MAX_CREDENTIAL_BLOB_SIZE as usize || value.CredentialBlob.is_null() {
                None
            } else {
                std::str::from_utf8(std::slice::from_raw_parts(value.CredentialBlob, size)).ok().map(str::to_owned)
            }
        };
        unsafe { CredFree(credential.cast()); }
        result
    }

    pub(super) fn write(target: &str, user: &str, password: &str) -> Result<(), String> {
        let mut target: Vec<u16> = target.encode_utf16().chain(Some(0)).collect();
        let mut user: Vec<u16> = user.encode_utf16().chain(Some(0)).collect();
        let credential = CREDENTIALW {
            Type: CRED_TYPE_GENERIC,
            TargetName: target.as_mut_ptr(),
            CredentialBlobSize: password.len() as u32,
            // CredWriteW reads this blob; its legacy API type is mutable but it does not mutate it.
            CredentialBlob: password.as_ptr().cast_mut(),
            Persist: CRED_PERSIST_LOCAL_MACHINE,
            UserName: user.as_mut_ptr(),
            ..CREDENTIALW::default()
        };
        // The API copies all pointers synchronously; no buffers escape this call.
        let written = unsafe { CredWriteW(&credential, 0) };
        if written == 0 {
            Err("Windows не сохранила пароль в Диспетчере учётных данных. Прежние настройки не изменены.".into())
        } else { Ok(()) }
    }

    pub(super) fn delete(target: &str) {
        let target: Vec<u16> = target.encode_utf16().chain(Some(0)).collect();
        unsafe { CredDeleteW(target.as_ptr(), CRED_TYPE_GENERIC, 0); }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> ServerConfig {
        ServerConfig { server: "https://music.example.test".into(), hub: "https://hub.example.test".into(),
            user: "Иван".into(), password: "a\\b\"c#d\nсекрет".into(), loaded: true }
    }

    struct Directory(PathBuf);
    impl Directory {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("homemusic-connection-test-{}", unique_suffix()));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Directory { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); } }

    #[test]
    fn urls_reject_secrets_without_echoing_them() {
        for url in ["https://user:secret@host", "https://host?token=secret", "https://host/#secret",
            "ftp://secret", "https://@host", "https://host\\secret", "https://ho\nst/secret"] {
            let error = endpoint(url, "Hub").unwrap_err();
            assert!(!error.contains("secret"));
        }
        assert!(endpoint("http://[::1]:8080/base/", "Hub").is_ok());
    }

    #[test]
    fn default_hub_supports_ipv6_and_preserves_explicit_https() {
        let mut input = config();
        input.server = "https://[::1]:4533/music/".into();
        input.hub.clear();
        let result = candidate(input).unwrap();
        assert_eq!(result.hub, "https://[::1]:8080");
        assert_eq!(result.server, "https://[::1]:4533/music");
    }

    #[test]
    fn secret_round_trip_handles_toml_metacharacters() {
        let original = config();
        let text = serialize_config(&original, None);
        assert_eq!(crate::navidrome::parse_config(&text), original);
        assert!(!format!("{original:?}").contains(&original.password));
    }

    #[test]
    fn credential_reference_has_no_plaintext_and_cannot_cross_accounts() {
        let original = config();
        let target = format!("{}test", credential_prefix(&original));
        let text = serialize_config(&original, Some(&target));
        let table = text.parse::<toml::Table>().unwrap();
        assert!(!table.contains_key("password"));
        assert!(!text.contains(&original.password));
        assert_eq!(stored_target(&text, &original), Some(target));
        for field in [0, 1, 2] {
            let mut other = original.clone();
            match field { 0 => other.server.push('x'), 1 => other.hub.push('x'), _ => other.user.push('x') }
            assert_eq!(stored_target(&text, &other), None);
        }
    }

    #[test]
    fn stale_checks_cannot_commit_newer_form() {
        let first = config();
        let mut edited = first.clone();
        edited.user = "другая учётная запись".into();
        assert!(!request_current(1, 2, &first, &edited));
        assert!(!request_current(1, 1, &first, &edited));
        assert!(!request_current(1, 3, &first, &first)); // Edited away, then back.
        assert!(request_current(3, 3, &first, &first));
    }

    #[test]
    fn legacy_unescaped_password_stays_readable_until_migration() {
        let legacy = "user = \"ivan\"\npassword = \"C:\\old\\secret\"\n";
        let parsed = crate::navidrome::parse_config(legacy);
        assert_eq!(parsed.user, "ivan");
        assert_eq!(parsed.password, "C:\\old\\secret");
        assert_eq!(crate::navidrome::parse_config(&serialize_config(&parsed, None)), parsed);
    }

    #[cfg(windows)]
    #[test]
    fn windows_migration_restart_load_and_failed_save_preserve_working_secret() {
        use std::os::windows::fs::OpenOptionsExt;
        struct Credentials(Vec<String>);
        impl Drop for Credentials {
            fn drop(&mut self) { for target in &self.0 { secret::delete(target); } }
        }
        let dir = Directory::new();
        let path = dir.0.join("config.toml");
        let original = config();
        fs::write(&path, serialize_config(&original, None)).unwrap();
        save_config(&path, &original).unwrap();
        let text = read_config(&path).unwrap();
        let target = stored_target(&text, &original).unwrap();
        let mut cleanup = Credentials(vec![target.clone()]);
        assert!(!text.parse::<toml::Table>().unwrap().contains_key("password"));
        let mut loaded = crate::navidrome::parse_config(&text);
        assert!(loaded.password.is_empty());
        restore_password(&text, &mut loaded);
        assert_eq!(loaded, original);

        let mut other = original.clone();
        other.user = "another-account".into();
        other.password = "replacement-test-password".into();
        // Windows denies replacement while a handle excludes FILE_SHARE_DELETE.
        let lock = OpenOptions::new().read(true).share_mode(0).open(&path).unwrap();
        assert!(save_config(&path, &other).is_err());
        drop(lock);
        assert_eq!(read_config(&path).unwrap(), text);
        assert_eq!(secret::read(&target).as_deref(), Some(original.password.as_str()));

        save_config(&path, &other).unwrap();
        let text = read_config(&path).unwrap();
        cleanup.0.push(stored_target(&text, &other).unwrap());
        assert!(secret::read(&target).is_none());
        let mut loaded = crate::navidrome::parse_config(&text);
        restore_password(&text, &mut loaded);
        assert_eq!(loaded, other);
    }

    #[test]
    fn missing_source_does_not_destroy_previous_config() {
        let dir = Directory::new();
        let path = dir.0.join("config.toml");
        fs::write(&path, "previous working config").unwrap();
        assert!(atomic_replace(&dir.0.join("absent.tmp"), &path).is_err());
        assert_eq!(fs::read_to_string(path).unwrap(), "previous working config");
    }

    #[cfg(unix)]
    #[test]
    fn private_atomic_save_and_legacy_load_permissions() {
        use std::os::unix::fs::PermissionsExt;
        let dir = Directory::new();
        let path = dir.0.join("config.toml");
        fs::write(&path, "user = \"legacy\"\npassword = \"old\"\n").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(read_config(&path).unwrap().contains("legacy"));
        assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        save_config(&path, &config()).unwrap();
        assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert_eq!(crate::navidrome::parse_config(&read_config(&path).unwrap()), config());
        assert_eq!(fs::read_dir(&dir.0).unwrap().count(), 1);
    }
}
