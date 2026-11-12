//! Audio settings and bounded background album resolution. Playback stays in Player.
use std::cell::{Cell, RefCell};
use std::collections::VecDeque;
use std::rc::Rc;
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;
use slint::ComponentHandle;
use crate::feature_context::Context;
use crate::player::{AudioTrack, Normalization};

struct AlbumJob { token: u64, item: crate::queue::Item, track: AudioTrack }
struct AlbumResult { token: u64, track: AudioTrack, tracks: Vec<AudioTrack> }

pub(crate) struct State {
    ctx: Context,
    tools: slint::Weak<crate::ToolsWindow>,
    timer: slint::Timer,
    tick: Cell<u32>,
    token: Cell<Option<u64>>,
    album_requested: Cell<bool>,
    jobs: mpsc::SyncSender<AlbumJob>,
    results: RefCell<mpsc::Receiver<AlbumResult>>,
    settings: Arc<Mutex<Option<String>>>,
    save_signal: mpsc::SyncSender<()>,
    crossfade: Cell<f64>,
    overlap: Cell<bool>,
}

pub(crate) fn bind(ctx: &Context, tools: &crate::ToolsWindow) -> Rc<State> {
    let settings_pending = Arc::new(Mutex::new(None::<String>));
    let pending = settings_pending.clone();
    let (save_signal, saves) = mpsc::sync_channel::<()>(1);
    std::thread::spawn(move || {
        while saves.recv().is_ok() {
            let Some(value) = pending.lock().ok().and_then(|mut value| value.take()) else { continue; };
            let path = crate::paths::dir().join("audio-settings.json");
            let temporary = path.with_extension("json.tmp");
            if std::fs::write(&temporary, value).is_ok() { let _ = std::fs::rename(temporary, path); }
        }
    });
    let (jobs, input) = mpsc::sync_channel::<AlbumJob>(1);
    let (output, results) = mpsc::sync_channel::<AlbumResult>(2);
    let config = ctx.config.as_ref().clone();
    let hub = ctx.hub.hub.clone();
    std::thread::spawn(move || {
        let mut cache = VecDeque::<(String, Vec<AudioTrack>)>::new();
        while let Ok(mut job) = input.recv() {
            let cached = cache.iter().find(|(_, tracks)| tracks.iter().any(|t| t.key == job.track.key));
            let tracks = if let Some((album, tracks)) = cached {
                job.track.album = album.clone();
                // Session headers/stream URLs are deliberately kept only in memory.
                tracks.clone()
            } else {
                resolve_album(&config, &hub, &job.item, &mut job.track).unwrap_or_default()
            };
            if !tracks.is_empty() && !cache.iter().any(|(key, _)| key == &job.track.album) {
                if cache.len() == 16 { cache.pop_front(); }
                cache.push_back((job.track.album.clone(), tracks.clone()));
            }
            if output.send(AlbumResult { token: job.token, track: job.track, tracks }).is_err() { break; }
        }
    });
    let settings = std::fs::read(crate::paths::dir().join("audio-settings.json")).ok()
        .and_then(|data| serde_json::from_slice::<serde_json::Value>(&data).ok()).unwrap_or_default();
    let crossfade = settings["crossfade"].as_f64().filter(|v| v.is_finite()).unwrap_or(0.0).clamp(0.0, 12.0);
    let overlap = settings["overlap_album"].as_bool().unwrap_or(false);
    let mode = mode_from_index(settings["normalization"].as_i64().unwrap_or(0) as i32);
    tools.set_audio_crossfade(crossfade as f32);
    tools.set_audio_overlap_album(overlap);
    tools.set_audio_normalization(mode_index(mode));
    ctx.player.borrow_mut().set_crossfade(crossfade, overlap);
    ctx.player.borrow_mut().set_normalization(mode);
    let state = Rc::new(State {
        ctx: ctx.clone(), tools: tools.as_weak(), timer: slint::Timer::default(),
        tick: Cell::new(0), token: Cell::new(None), album_requested: Cell::new(false),
        jobs, results: RefCell::new(results), crossfade: Cell::new(crossfade), overlap: Cell::new(overlap),
        settings: settings_pending, save_signal,
    });
    let weak = Rc::downgrade(&state);
    tools.on_audio_set_crossfade(move |seconds| {
        let Some(state) = weak.upgrade() else { return; };
        let seconds = if seconds.is_finite() { f64::from(seconds).clamp(0.0, 12.0) } else { 0.0 };
        state.crossfade.set(seconds);
        state.ctx.player.borrow_mut().set_crossfade(seconds, state.overlap.get());
        if let Some(tools) = state.tools.upgrade() { tools.set_audio_crossfade(seconds as f32); }
        state.save();
    });
    let weak = Rc::downgrade(&state);
    tools.on_audio_set_overlap_album(move |enabled| {
        let Some(state) = weak.upgrade() else { return; };
        state.overlap.set(enabled);
        state.ctx.player.borrow_mut().set_crossfade(state.crossfade.get(), enabled);
        if let Some(tools) = state.tools.upgrade() { tools.set_audio_overlap_album(enabled); }
        state.save();
    });
    let weak = Rc::downgrade(&state);
    tools.on_audio_set_normalization(move |index| {
        let Some(state) = weak.upgrade() else { return; };
        state.ctx.player.borrow_mut().set_normalization(mode_from_index(index));
        state.album_requested.set(false);
        if let Some(tools) = state.tools.upgrade() { tools.set_audio_normalization(mode_index(mode_from_index(index))); }
        state.save();
    });
    let weak = Rc::downgrade(&state);
    state.timer.start(slint::TimerMode::Repeated, Duration::from_millis(20), move || {
        if let Some(state) = weak.upgrade() { state.poll(); }
    });
    state
}

impl State {
    fn save(&self) {
        let value = serde_json::json!({
            "crossfade": self.crossfade.get(), "overlap_album": self.overlap.get(),
            "normalization": mode_index(self.ctx.player.borrow().normalization()),
        });
        // Coalesce slider changes; no file I/O on the UI thread.
        if let Ok(mut pending) = self.settings.lock() { *pending = Some(value.to_string()); }
        let _ = self.save_signal.try_send(());
    }

    fn poll(&self) {
        self.ctx.player.borrow_mut().audio_tick();
        let tick = self.tick.get().wrapping_add(1);
        self.tick.set(tick);
        if tick % 10 != 0 { return; }
        let token = self.ctx.queue.borrow().current_token();
        if self.token.get() != token || self.ctx.player.borrow().needs_audio_context() {
            self.token.set(token);
            self.album_requested.set(false);
            if let (Some(item), Some((target, cookie))) = (self.ctx.queue.borrow().current().cloned(), self.ctx.current_target()) {
                self.ctx.player.borrow_mut().set_audio_context(AudioTrack {
                    key: media_key(&item), target, cookie, album: String::new(),
                }, Vec::new());
            }
        }
        while let Ok(result) = self.results.borrow_mut().try_recv() {
            if Some(result.token) == token {
                self.ctx.player.borrow_mut().set_audio_context(result.track, result.tracks);
            }
        }
        if self.ctx.player.borrow().normalization() == Normalization::Album && !self.album_requested.get() {
            if let (Some(token), Some(item), Some((target, cookie))) =
                (token, self.ctx.queue.borrow().current().cloned(), self.ctx.current_target()) {
                let track = AudioTrack { key: media_key(&item), target, cookie, album: String::new() };
                if self.jobs.try_send(AlbumJob { token, item, track }).is_ok() { self.album_requested.set(true); }
            }
        }
        let mut player = self.ctx.player.borrow_mut();
        player.poll_normalization();
        if let Some(tools) = self.tools.upgrade() {
            tools.set_audio_status(player.normalization_status().into());
        }
    }
}

fn mode_from_index(index: i32) -> Normalization {
    match index { 1 => Normalization::Track, 2 => Normalization::Album, _ => Normalization::Off }
}
fn mode_index(mode: Normalization) -> i32 {
    match mode { Normalization::Off => 0, Normalization::Track => 1, Normalization::Album => 2 }
}
fn media_key(item: &crate::queue::Item) -> String {
    if !item.id.is_empty() { format!("nd:{}", item.id) }
    else if !item.listen_ref.is_empty() { item.listen_ref.clone() }
    else { format!("file:{}", item.path) }
}

fn resolve_album(config: &crate::navidrome::ServerConfig, hub: &Arc<crate::hub::Hub>, item: &crate::queue::Item, track: &mut AudioTrack) -> Option<Vec<AudioTrack>> {
    if !item.id.is_empty() {
        let (id, album) = crate::navidrome::song_album(config, &item.id).ok()?;
        track.album = format!("nd:{id}");
        return Some(album.tracks.into_iter().map(|song| AudioTrack {
            key: format!("nd:{}", song.id), target: crate::navidrome::stream_url(config, &song.id),
            cookie: String::new(), album: track.album.clone(),
        }).collect());
    }
    if item.listen_ref.is_empty() { return None; } // Embedded album gain is read by Player.
    let query = format!("{} {}", item.artist, item.title);
    let encoded: String = query.bytes().map(|b| {
        if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) { (b as char).to_string() }
        else { format!("%{b:02X}") }
    }).collect();
    let search = hub.send("GET", &format!("/api/search?q={encoded}"), None).ok()?;
    let row = search["tracks"].as_array()?.iter().find(|row| row["ref"].as_str() == Some(&item.listen_ref))?;
    let album_ref = row["album_ref"].as_str()?;
    // Only canonical catalog references enter path segments; never resolved URLs.
    if !album_ref.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_:".contains(&b)) { return None; }
    let album = hub.send("GET", &format!("/api/catalog/album/{album_ref}"), None).ok()?;
    let rows = album["tracks"].as_array()?;
    let count = album["nb_tracks"].as_u64()? as usize;
    if count == 0 || rows.len() != count { return None; }
    track.album = album_ref.to_owned();
    let cookie = hub.session_token();
    let tracks: Option<Vec<_>> = rows.iter().map(|row| {
        let reference = row["ref"].as_str()?;
        Some(AudioTrack { key: reference.into(), target: hub.listen_url(reference, true), cookie: cookie.clone(), album: track.album.clone() })
    }).collect();
    tracks.filter(|tracks| tracks.iter().any(|member| member.key == track.key))
}
