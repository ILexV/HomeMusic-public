//! Playback through libmpv, with native gapless prefetch and a second crossfade deck.

use std::ffi::c_char;

use libmpv2::Mpv;

use crate::eq::{self, Settings};

#[path = "audio_analysis.rs"]
pub(crate) mod analysis;
pub use analysis::{AudioTrack, Normalization};

#[path = "spectrum.rs"]
mod spectrum;
use spectrum::SpectrumTap;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) enum MeterMode { #[default] Off, Stereo, Spectrum }

// Use mpv's direct lavfi bridge so it rejects a missing astats even while idle.
// reset=1 measures the latest decoded frame, not the entire track.
const METER_FILTER: &str =
    "@meter:lavfi-astats=metadata=1:reset=1:measure_perchannel=Peak_level:measure_overall=none";

#[derive(Default)]
pub struct StreamDetails {
    pub codec: String,
    pub bitrate_kbps: Option<u32>,
    pub sample_rate: Option<u32>,
    pub channels: Option<String>,
}

struct Prepared {
    key: String,
    deck: Option<Box<Player>>,
    same_album: bool,
    hub_source: bool,
}

struct Fade {
    outgoing: Box<Player>,
    seconds: f64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PlaybackPhase {
    Idle,
    Loading,
    Buffering,
    Playing,
    Paused,
    Stopped,
    Ended,
    Error,
}

#[derive(Clone, Debug)]
pub struct PlaybackState {
    pub phase: PlaybackPhase,
    pub resolving_source: bool,
    pub seekable: bool,
    /// Active, unpaused playback intent, including loading and cache waits.
    pub playing: bool,
    pub position_s: f32,
    pub duration_s: f32,
    pub volume: f32,
}

pub struct PlayerOptions {
    /// Tests set this so the playhead moves without a sound card.
    /// The window leaves it false and uses the system output.
    pub null_audio: bool,
}

pub struct Player {
    mpv: Option<Mpv>,
    volume: f32,
    eq: Settings,
    eq_filter_active: bool,
    meter_mode: MeterMode,
    spectrum_tap: Option<Box<SpectrumTap>>,
    spectrum_pending: bool,
    notice: Option<String>,
    phase: PlaybackPhase,
    /// Ignore the replaced file's trailing events until the next StartFile.
    awaiting_start: bool,
    /// Expected while waiting for StartFile, then the accepted foreground entry.
    entry_id: Option<i64>,
    hub_source: bool,
    /// Set by stop and by a natural end that was already handled.
    /// A following end-of-file event must not advance the queue.
    user_stopped: bool,
    /// One foreground media failure with unpaused playback intent.
    skip_failed_media: bool,
    null_audio: bool,
    prepared: Option<Prepared>,
    promoted: Option<String>,
    native_ending: bool,
    fade: Option<Fade>,
    fade_factor: f64,
    crossfade_s: f64,
    overlap_album: bool,
    normalization: Normalization,
    gain_db: f64,
    audio_track: Option<AudioTrack>,
    album_tracks: Vec<AudioTrack>,
    analyzer: Option<analysis::Analyzer>,
    normalization_status: String,
}

fn volume_setting(user_volume: f32, gain_db: f64, fade: f64) -> f64 {
    // mpv applies (volume / 100)^3 to PCM. Preserve the user's existing curve,
    // but convert our physical dB gain and linear-amplitude fade into that curve.
    f64::from(user_volume) * 100.0 * 10f64.powf(gain_db / 60.0) * fade.cbrt()
}

fn open_mpv(null_audio: bool) -> Result<Mpv, libmpv2::Error> {
    Mpv::with_initializer(|init| {
        init.set_option("idle", "yes")?;
        init.set_option("vid", "no")?;
        init.set_option("audio-display", "no")?;
        init.set_option("ytdl", "no")?;
        init.set_option("msg-level", "all=no")?;
        init.set_option("gapless-audio", "yes")?;
        init.set_option("prefetch-playlist", "yes")?;
        init.set_option("network-timeout", "15")?;
        init.set_option("volume-max", "1000")?;
        if null_audio { init.set_option("ao", "null")?; }
        Ok(())
    })
}

fn append_file(mpv: &Mpv, target: &str, cookie: &str) -> Result<(), libmpv2::Error> {
    let header = cookie_header(cookie).unwrap_or_default();
    let options = format!("http-header-fields=%{}%{},start=0", header.len(), header);
    let version = mpv.get_property::<String>("mpv-version").unwrap_or_default();
    let minor = version.trim_start_matches("mpv ").split('.').nth(1)
        .and_then(|s| s.parse::<u32>().ok()).unwrap_or(38);
    if minor >= 38 {
        mpv.command("loadfile", &[target, "append", "-1", &options])
    } else {
        mpv.command("loadfile", &[target, "append", &options])
    }
}

enum PlaybackEvent {
    StartFile(i64),
    EndFile(libmpv2::EndFileReason, libmpv2::MpvError, i64),
    FileLoaded,
    Shutdown,
    QueueOverflow,
}

/// libmpv2 drops the StartFile entry ID and conflates EndFile.error with
/// unrelated command/property errors. Keep the native boundary identity.
fn playback_event(mpv: &Mpv) -> Option<PlaybackEvent> {
    use libmpv2::events::mpv_event_id as id;
    loop {
        // SAFETY: this is the only reader of this client during the drain. All
        // event data is copied before the next wait, using its documented type.
        unsafe {
            let event = &*libmpv2_sys::mpv_wait_event(mpv.ctx.as_ptr(), 0.0);
            let boundary = match event.event_id {
                id::None => return None,
                id::StartFile => {
                    let start = &*event.data.cast::<libmpv2_sys::mpv_event_start_file>();
                    PlaybackEvent::StartFile(start.playlist_entry_id)
                }
                id::EndFile => {
                    let end = &*event.data.cast::<libmpv2_sys::mpv_event_end_file>();
                    PlaybackEvent::EndFile(end.reason, end.error, end.playlist_entry_id)
                }
                id::FileLoaded => PlaybackEvent::FileLoaded,
                id::Shutdown => PlaybackEvent::Shutdown,
                id::QueueOverflow => PlaybackEvent::QueueOverflow,
                _ => continue,
            };
            return Some(boundary);
        }
    }
}

impl Player {
    pub fn open(options: PlayerOptions) -> Self {
        ensure_c_numeric();
        let opened = open_mpv(options.null_audio);
        match opened {
            Ok(mpv) => {
                let player = Self {
                    mpv: Some(mpv),
                    volume: 0.8,
                    eq: Settings::default(),
                    eq_filter_active: false, meter_mode: MeterMode::Off, spectrum_tap: None, spectrum_pending: false,
                    notice: None,
                    phase: PlaybackPhase::Idle,
                    awaiting_start: false,
                    entry_id: None,
                    hub_source: false,
                    user_stopped: false,
                    skip_failed_media: false,
                    null_audio: options.null_audio,
                    prepared: None, promoted: None, native_ending: false, fade: None,
                    fade_factor: 1.0, crossfade_s: 0.0, overlap_album: false,
                    normalization: Normalization::Off, gain_db: 0.0,
                    audio_track: None, album_tracks: Vec::new(), analyzer: None,
                    normalization_status: "Нормализация выключена".into(),
                };
                player.apply_volume();
                player
            }
            Err(err) => Self {
                mpv: None,
                volume: 0.8,
                eq: Settings::default(),
                eq_filter_active: false, meter_mode: MeterMode::Off, spectrum_tap: None, spectrum_pending: false,
                notice: Some(open_error(&err)),
                phase: PlaybackPhase::Error,
                awaiting_start: false,
                entry_id: None,
                hub_source: false,
                user_stopped: false,
                skip_failed_media: false,
                null_audio: options.null_audio,
                prepared: None, promoted: None, native_ending: false, fade: None,
                fade_factor: 1.0, crossfade_s: 0.0, overlap_album: false,
                normalization: Normalization::Off, gain_db: 0.0,
                audio_track: None, album_tracks: Vec::new(), analyzer: None,
                normalization_status: "Нормализация выключена".into(),
            },
        }
    }

    pub fn stream_details(&self) -> StreamDetails {
        let Some(mpv) = self.mpv.as_ref().filter(|_| !self.is_idle() && self.phase != PlaybackPhase::Loading) else { return StreamDetails::default(); };
        StreamDetails {
            codec: mpv.get_property("audio-codec-name").unwrap_or_default(),
            bitrate_kbps: prop_f64(mpv, "audio-bitrate").map(|v| (v / 1000.0).round() as u32),
            sample_rate: prop_f64(mpv, "audio-params/samplerate").map(|v| v as u32),
            channels: mpv.get_property::<String>("audio-params/hr-channels").ok(),
        }
    }

    pub fn set_crossfade(&mut self, seconds: f64, overlap_album: bool) {
        let seconds = if seconds.is_finite() { seconds.clamp(0.0, 12.0) } else { 0.0 };
        if self.crossfade_s != seconds || self.overlap_album != overlap_album {
            self.cancel_preload();
            self.finish_fade();
            self.crossfade_s = seconds;
            self.overlap_album = overlap_album;
        }
    }

    /// Key identifies the predicted queue occurrence. Invalidate on queue changes.
    pub fn prepare_next(&mut self, target: &str, cookie: &str, key: &str, same_album: bool) -> Result<(), String> {
        if self.prepared.as_ref().is_some_and(|p| p.key == key) { return Ok(()); }
        self.cancel_preload();
        if self.is_idle() || self.promoted.is_some() { return Ok(()); }
        let deck = if self.crossfade_s > 0.0 && (!same_album || self.overlap_album) {
            let mut next = Box::new(Self::open(PlayerOptions { null_audio: self.null_audio }));
            next.volume = self.volume;
            next.restore_eq(self.eq);
            next.set_meter_mode(self.meter_mode);
            next.load_cookie_at(target, cookie, 0.0, true)?;
            if same_album && self.normalization == Normalization::Album {
                next.gain_db = self.gain_db;
                next.apply_volume();
            }
            Some(next)
        } else {
            let mpv = self.mpv.as_ref().ok_or("Звук недоступен")?;
            append_file(mpv, target, cookie).map_err(|_| "Не удалось подготовить следующий трек")?;
            None
        };
        self.prepared = Some(Prepared { key: key.to_owned(), deck, same_album, hub_source: !cookie.is_empty() });
        Ok(())
    }

    pub fn cancel_preload(&mut self) {
        self.prepared = None;
        self.native_ending = false;
        if let Some(mpv) = &self.mpv { let _ = mpv.command("playlist-clear", &[]); }
    }

    /// After take_natural_end, advance the queue but never reload promoted audio.
    pub fn take_promoted(&mut self) -> Option<String> { self.promoted.take() }

    pub fn take_failed_media(&mut self) -> bool {
        std::mem::take(&mut self.skip_failed_media)
    }

    pub fn set_audio_context(&mut self, track: AudioTrack, complete_album: Vec<AudioTrack>) {
        self.audio_track = Some(track);
        self.album_tracks = complete_album;
        self.update_normalization();
    }

    pub fn set_normalization(&mut self, mode: Normalization) {
        if self.normalization != mode {
            if let Some(analyzer) = &mut self.analyzer { analyzer.cancel(); }
            self.normalization = mode;
            self.gain_db = 0.0;
            self.apply_volume();
            self.update_normalization();
        }
    }

    pub fn normalization(&self) -> Normalization { self.normalization }
    pub fn normalization_status(&self) -> &str { &self.normalization_status }
    pub fn needs_audio_context(&self) -> bool {
        self.audio_track.is_none() && self.promoted.is_none() && !self.is_idle()
    }

    fn update_normalization(&mut self) {
        if self.normalization == Normalization::Off {
            self.normalization_status = "Нормализация выключена".into();
            return;
        }
        let Some(track) = &self.audio_track else {
            self.normalization_status = "Ожидание данных трека…".into();
            return;
        };
        let (key, tracks) = match self.normalization {
            Normalization::Track => (format!("track:{}", track.key), std::slice::from_ref(track)),
            Normalization::Album if !track.album.is_empty() && !self.album_tracks.is_empty() => {
                (format!("album:{}", track.album), self.album_tracks.as_slice())
            }
            _ => {
                let tagged = self.mpv.as_ref().and_then(|m| m.get_property::<String>("metadata/by-key/REPLAYGAIN_ALBUM_GAIN").ok())
                    .and_then(|s| s.split_whitespace().next()?.parse::<f64>().ok())
                    .filter(|v| v.is_finite());
                if let Some(gain) = tagged {
                    self.gain_db = gain.clamp(-24.0, 12.0);
                    self.apply_volume();
                    self.normalization_status = format!("Альбом · теги · {:+.1} дБ", self.gain_db);
                } else {
                    self.normalization_status = "Альбом: ожидается полный состав / нет данных альбома".into();
                }
                return;
            }
        };
        let analyzer = self.analyzer.get_or_insert_with(analysis::Analyzer::new);
        match analyzer.gain(&key, tracks) {
            None => self.normalization_status = "Анализ громкости полного аудио…".into(),
            Some(None) => self.normalization_status = "Анализ недоступен · усиление не изменено".into(),
            Some(Some(gain)) => {
                if (self.gain_db - gain).abs() > 0.001 {
                    self.gain_db = gain;
                    self.apply_volume();
                }
                let mode = if self.normalization == Normalization::Album { "Альбом · общий коэффициент" } else { "Трек" };
                self.normalization_status = format!("{mode} · {gain:+.1} дБ · −18 LUFS");
            }
        }
    }

    /// 20 ms envelope tick. Never consumes foreground EOF/promotion notifications.
    pub fn audio_tick(&mut self) {
        if self.spectrum_pending { self.attach_spectrum(); }
        if let Some(prepared) = &mut self.prepared {
            if let Some(deck) = &mut prepared.deck { deck.take_natural_end(); }
        }
        if let Some(fade) = &mut self.fade {
            if fade.outgoing.spectrum_pending { fade.outgoing.attach_spectrum(); }
            let position = self.mpv.as_ref().and_then(|m| prop_f64(m, "time-pos")).unwrap_or(0.0);
            let progress = (position / fade.seconds).clamp(0.0, 1.0);
            self.fade_factor = progress;
            fade.outgoing.fade_factor = 1.0 - progress;
            fade.outgoing.volume = self.volume;
            self.apply_volume();
            if progress >= 1.0 || self.is_idle() { self.finish_fade(); }
        } else {
            let state = self.snapshot();
            let remaining = f64::from(state.duration_s - state.position_s);
            if state.phase == PlaybackPhase::Playing && state.duration_s > 0.0
                && remaining > 0.0 && remaining <= self.crossfade_s {
                self.promote_deck(true);
            }
        }
    }

    pub fn poll_normalization(&mut self) { self.update_normalization(); }

    fn finish_fade(&mut self) {
        if self.fade.take().is_some() {
            self.fade_factor = 1.0;
            self.apply_volume();
        }
    }

    fn promote_deck(&mut self, overlap: bool) -> bool {
        let Some(prepared) = &self.prepared else { return false; };
        let Some(deck) = &prepared.deck else { return false; };
        if deck.phase != PlaybackPhase::Playing || (overlap && prepared.same_album && !self.overlap_album) {
            return false;
        }
        let state = self.snapshot();
        let fade_seconds = self.crossfade_s.min(f64::from(state.duration_s - state.position_s)).max(0.02);
        let prepared = self.prepared.take().unwrap();
        let mut next = prepared.deck.unwrap();
        let incoming_gain = next.gain_db;
        next.volume = self.volume;
        next.fade_factor = if overlap { 0.0 } else { 1.0 };
        next.apply_volume();
        next.set_paused(false);
        std::mem::swap(&mut self.mpv, &mut next.mpv);
        std::mem::swap(&mut self.eq_filter_active, &mut next.eq_filter_active);
        std::mem::swap(&mut self.spectrum_tap, &mut next.spectrum_tap);
        std::mem::swap(&mut self.spectrum_pending, &mut next.spectrum_pending);
        std::mem::swap(&mut self.entry_id, &mut next.entry_id);
        std::mem::swap(&mut self.hub_source, &mut next.hub_source);
        next.phase = PlaybackPhase::Playing;
        next.gain_db = self.gain_db;
        next.fade_factor = 1.0;
        self.phase = PlaybackPhase::Playing;
        self.awaiting_start = false;
        self.user_stopped = false;
        self.notice = None;
        self.audio_track = None;
        self.gain_db = incoming_gain;
        self.fade_factor = if overlap { 0.0 } else { 1.0 };
        if overlap { self.fade = Some(Fade { outgoing: next, seconds: fade_seconds }); }
        self.promoted = Some(prepared.key);
        true
    }

    /// Status line for the window. The Navidrome line is used when mpv is quiet.
    pub fn status_or(&self, fallback: &str) -> String {
        self.notice.clone().unwrap_or_else(|| fallback.to_string())
    }

    pub fn notice(&self) -> Option<&str> {
        self.notice.as_deref()
    }

    pub fn load(&mut self, target: &str) -> Result<(), String> {
        self.load_cookie(target, "")
    }

    /// `cookie` is the Hub session for `/api/listen/stream`. It is sent as a header
    /// and never written into the URL or into an error.
    pub fn load_cookie(&mut self, target: &str, cookie: &str) -> Result<(), String> {
        self.load_cookie_at(target, cookie, 0.0, false)
    }

    /// Set pause before opening the file: a restored session must never emit audio.
    /// `start` is explicitly reset on every normal load as well.
    pub fn load_cookie_at(
        &mut self,
        target: &str,
        cookie: &str,
        position_s: f32,
        paused: bool,
    ) -> Result<(), String> {
        self.skip_failed_media = false;
        self.cancel_preload();
        self.finish_fade();
        self.promoted = None;
        self.audio_track = None;
        self.album_tracks.clear();
        self.gain_db = 0.0;
        self.apply_volume();
        if self.mpv.is_none() {
            let err = self
                .notice
                .clone()
                .unwrap_or_else(|| "Звук: libmpv недоступен".into());
            self.notice = Some(err.clone());
            self.phase = PlaybackPhase::Error;
            return Err(err);
        }
        // Drop the previous file's end event before the new file starts.
        self.discard_events();
        self.user_stopped = false;
        self.awaiting_start = true;
        self.hub_source = !cookie.is_empty();
        self.phase = PlaybackPhase::Loading;
        self.notice = None;
        let loaded = {
            let mpv = self.mpv.as_ref().unwrap();
            let start = if position_s.is_finite() {
                position_s.max(0.0)
            } else {
                0.0
            };
            let start = format!("{start:.3}");
            apply_cookie(mpv, cookie);
            mpv.set_property("pause", paused)
                .and_then(|_| mpv.set_property("start", start.as_str()))
                .and_then(|_| mpv.command("loadfile", &[target, "replace"]))
        };
        if let Some(tap) = &self.spectrum_tap { tap.clear(); }
        if loaded.is_err() {
            let text = "Не удалось открыть файл".to_string();
            self.notice = Some(text.clone());
            self.phase = PlaybackPhase::Error;
            self.awaiting_start = false;
            self.user_stopped = true;
            return Err(text);
        }
        // loadfile only accepted the request. FileLoaded/EndFile tell us whether
        // the asynchronous open actually succeeded.
        self.entry_id = self.mpv.as_ref().and_then(|m| m.get_property("playlist/0/id").ok());
        self.apply_eq();
        Ok(())
    }

    pub fn is_idle(&self) -> bool {
        matches!(
            self.phase,
            PlaybackPhase::Idle
                | PlaybackPhase::Stopped
                | PlaybackPhase::Ended
                | PlaybackPhase::Error
        )
    }

    pub fn toggle(&mut self) {
        let Some(mpv) = self.mpv.as_ref() else {
            return;
        };
        let paused = flag(mpv, "pause").unwrap_or(true);
        self.set_paused(!paused);
    }

    /// Also works before FileLoaded, so loading does not swallow pause requests.
    pub fn set_paused(&mut self, paused: bool) {
        if self.is_idle() {
            return;
        }
        if let Some(mpv) = self.mpv.as_ref() {
            let _ = mpv.set_property("pause", paused);
        }
        if let Some(tap) = &self.spectrum_tap { tap.clear(); }
        if let Some(fade) = &mut self.fade {
            fade.outgoing.set_paused(paused);
        }
    }

    pub fn stop(&mut self) {
        self.skip_failed_media = false;
        self.cancel_preload();
        self.finish_fade();
        self.promoted = None;
        if let Some(analyzer) = &mut self.analyzer { analyzer.cancel(); }
        // Mark this before the command, so an end event already in the queue is not a new track.
        self.user_stopped = true;
        self.awaiting_start = false;
        self.phase = PlaybackPhase::Stopped;
        self.notice = None;
        let Some(mpv) = self.mpv.as_ref() else {
            return;
        };
        let _ = mpv.command("stop", &[]);
        let _ = mpv.set_property("pause", true);
    }

    pub(crate) fn fail_to_open(&mut self, message: &str) {
        self.stop();
        self.phase = PlaybackPhase::Error;
        self.notice = Some(message.to_owned());
    }

    /// Drain asynchronous load and playback events before taking a snapshot.
    /// Return true exactly once for EOF, never for stop, replacement or errors.
    pub fn take_natural_end(&mut self) -> bool {
        self.audio_tick();
        use PlaybackEvent as Event;
        use libmpv2::mpv_end_file_reason as reason;

        let Some(mpv) = self.mpv.as_ref() else {
            return false;
        };
        let mut eof = false;
        while let Some(event) = playback_event(mpv) {
            // Stop is authoritative even when StartFile/FileLoaded were queued.
            if self.user_stopped {
                continue;
            }
            match event {
                Event::StartFile(entry) => {
                    if self.awaiting_start && self.entry_id.is_some_and(|id| id != entry) {
                        continue;
                    }
                    if self.native_ending {
                        if let Some(prepared) = self.prepared.take() {
                            self.promoted = Some(prepared.key);
                            self.audio_track = None;
                            if !prepared.same_album || self.normalization != Normalization::Album {
                                self.gain_db = 0.0;
                            }
                            self.apply_volume();
                        }
                        self.native_ending = false;
                    }
                    self.entry_id = Some(entry);
                    if let Some(tap) = &self.spectrum_tap { tap.clear(); }
                    self.awaiting_start = false;
                    self.phase = PlaybackPhase::Loading;
                    eof = false;
                }
                _ if self.awaiting_start => {}
                Event::EndFile(_, _, entry) if self.entry_id != Some(entry) => {}
                Event::FileLoaded => {
                    self.phase = PlaybackPhase::Playing;
                    self.notice = None;
                    eof = false;
                }
                Event::EndFile(reason::Eof, _, _) => {
                    self.phase = PlaybackPhase::Ended;
                    // Restoring an old position beyond a shortened file must not
                    // advance into an audible next track while pause is requested.
                    eof = !flag(mpv, "pause").unwrap_or(true);
                    if let Some(prepared) = self.prepared.as_ref().filter(|p| eof && p.deck.is_none()) {
                        self.hub_source = prepared.hub_source;
                        self.native_ending = true;
                        self.phase = PlaybackPhase::Loading;
                        eof = false;
                    }
                }
                Event::EndFile(reason::Stop | reason::Quit, _, _) => {
                    self.phase = PlaybackPhase::Stopped;
                    eof = false;
                }
                Event::EndFile(reason::Redirect, _, _) => {
                    self.entry_id = None;
                    self.phase = PlaybackPhase::Loading;
                    self.awaiting_start = true;
                    eof = false;
                }
                Event::EndFile(reason::Error, error, _) => {
                    self.skip_failed_media = !flag(mpv, "pause").unwrap_or(true);
                    // mpv may already have started the appended entry. Queue policy
                    // (not its native playlist) decides the successor after failure.
                    if self.prepared.as_ref().is_some_and(|p| p.deck.is_none()) {
                        let _ = mpv.command("stop", &[]);
                    }
                    self.user_stopped = true;
                    self.phase = PlaybackPhase::Error;
                    self.notice = Some(media_error(error));
                    eof = false;
                }
                Event::QueueOverflow => {
                    self.phase = PlaybackPhase::Error;
                    self.notice = Some("Переполнена очередь событий плеера".into());
                    eof = false;
                }
                Event::Shutdown => {
                    self.phase = PlaybackPhase::Stopped;
                    eof = false;
                }
                _ => {}
            }
        }
        if matches!(
            self.phase,
            PlaybackPhase::Ended | PlaybackPhase::Stopped | PlaybackPhase::Error
        ) {
            self.user_stopped = true;
        }
        if eof && self.promote_deck(false) {
            return false;
        }
        eof
    }

    pub fn seek_ratio(&mut self, ratio: f32) {
        if !ratio.is_finite() { return; }
        let Some(duration) = self.mpv.as_ref().and_then(|mpv| prop_f64(mpv, "duration")) else { return; };
        self.seek_seconds(f64::from(ratio.clamp(0.0, 1.0)) * duration);
    }

    /// Timestamp callers must not round-trip through the slider's f32 ratio.
    pub fn seek_seconds(&mut self, seconds: f64) {
        if !seconds.is_finite() || !self.snapshot().seekable { return; }
        self.finish_fade();
        let Some(mpv) = self.mpv.as_ref() else { return; };
        let Some(duration) = prop_f64(mpv, "duration").filter(|v| *v > 0.0) else { return; };
        let mut target = seconds.clamp(0.0, duration);
        if target > 0.0 && target < duration {
            if let Some(rate) = prop_f64(mpv, "audio-params/samplerate").filter(|v| *v > 0.0) {
                // mpv trims PCM by truncating to a whole sample. Request the next
                // sample, with a fractional-sample roundoff guard, rather than
                // landing just BEFORE an exact timestamp (e.g. 17.96 at 8 kHz).
                target = (target + 1.001 / rate).min(duration);
            }
        }
        if flag(mpv, "pause") == Some(true) {
            // mpv's push-output queue retains prepause_state.delay across seek.
            // Reset only the output to discard that stale hardware latency;
            // the file, decode context, filters and paused intent remain intact.
            let _ = mpv.command("ao-reload", &[]);
        }
        let target = format!("{target:.9}");
        let _ = mpv.command("seek", &[&target, "absolute+exact"]);
        if let Some(tap) = &self.spectrum_tap { tap.clear(); }
    }

    /// Native timestamp and seekability; unseekable streams still have a playhead.
    pub fn playback_position(&self) -> Option<(f64, bool)> {
        if self.is_idle() || self.phase == PlaybackPhase::Loading { return None; }
        let mpv = self.mpv.as_ref()?;
        let position = prop_f64(mpv, "time-pos")?;
        let seekable = prop_f64(mpv, "duration").is_some_and(|duration| duration > 0.0)
            && flag(mpv, "seekable").unwrap_or(false);
        Some((position, seekable))
    }

    pub fn set_volume(&mut self, volume: f32) {
        self.volume = volume.clamp(0.0, 1.0);
        if let Some(fade) = &mut self.fade { fade.outgoing.volume = self.volume; }
        self.apply_volume();
    }

    /// Attach/detach just our labeled tap without loading, seeking or changing gain.
    pub(crate) fn set_meter_mode(&mut self, mode: MeterMode) {
        if self.meter_mode == mode { return; }
        self.spectrum_pending = mode == MeterMode::Spectrum;
        let attached = match mode {
            MeterMode::Off => false,
            MeterMode::Stereo => self.mpv.as_ref().is_some_and(|m| m.command("af", &["add", METER_FILTER]).is_ok()),
            MeterMode::Spectrum => self.attach_spectrum(),
        };
        if !attached && self.meter_mode != MeterMode::Off {
            if let Some(mpv) = &self.mpv { let _ = mpv.command("af", &["remove", "@meter"]); }
        }
        if mode != MeterMode::Spectrum {
            self.spectrum_tap = None;
        }
        self.meter_mode = mode;
        if let Some(deck) = self.prepared.as_mut().and_then(|p| p.deck.as_mut()) {
            deck.set_meter_mode(mode);
        }
        if let Some(fade) = &mut self.fade { fade.outgoing.set_meter_mode(mode); }
    }

    fn attach_spectrum(&mut self) -> bool {
        // An idle mpv accepts even an invalid lavfi graph and can subsequently
        // fail the file open. On a live filter chain `af add` validates the
        // graph (including the UDP protocol) and rolls back an unsupported tap.
        let Some(mpv) = &self.mpv else { return false; };
        if self.phase != PlaybackPhase::Playing || mpv.get_property::<i64>("audio-out-params/channel-count").is_err() {
            return false;
        }
        self.spectrum_pending = false;
        let Ok((tap, filter)) = SpectrumTap::open() else { return false; };
        if mpv.command("af", &["add", &filter]).is_err() { return false; }
        self.spectrum_tap = Some(Box::new(tap));
        true
    }

    /// Post-EQ peak envelope, scaled to the same -60..0 dB range on both channels.
    pub fn meter_levels(&self) -> [f32; 2] {
        self.mixed_levels(MeterMode::Stereo, |_, mpv| {
            let Ok(tags) = mpv.get_property::<analysis::Metadata>("af-metadata/meter") else {
                return [f64::NEG_INFINITY; 2];
            };
            let left = tags.raw_number("lavfi.astats.1.Peak_level").unwrap_or(f64::NEG_INFINITY);
            [left, tags.raw_number("lavfi.astats.2.Peak_level").unwrap_or(left)] // mono
        })
    }

    pub(crate) fn spectrum_levels(&self) -> [f32; 10] {
        self.mixed_levels(MeterMode::Spectrum, |deck, _| {
            deck.spectrum_tap.as_deref().map_or([f64::NEG_INFINITY; 10], SpectrumTap::peaks)
        })
    }

    fn mixed_levels<const N: usize>(&self, mode: MeterMode, peaks: fn(&Self, &Mpv) -> [f64; N]) -> [f32; N] {
        if self.meter_mode != mode { return [0.0; N]; }
        let mut db = self.deck_peak_db(peaks);
        if let Some(fade) = &self.fade {
            let outgoing = fade.outgoing.deck_peak_db(peaks);
            for (current, previous) in db.iter_mut().zip(outgoing) {
                // Independent outputs expose peaks, not phase-aligned PCM. Sum their
                // weighted peak amplitudes (an upper bound on the mixed peak).
                let sum = 10f64.powf(*current / 20.0) + 10f64.powf(previous / 20.0);
                *current = 20.0 * sum.log10();
            }
        }
        db.map(|value| ((value + 60.0) / 60.0).clamp(0.0, 1.0) as f32)
    }

    fn deck_peak_db<const N: usize>(&self, peaks: fn(&Self, &Mpv) -> [f64; N]) -> [f64; N] {
        let audible = self.phase == PlaybackPhase::Playing && self.volume > 0.0 && self.fade_factor > 0.0;
        // core-idle includes explicit pause, cache underruns, seeking and EOF;
        // the last decoded measurement alone doesn't reflect those states.
        let Some(mpv) = self.mpv.as_ref().filter(|m| audible && flag(m, "core-idle") == Some(false)) else {
            if let Some(tap) = &self.spectrum_tap { tap.clear(); }
            return [f64::NEG_INFINITY; N];
        };
        // mpv applies its cubic volume curve, normalization and fade after user filters.
        let gain_db = self.gain_db + 20.0 * (f64::from(self.volume).powi(3) * self.fade_factor).log10();
        peaks(self, mpv).map(|db| if db.is_finite() { db + gain_db } else { f64::NEG_INFINITY })
    }

    pub fn eq(&self) -> Settings {
        self.eq
    }

    pub fn restore_eq(&mut self, settings: Settings) {
        self.eq.enabled = settings.enabled;
        self.eq.preamp = eq::clamp_db(settings.preamp);
        self.eq.bands = settings.bands.map(eq::clamp_db);
        self.apply_eq();
    }

    pub fn set_eq_enabled(&mut self, enabled: bool) {
        self.eq.enabled = enabled;
        self.apply_eq();
    }

    pub fn set_preamp(&mut self, db: f32) {
        self.eq.preamp = eq::clamp_db(db);
        self.apply_eq();
    }

    pub fn set_band(&mut self, index: usize, db: f32) {
        if let Some(band) = self.eq.bands.get_mut(index) {
            *band = eq::clamp_db(db);
            self.apply_eq();
        }
    }

    pub fn reset_eq(&mut self) {
        self.eq.preamp = 0.0;
        self.eq.bands = [0.0; 10];
        self.apply_eq();
    }

    pub fn snapshot(&self) -> PlaybackState {
        let mut state = PlaybackState {
            phase: self.phase,
            resolving_source: self.phase == PlaybackPhase::Loading && self.hub_source,
            seekable: false,
            playing: false,
            position_s: 0.0,
            duration_s: 0.0,
            volume: self.volume,
        };
        let Some(mpv) = self.mpv.as_ref() else {
            return state;
        };
        state.volume = self.volume;
        if self.is_idle() {
            return state;
        }
        let paused = flag(mpv, "pause").unwrap_or(true);
        state.playing = !paused;
        if self.phase == PlaybackPhase::Loading {
            return state;
        }
        state.phase = active_phase(paused, flag(mpv, "paused-for-cache").unwrap_or(false));
        state.duration_s = prop_f64(mpv, "duration").unwrap_or(0.0) as f32;
        // A live stream can have a real position without a known duration.
        state.position_s = prop_f64(mpv, "time-pos").unwrap_or(0.0) as f32;
        state.seekable = state.duration_s > 0.0 && flag(mpv, "seekable").unwrap_or(false);
        state
    }

    fn discard_events(&self) {
        let Some(mpv) = self.mpv.as_ref() else {
            return;
        };
        while mpv.wait_event(0.0).is_some() {}
    }

    fn apply_volume(&self) {
        if let Some(mpv) = self.mpv.as_ref() {
            let _ = mpv.set_property("volume", volume_setting(self.volume, self.gain_db, self.fade_factor));
        }
        if let Some(fade) = &self.fade {
            fade.outgoing.apply_volume();
        }
    }

    fn apply_eq(&mut self) {
        let Some(mpv) = self.mpv.as_ref() else {
            return;
        };
        match eq::chain(self.eq) {
            Some(filter) => {
                // mpv replaces a matching label in place; a new EQ goes before
                // the optional meter so both see the same post-EQ signal.
                if mpv.command("af", &["pre", &format!("@eq:{filter}")]).is_ok() {
                    self.eq_filter_active = true;
                }
            }
            None if self.eq_filter_active => {
                if mpv.command("af", &["remove", "@eq"]).is_ok() {
                    self.eq_filter_active = false;
                }
            }
            None => {}
        }
        if let Some(prepared) = &mut self.prepared {
            if let Some(deck) = &mut prepared.deck { deck.restore_eq(self.eq); }
        }
        if let Some(fade) = &mut self.fade { fade.outgoing.restore_eq(self.eq); }
    }

    #[cfg(test)]
    fn audio_filter(&self) -> String {
        self.mpv
            .as_ref()
            .and_then(|mpv| mpv.get_property::<String>("af").ok())
            .unwrap_or_default()
    }
}

fn active_phase(paused: bool, paused_for_cache: bool) -> PlaybackPhase {
    if paused {
        PlaybackPhase::Paused
    } else if paused_for_cache {
        PlaybackPhase::Buffering
    } else {
        // Duration is deliberately irrelevant: live streams are still playing.
        PlaybackPhase::Playing
    }
}

fn ensure_c_numeric() {
    unsafe {
        libc::setlocale(libc::LC_NUMERIC, c"C".as_ptr() as *const c_char);
    }
}

fn media_error(code: libmpv2::MpvError) -> String {
    use libmpv2::mpv_error as error;
    match code {
        error::LoadingFailed => "Не удалось загрузить файл или поток",
        error::UnknownFormat => "Не удалось распознать формат файла",
        error::NothingToPlay => "В файле нет дорожки для воспроизведения",
        error::AoInitFailed => "Не удалось открыть устройство вывода звука",
        error::VoInitFailed => "Не удалось открыть устройство вывода видео",
        error::Unsupported => "Система не поддерживает воспроизведение этого файла",
        error::NoMem => "Недостаточно памяти для воспроизведения",
        error::Success => "Не удалось воспроизвести файл",
        _ => return format!("Ошибка воспроизведения (код mpv: {code})"),
    }.into()
}

fn open_error(err: &libmpv2::Error) -> String {
    match err {
        libmpv2::Error::VersionMismatch { .. } => "Звук: версия libmpv не совпадает".into(),
        libmpv2::Error::Null => "Звук: libmpv не выделил плеер".into(),
        other => format!("Звук: {other}"),
    }
}

/// Header value for one Hub session. `None` clears the header.
/// A cookie with a quote or a line break is refused so it cannot break the command.
pub fn cookie_header(cookie: &str) -> Option<String> {
    if cookie.is_empty() {
        return None;
    }
    if cookie.bytes().any(|byte| matches!(byte, b'"' | b'\n' | b'\r')) {
        return None;
    }
    Some(format!("Cookie: hm_session={cookie}"))
}

fn apply_cookie(mpv: &Mpv, cookie: &str) {
    let _ = mpv.command("change-list", &["http-header-fields", "clr"]);
    let Some(header) = cookie_header(cookie) else {
        return;
    };
    let _ = mpv.command("change-list", &["http-header-fields", "append", &header]);
}

fn flag(mpv: &Mpv, name: &str) -> Option<bool> {
    match mpv.get_property::<String>(name) {
        Ok(value) => Some(value == "yes" || value == "true"),
        Err(_) => None,
    }
}

fn prop_f64(mpv: &Mpv, name: &str) -> Option<f64> {
    mpv.get_property::<f64>(name)
        .ok()
        .filter(|value| value.is_finite() && *value >= 0.0)
}

#[cfg(test)]
mod tests {
    #[test]
    fn normalization_and_fade_have_correct_physical_pcm_amplitude() {
        use libmpv2::{events::Event, mpv_end_file_reason};
        ensure_c_numeric();
        let root = std::env::temp_dir();
        let source = root.join(format!("hm-physical-gain-{}.wav", std::process::id()));
        let output = root.join(format!("hm-physical-gain-{}.raw", std::process::id()));
        std::fs::write(&source, tone_wav_at_rate(0.2, 8_000)).unwrap();
        let output_name = output.to_string_lossy();
        let mut rms = Vec::new();
        for (gain, fade) in [(0.0, 1.0), (-6.0, 1.0), (6.0, 1.0), (0.0, 0.5)] {
            let mpv = Mpv::with_initializer(|init| {
                init.set_option("vid", "no")?;
                init.set_option("audio-display", "no")?;
                init.set_option("msg-level", "all=no")?;
                init.set_option("ao", "pcm")?;
                init.set_option("ao-pcm-file", output_name.as_ref())?;
                init.set_option("ao-pcm-waveheader", "no")?;
                init.set_option("audio-format", "s16")?;
                init.set_option("volume-max", "1000")?;
                Ok(())
            }).unwrap();
            mpv.set_property("volume", volume_setting(0.8, gain, fade)).unwrap();
            mpv.command("loadfile", &[source.to_string_lossy().as_ref(), "replace"]).unwrap();
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                match mpv.wait_event(0.02) {
                    Some(Ok(Event::EndFile(mpv_end_file_reason::Eof))) => break,
                    Some(Err(error)) => panic!("PCM capture failed: {error}"),
                    _ => {}
                }
                assert!(Instant::now() < deadline, "PCM capture did not finish");
            }
            drop(mpv); // Flush and close the real native audio output.
            let bytes = std::fs::read(&output).unwrap();
            assert_eq!(bytes.len(), 3_200);
            let energy: f64 = bytes.chunks_exact(2).map(|sample| {
                f64::from(i16::from_le_bytes([sample[0], sample[1]])).powi(2)
            }).sum();
            rms.push((energy / 1_600.0).sqrt());
        }
        // Allow native s16 gain quantization, not a different volume law.
        assert!((20.0 * (rms[1] / rms[0]).log10() + 6.0).abs() < 0.08);
        assert!((20.0 * (rms[2] / rms[0]).log10() - 6.0).abs() < 0.08);
        assert!((rms[3] / rms[0] - 0.5).abs() < 0.005);
        let _ = std::fs::remove_file(source);
        let _ = std::fs::remove_file(output);
    }

    #[test]
    fn paused_timestamp_seek_lands_on_the_requested_lyric_not_before_it() {
        let path = std::env::temp_dir().join(format!("hm-precise-seek-{}.wav", std::process::id()));
        std::fs::write(&path, tone_wav_at_rate(20.0, 8_000)).unwrap();
        let mut player = open_null();
        player.load(&path.to_string_lossy()).unwrap();
        wait_until(&mut player, |state| state.position_s > 0.1);
        player.set_paused(true);
        wait_until(&mut player, |state| state.phase == PlaybackPhase::Paused);
        player.seek_seconds(17.96);
        let seeked = wait_until(&mut player, |state| {
            f64::from(state.position_s) >= 17.96 && f64::from(state.position_s) < 17.961
        });
        assert_eq!(seeked.phase, PlaybackPhase::Paused);
        assert!(!seeked.playing);
        assert!((seeked.duration_s - 20.0).abs() < 0.001);
        std::thread::sleep(Duration::from_millis(100));
        assert!((player.snapshot().position_s - seeked.position_s).abs() < 0.0002);
        player.set_paused(false);
        wait_until(&mut player, |state| state.playing && state.position_s > 18.0);
        player.stop();
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn prepared_native_next_promotes_once_without_reopening() {
        let root = std::env::temp_dir();
        let first = root.join(format!("hm-gap-first-{}.wav", std::process::id()));
        let second = root.join(format!("hm-gap-second-{}.wav", std::process::id()));
        std::fs::write(&first, tone_wav(0.8)).unwrap();
        std::fs::write(&second, tone_wav(2.0)).unwrap();
        let mut player = open_null();
        player.load(&first.to_string_lossy()).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Playing);
        player.prepare_next(&second.to_string_lossy(), "", "next-occurrence", true).unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            assert!(!player.take_natural_end(), "preloaded transition fell back to reload");
            if let Some(key) = player.take_promoted() {
                assert_eq!(key, "next-occurrence");
                break;
            }
            assert!(Instant::now() < deadline, "missing promotion: {}", player.status_or(""));
            std::thread::sleep(Duration::from_millis(10));
        }
        let position = wait_until(&mut player, |s| s.position_s > 0.15).position_s;
        assert!(player.take_promoted().is_none());
        assert!(player.snapshot().position_s >= position);
        assert!(player.snapshot().duration_s > 1.8);
        player.stop();
        let _ = std::fs::remove_file(first);
        let _ = std::fs::remove_file(second);
    }

    #[test]
    fn crossfade_promotes_the_open_deck_and_pause_holds_both() {
        let path = std::env::temp_dir().join(format!("hm-crossfade-{}.wav", std::process::id()));
        std::fs::write(&path, tone_wav(2.0)).unwrap();
        let mut player = open_null();
        player.set_crossfade(0.8, false);
        player.load(&path.to_string_lossy()).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Playing);
        player.prepare_next(&path.to_string_lossy(), "", "second", false).unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        while player.take_promoted().is_none() {
            assert!(!player.take_natural_end());
            assert!(Instant::now() < deadline, "crossfade never promoted");
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(player.fade.is_some());
        assert!(player.snapshot().position_s < 0.5);
        player.set_paused(true);
        assert_eq!(flag(player.mpv.as_ref().unwrap(), "pause"), Some(true));
        assert_eq!(flag(player.fade.as_ref().unwrap().outgoing.mpv.as_ref().unwrap(), "pause"), Some(true));
        player.seek_ratio(0.5);
        assert!(player.fade.is_none(), "seek must silence the old track");
        player.stop();
        assert!(player.prepared.is_none());
        assert!(!player.take_natural_end());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn untagged_audio_analysis_applies_gain_and_off_restores_user_volume() {
        let path = std::env::temp_dir().join(format!("hm-normalization-{}.wav", std::process::id()));
        std::fs::write(&path, tone_wav(2.0)).unwrap();
        let mut player = open_null();
        player.load_cookie_at(&path.to_string_lossy(), "", 0.0, true).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Paused);
        player.set_audio_context(AudioTrack {
            key: "untagged-test".into(), target: path.to_string_lossy().into_owned(),
            cookie: String::new(), album: String::new(),
        }, Vec::new());
        player.set_normalization(Normalization::Track);
        let deadline = Instant::now() + Duration::from_secs(8);
        while player.gain_db.abs() < 0.01 {
            player.poll_normalization();
            assert!(Instant::now() < deadline, "{}", player.normalization_status());
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(player.gain_db.is_finite());
        let applied = prop_f64(player.mpv.as_ref().unwrap(), "volume").unwrap();
        assert!((applied - f64::from(player.volume) * 100.0).abs() > 0.1);
        player.set_normalization(Normalization::Off);
        assert!((prop_f64(player.mpv.as_ref().unwrap(), "volume").unwrap() - 80.0).abs() < 0.01);
        player.stop();
        let _ = std::fs::remove_file(path);
    }

    use super::*;
    use std::io::{Read, Write};
    use std::path::PathBuf;
    use std::time::{Duration, Instant};

    #[test]
    fn the_session_cookie_stays_out_of_a_refused_header() {
        assert_eq!(
            cookie_header("abc.def"),
            Some("Cookie: hm_session=abc.def".into())
        );
        assert!(cookie_header("bad\"cookie").is_none());
        assert!(cookie_header("").is_none());
    }

    fn wait_until(player: &mut Player, pred: impl Fn(&PlaybackState) -> bool) -> PlaybackState {
        let start = Instant::now();
        loop {
            assert!(!player.take_natural_end(), "unexpected queue advance while waiting");
            let state = player.snapshot();
            if pred(&state) {
                return state;
            }
            if start.elapsed() > Duration::from_secs(5) {
                panic!("timeout: {state:?} notice={}", player.status_or(""));
            }
            std::thread::sleep(Duration::from_millis(30));
        }
    }

    fn wait_for_levels<const N: usize>(
        player: &mut Player, read: fn(&Player) -> [f32; N], matches: impl Fn([f32; N]) -> bool,
    ) {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            assert!(!player.take_natural_end(), "ended before expected meter levels");
            let levels = read(player);
            if matches(levels) { return; }
            assert!(Instant::now() < deadline, "last levels: {levels:?}, af: {}", player.audio_filter());
            std::thread::sleep(Duration::from_millis(15));
        }
    }

    #[test]
    fn stereo_meter_reads_live_channels_after_eq_and_detaches_without_reload() {
        let url = serve_wav(tone_channels_wav(6.0, 44_100, &[8192.0, 2048.0]));
        let mut player = open_null();
        player.set_volume(1.0);
        player.load(&url).unwrap();
        let before = wait_until(&mut player, |s| s.position_s > 0.15).position_s;
        assert!(!player.audio_filter().contains("astats"));
        assert_eq!(player.meter_levels(), [0.0; 2]);

        player.set_meter_mode(MeterMode::Stereo);
        // Peaks at -12 and -24 dBFS on the common -60..0 scale.
        wait_for_levels(&mut player, Player::meter_levels, |[l, r]| (l - 0.8).abs() < 0.02 && (r - 0.6).abs() < 0.02);
        player.set_eq_enabled(true);
        player.set_preamp(-6.0);
        wait_for_levels(&mut player, Player::meter_levels, |[l, r]| (l - 0.7).abs() < 0.02 && (r - 0.5).abs() < 0.02);
        player.set_paused(true);
        assert_eq!(player.meter_levels(), [0.0; 2]);
        player.set_meter_mode(MeterMode::Off);
        player.set_paused(false);
        let filters = player.audio_filter();
        assert!(!filters.contains("astats") && filters.contains("volume=-6.0dB"), "{filters}");
        assert_eq!(player.meter_levels(), [0.0; 2]);
        assert!(player.snapshot().position_s >= before);
        player.stop();
    }

    #[test]
    fn stereo_meter_combines_weighted_decks_and_duplicates_mono() {
        let left_only = serve_wav(tone_channels_wav(3.0, 44_100, &[8192.0, 0.0]));
        let mono = serve_wav(tone_channels_wav(4.0, 44_100, &[8192.0]));
        let mut player = open_null();
        player.set_volume(1.0);
        player.set_crossfade(1.5, false);
        player.load(&left_only).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Playing);
        player.prepare_next(&mono, "", "mono", false).unwrap();
        player.set_meter_mode(MeterMode::Stereo); // Include the already prepared, paused deck.
        wait_for_levels(&mut player, Player::meter_levels, |[l, r]| (l - 0.8).abs() < 0.02 && r == 0.0);
        // Left stays at full source level while both decks contribute;
        // right is silent on the outgoing track and rises with the mono deck.
        wait_for_levels(&mut player, Player::meter_levels, |[l, r]| (l - 0.8).abs() < 0.02 && (r - 0.7).abs() < 0.02);
        assert!(player.fade.is_some());
        player.stop();
        assert_eq!(player.meter_levels(), [0.0; 2]);
    }

    #[test]
    fn frequency_spectrum_separates_mono_bass_and_right_only_treble_across_decks() {
        let low = serve_wav(frequency_wav(4.0, 8000, 62.5, &[16384.0]));
        let high = serve_wav(frequency_wav(6.0, 48000, 8000.0, &[0.0, 16384.0]));
        let mut player = open_null();
        player.set_volume(1.0);
        player.set_crossfade(1.5, false);
        player.load(&low).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Playing);
        assert_eq!(player.spectrum_levels(), [0.0; 10]);
        player.prepare_next(&high, "", "treble", false).unwrap();
        player.set_meter_mode(MeterMode::Spectrum);
        assert_eq!(player.meter_levels(), [0.0; 2]);
        wait_for_levels(&mut player, Player::spectrum_levels, |v| v[1] > 0.65 && v[8] < 0.05);
        wait_for_levels(&mut player, Player::spectrum_levels, |v| v[1] > 0.45 && v[8] > 0.45);
        assert!(player.fade.is_some());
        wait_for_levels(&mut player, Player::spectrum_levels, |v| v[8] > 0.65 && v[1] < 0.05);
        player.set_paused(true);
        assert_eq!(player.spectrum_levels(), [0.0; 10]);
        player.set_meter_mode(MeterMode::Stereo);
        player.set_paused(false);
        wait_for_levels(&mut player, Player::meter_levels, |[l, r]| l == 0.0 && r > 0.85);
        assert_eq!(player.spectrum_levels(), [0.0; 10]);
        player.set_meter_mode(MeterMode::Off);
        assert!(!player.audio_filter().contains("astats"));
        assert!(player.spectrum_tap.is_none());
        player.stop();
    }

    fn tone_wav(seconds: f32) -> Vec<u8> {
        tone_wav_at_rate(seconds, 44_100)
    }

    fn tone_wav_at_rate(seconds: f32, rate: u32) -> Vec<u8> {
        tone_channels_wav(seconds, rate, &[8_000.0])
    }

    fn tone_channels_wav(seconds: f32, rate: u32, amplitudes: &[f32]) -> Vec<u8> {
        frequency_wav(seconds, rate, 440.0, amplitudes)
    }

    fn frequency_wav(seconds: f32, rate: u32, hz: f32, amplitudes: &[f32]) -> Vec<u8> {
        let channels = amplitudes.len() as u16;
        let stride = channels * 2;
        let samples = (rate as f32 * seconds) as u32;
        let data_bytes = samples * u32::from(stride);
        let mut bytes = Vec::with_capacity(44 + data_bytes as usize);
        bytes.extend(b"RIFF");
        bytes.extend((36 + data_bytes).to_le_bytes());
        bytes.extend(b"WAVEfmt ");
        bytes.extend(16u32.to_le_bytes());
        bytes.extend(1u16.to_le_bytes());
        bytes.extend(channels.to_le_bytes());
        bytes.extend(rate.to_le_bytes());
        bytes.extend((rate * u32::from(stride)).to_le_bytes());
        bytes.extend(stride.to_le_bytes());
        bytes.extend(16u16.to_le_bytes());
        bytes.extend(b"data");
        bytes.extend(data_bytes.to_le_bytes());
        for i in 0..samples {
            let t = i as f32 / rate as f32;
            let sample = (t * hz * std::f32::consts::TAU).sin();
            for amplitude in amplitudes {
                bytes.extend(((sample * amplitude) as i16).to_le_bytes());
            }
        }
        bytes
    }

    fn open_null() -> Player {
        let player = Player::open(PlayerOptions { null_audio: true });
        assert_eq!(player.status_or("ok"), "ok");
        player
    }

    fn assert_plays_and_seeks(player: &mut Player) {
        let started = wait_until(player, |state| state.duration_s > 0.8 && state.playing);
        let duration = started.duration_s;
        let moved = wait_until(player, |state| state.position_s > 0.12);
        assert!(moved.position_s < duration);
        player.seek_ratio(0.5);
        let half = duration * 0.5;
        let seeked = wait_until(player, |state| (state.position_s - half).abs() < 0.35);
        assert!(seeked.playing, "{seeked:?}");
        player.set_volume(0.25);
        let volume = player.snapshot().volume;
        assert!((volume - 0.25).abs() < 0.02, "{volume}");
        player.toggle();
        let paused = wait_until(player, |state| !state.playing && state.duration_s > 0.0);
        let mark = paused.position_s;
        std::thread::sleep(Duration::from_millis(250));
        let held = player.snapshot();
        assert!(!held.playing);
        assert!(
            (held.position_s - mark).abs() < 0.12,
            "{mark} -> {}",
            held.position_s
        );
        player.stop();
        let stopped = wait_until(player, |state| !state.playing && state.position_s < 0.05);
        let _ = stopped;
    }

    #[test]
    fn a_raised_band_reaches_mpv_and_a_flat_curve_clears_it() {
        let path = std::env::temp_dir().join("home music eq.wav");
        std::fs::write(&path, tone_wav(2.0)).unwrap();
        let mut player = open_null();
        player.load(&path.to_string_lossy()).unwrap();
        let _ = wait_until(&mut player, |state| state.playing && state.duration_s > 0.4);
        player.set_eq_enabled(true);
        player.set_band(5, 6.0);
        let filtered = player.audio_filter();
        assert!(
            filtered.contains("equalizer") && filtered.contains("g=6"),
            "{filtered}"
        );
        player.reset_eq();
        let cleared = player.audio_filter();
        assert!(cleared.is_empty() || !cleared.contains("g=6"), "{cleared}");
        player.stop();
    }

    #[test]
    fn local_file_with_spaces_seeks_with_mpv() {
        let path = std::env::temp_dir().join("home music tone.wav");
        std::fs::write(&path, tone_wav(2.0)).unwrap();
        let mut player = open_null();
        player.load(&path.to_string_lossy()).unwrap();
        assert_plays_and_seeks(&mut player);
    }

    #[test]
    fn http_url_seeks_with_mpv() {
        let url = serve_wav(tone_wav(2.0));
        let mut player = open_null();
        player.load(&url).unwrap();
        assert_plays_and_seeks(&mut player);
    }

    #[test]
    fn library_formats_report_duration() {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("testdata");
        for name in ["tone.mp3", "tone.opus", "tone.m4a"] {
            let path = root.join(name);
            let mut player = open_null();
            player
                .load(&path.to_string_lossy())
                .unwrap_or_else(|err| panic!("{name}: {err}"));
            let state = wait_until(&mut player, |state| state.duration_s > 1.0 && state.playing);
            assert!(
                state.duration_s < 2.2,
                "{name} duration {}",
                state.duration_s
            );
            player.seek_ratio(0.5);
            let half = state.duration_s * 0.5;
            wait_until(&mut player, |state| (state.position_s - half).abs() < 0.4);
        }
    }

    #[test]
    #[ignore = "needs desktop/config.toml and a running Navidrome"]
    fn streams_one_navidrome_track() {
        let config = crate::navidrome::ServerConfig::load();
        let track = crate::navidrome::first_track(&config).unwrap_or_else(|err| panic!("{err}"));
        assert!(!track.title.is_empty(), "empty title");
        assert!(
            !track.stream_url.contains(&config.password),
            "stream url contains the password"
        );
        let mut player = open_null();
        player
            .load(&track.stream_url)
            .unwrap_or_else(|err| panic!("{err}"));
        let state = wait_until(&mut player, |state| {
            state.duration_s > 1.0 && state.position_s > 0.1 && state.playing
        });
        assert!(state.position_s < state.duration_s);
        eprintln!("playing: {} — {}", track.artist, track.title);
        let mut cover = crate::navidrome::cover_art(&config, &track.id)
            .map(|bytes| (track.artist.clone(), track.title.clone(), bytes.len()));
        if cover.is_none() {
            for _ in 0..5 {
                let candidate =
                    crate::navidrome::first_track(&config).unwrap_or_else(|err| panic!("{err}"));
                if let Some(bytes) = crate::navidrome::cover_art(&config, &candidate.id) {
                    cover = Some((candidate.artist, candidate.title, bytes.len()));
                    break;
                }
            }
        }
        let (artist, title, len) = cover.expect("no cover in the sampled tracks");
        eprintln!("cover: {artist} — {title} ({len} bytes)");
    }

    #[test]
    #[ignore = "needs desktop/config.toml and a running Navidrome"]
    fn streams_the_first_queued_track() {
        let config = crate::navidrome::ServerConfig::load();
        let library = crate::navidrome::load_queue(&config).unwrap_or_else(|err| panic!("{err}"));
        assert!(library.tracks.len() >= 2, "count {}", library.tracks.len());
        let total: u64 = library
            .tracks
            .iter()
            .map(|track| u64::from(track.duration_s))
            .sum();
        eprintln!(
            "queue: {} ({} tracks, {total}s)",
            library.name,
            library.tracks.len()
        );
        let url = crate::navidrome::stream_url(&config, &library.tracks[0].id);
        assert!(
            !url.contains(&config.password),
            "stream url contains the password"
        );
        let mut player = open_null();
        player.load(&url).unwrap_or_else(|err| {
            let safe = if err.contains("http") || err.contains(&config.password) {
                "Не удалось открыть поток".to_string()
            } else {
                err
            };
            panic!("{safe}");
        });
        let state = wait_until(&mut player, |state| {
            state.duration_s > 1.0 && state.position_s > 0.1 && state.playing
        });
        assert!(state.position_s < state.duration_s);
    }

    #[test]
    fn natural_end_is_reported_once() {
        let path = std::env::temp_dir().join("home music end.wav");
        std::fs::write(&path, tone_wav(0.4)).unwrap();
        let mut player = open_null();
        player.load(&path.to_string_lossy()).unwrap();
        let start = Instant::now();
        let mut saw = false;
        while start.elapsed() < Duration::from_secs(4) {
            if player.take_natural_end() {
                saw = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(saw, "eof was not reported");
        assert!(!player.take_natural_end());
        std::thread::sleep(Duration::from_millis(200));
        assert!(!player.take_natural_end());
    }

    #[test]
    fn stop_is_not_a_natural_end() {
        let path = std::env::temp_dir().join("home music stop.wav");
        std::fs::write(&path, tone_wav(2.0)).unwrap();
        let mut player = open_null();
        player.load(&path.to_string_lossy()).unwrap();
        wait_until(&mut player, |state| state.playing && state.position_s > 0.05);
        player.stop();
        let start = Instant::now();
        while start.elapsed() < Duration::from_millis(800) {
            assert!(
                !player.take_natural_end(),
                "stop was treated as the end of the file"
            );
            std::thread::sleep(Duration::from_millis(40));
        }
    }

    #[test]
    fn play_without_a_file_does_nothing() {
        let mut player = open_null();
        player.toggle();
        let state = player.snapshot();
        assert!(!state.playing);
        assert_eq!(state.position_s, 0.0);
    }

    #[test]
    fn failed_media_signals_once_and_recovers() {
        let root = std::env::temp_dir();
        let missing = root.join(format!(
            "home-music-missing-{}-private-token.wav",
            std::process::id()
        ));
        let mut player = open_null();
        player.load(&missing.to_string_lossy()).unwrap();
        assert_eq!(player.snapshot().phase, PlaybackPhase::Loading);
        let failed = wait_until(&mut player, |state| state.phase == PlaybackPhase::Error);
        assert!(!failed.playing);
        assert!(!failed.seekable);
        assert_eq!(failed.duration_s, 0.0);
        assert!(!player.take_natural_end());
        assert_eq!(player.notice(), Some("Не удалось загрузить файл или поток"));
        assert!(player.take_failed_media());
        assert!(!player.take_failed_media());

        let path = root.join(format!("home-music-recovery-{}.wav", std::process::id()));
        std::fs::write(&path, tone_wav(2.0)).unwrap();
        player.load(&path.to_string_lossy()).unwrap();
        let recovered = wait_until(&mut player, |state| state.phase == PlaybackPhase::Playing);
        assert!(recovered.seekable);
        assert!(recovered.duration_s > 1.0);
        assert!(player.notice().is_none());
        player.stop();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn failed_media_never_skips_after_pause_stop_or_replacement() {
        let root = std::env::temp_dir();
        let missing = root.join(format!("hm-cancel-failed-{}.wav", std::process::id()));
        let missing = missing.to_string_lossy();
        let mut player = open_null();
        player.load_cookie_at(&missing, "", 0.0, true).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Error);
        assert!(!player.take_failed_media());

        player.load(&missing).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Error);
        player.stop();
        assert!(!player.take_failed_media());

        let path = root.join(format!("hm-replace-failed-{}.wav", std::process::id()));
        std::fs::write(&path, tone_wav(2.0)).unwrap();
        player.load(&missing).unwrap();
        player.load(&path.to_string_lossy()).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Playing);
        assert!(!player.take_failed_media());
        assert!(player.notice().is_none());
        player.fail_to_open("Нет адреса потока");
        assert!(!player.take_natural_end());
        assert_eq!(player.snapshot().phase, PlaybackPhase::Error);
        assert_eq!(player.notice(), Some("Нет адреса потока"));
        assert!(!player.take_failed_media());
        player.stop();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn failed_media_with_native_preload_leaves_the_successor_to_the_queue() {
        let root = std::env::temp_dir();
        let missing = root.join(format!("hm-bad-before-preload-{}.wav", std::process::id()));
        let next = root.join(format!("hm-native-after-bad-{}.wav", std::process::id()));
        std::fs::write(&next, tone_wav(2.0)).unwrap();
        let mut player = open_null();
        player.load(&missing.to_string_lossy()).unwrap();
        player.prepare_next(&next.to_string_lossy(), "", "native", true).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Error);
        assert!(player.take_failed_media());
        assert!(player.take_promoted().is_none());
        player.load(&next.to_string_lossy()).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Playing);
        assert!(!player.take_failed_media());
        player.stop();
        std::fs::remove_file(next).unwrap();
    }

    #[test]
    fn failed_media_in_native_successor_keeps_both_notifications() {
        let root = std::env::temp_dir();
        let path = root.join(format!("hm-before-failed-{}.wav", std::process::id()));
        let missing = root.join(format!("hm-preload-failed-{}.wav", std::process::id()));
        std::fs::write(&path, tone_wav(0.6)).unwrap();
        let mut player = open_null();
        player.load(&path.to_string_lossy()).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Playing);
        player.prepare_next(&missing.to_string_lossy(), "", "bad-next", true).unwrap();
        wait_until(&mut player, |s| s.phase == PlaybackPhase::Error);
        assert_eq!(player.take_promoted().as_deref(), Some("bad-next"));
        assert!(player.take_failed_media());
        assert!(!player.take_failed_media());
        player.stop();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn malformed_http_media_does_not_expose_url_or_cookie() {
        let url = format!("{}?token=private-token", serve_wav(b"not audio".to_vec()));
        let mut player = open_null();
        player.load_cookie(&url, "private-session").unwrap();
        wait_until(&mut player, |state| state.phase == PlaybackPhase::Error);
        assert_eq!(player.notice(), Some("Не удалось распознать формат файла"));
        assert!(!player.take_natural_end());
        assert!(player.take_failed_media());
    }

    #[test]
    fn failed_media_full_stream_503_is_not_an_eof() {
        let url = format!(
            "{}?ref=dz%3A370418141&full=1",
            serve_response(
                br#"{"detail":"Full recording unavailable"}"#.to_vec(),
                "503 Service Unavailable",
            )
        );
        let mut player = open_null();
        player.load_cookie(&url, "private-session").unwrap();
        assert!(player.snapshot().resolving_source);
        let failed = wait_until(&mut player, |state| state.phase == PlaybackPhase::Error);
        assert!(!failed.resolving_source);
        assert!(!failed.playing);
        assert!(!failed.seekable);
        assert_eq!(failed.duration_s, 0.0);
        assert_eq!(failed.position_s, 0.0);
        assert_eq!(player.notice(), Some("Не удалось загрузить файл или поток"));
        assert!(player.take_failed_media());
        for _ in 0..5 {
            assert!(!player.take_natural_end(), "unavailable audio counted as EOF");
            assert!(!player.take_failed_media());
            assert_eq!(player.snapshot().phase, PlaybackPhase::Error);
            std::thread::sleep(Duration::from_millis(30));
        }
    }

    #[test]
    fn replacing_playback_clears_old_timing_and_honors_pause_during_load() {
        let path = std::env::temp_dir().join(format!(
            "home-music-replacement-{}.wav",
            std::process::id()
        ));
        std::fs::write(&path, tone_wav(2.0)).unwrap();
        let mut player = open_null();
        player.load(&path.to_string_lossy()).unwrap();
        wait_until(&mut player, |state| state.position_s > 0.05);
        player.load(&path.to_string_lossy()).unwrap();
        let loading = player.snapshot();
        assert_eq!(loading.phase, PlaybackPhase::Loading);
        assert!(!loading.resolving_source);
        assert_eq!(loading.position_s, 0.0);
        assert_eq!(loading.duration_s, 0.0);
        assert!(!loading.seekable);
        assert!(!player.is_idle());
        player.toggle();
        let paused = wait_until(&mut player, |state| state.phase == PlaybackPhase::Paused);
        assert!(!paused.playing);
        assert!(paused.duration_s > 1.0);
        assert!(!player.take_natural_end());
        player.set_paused(false);
        wait_until(&mut player, |state| state.phase == PlaybackPhase::Playing);
        player.stop();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn stop_before_draining_a_finished_load_never_advances() {
        let path = std::env::temp_dir().join(format!(
            "home-music-stop-pending-{}.wav",
            std::process::id()
        ));
        std::fs::write(&path, tone_wav(0.1)).unwrap();
        let mut player = open_null();
        player.load(&path.to_string_lossy()).unwrap();
        std::thread::sleep(Duration::from_millis(500));
        player.stop();
        assert!(!player.take_natural_end());
        let stopped = player.snapshot();
        assert_eq!(stopped.phase, PlaybackPhase::Stopped);
        assert!(!stopped.seekable);
        assert!(!stopped.playing);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn active_playback_does_not_require_a_duration_and_pause_beats_cache_wait() {
        assert_eq!(active_phase(false, false), PlaybackPhase::Playing);
        assert_eq!(active_phase(false, true), PlaybackPhase::Buffering);
        assert_eq!(active_phase(true, true), PlaybackPhase::Paused);
        assert_eq!(active_phase(true, false), PlaybackPhase::Paused);
    }

    #[test]
    fn replacing_a_finished_file_discards_its_pending_natural_end() {
        let path = std::env::temp_dir().join(format!(
            "home-music-replace-ended-{}.wav",
            std::process::id()
        ));
        std::fs::write(&path, tone_wav(0.5)).unwrap();
        let mut player = open_null();
        player.load(&path.to_string_lossy()).unwrap();
        wait_until(&mut player, |state| state.phase == PlaybackPhase::Playing);
        let deadline = Instant::now() + Duration::from_secs(5);
        while !flag(player.mpv.as_ref().unwrap(), "idle-active").unwrap_or(false) {
            assert!(Instant::now() < deadline, "file did not finish");
            std::thread::sleep(Duration::from_millis(10));
        }
        // EOF is now pending, but an explicit replacement must win over it.
        player.load(&path.to_string_lossy()).unwrap();
        player.set_paused(true);
        wait_until(&mut player, |state| state.phase == PlaybackPhase::Paused);
        assert!(!player.take_natural_end());
        player.stop();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn paused_restore_beyond_end_never_advances_the_queue() {
        let path = std::env::temp_dir().join(format!(
            "home-music-resume-ended-{}.wav",
            std::process::id()
        ));
        std::fs::write(&path, tone_wav(0.25)).unwrap();
        let mut player = open_null();
        player.load_cookie_at(&path.to_string_lossy(), "", 10.0, true).unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            assert!(!player.take_natural_end(), "paused restore advanced queue");
            let state = player.snapshot();
            assert!(!state.playing, "{state:?}");
            if state.phase != PlaybackPhase::Loading {
                break;
            }
            assert!(Instant::now() < deadline, "restore never completed");
            std::thread::sleep(Duration::from_millis(10));
        }
        player.stop();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn restoring_at_position_is_paused_before_open_and_normal_load_resets_start() {
        let path = std::env::temp_dir().join(format!(
            "home-music-resume-{}.wav",
            std::process::id()
        ));
        std::fs::write(&path, tone_wav(4.0)).unwrap();
        let mut player = open_null();
        player.load_cookie_at(&path.to_string_lossy(), "", 1.5, true).unwrap();
        let loading = player.snapshot();
        assert_eq!(loading.phase, PlaybackPhase::Loading);
        assert!(!loading.playing);
        assert_eq!(flag(player.mpv.as_ref().unwrap(), "pause"), Some(true));
        assert_eq!(loading.duration_s, 0.0);
        let deadline = Instant::now() + Duration::from_secs(5);
        let restored = loop {
            assert!(!player.take_natural_end());
            let state = player.snapshot();
            assert!(!state.playing, "restore briefly played: {state:?}");
            if state.phase == PlaybackPhase::Paused && state.position_s > 1.0 {
                break state;
            }
            assert!(Instant::now() < deadline, "restore did not open: {state:?}");
            std::thread::sleep(Duration::from_millis(10));
        };
        assert!((restored.position_s - 1.5).abs() < 0.15, "{restored:?}");
        std::thread::sleep(Duration::from_millis(200));
        assert!((player.snapshot().position_s - restored.position_s).abs() < 0.01);
        player.load(&path.to_string_lossy()).unwrap();
        let started = wait_until(&mut player, |state| state.phase == PlaybackPhase::Playing);
        assert!(started.position_s < 0.5, "{started:?}");
        assert_eq!(flag(player.mpv.as_ref().unwrap(), "pause"), Some(false));
        player.stop();
        std::fs::remove_file(path).unwrap();
    }

    fn serve_wav(body: Vec<u8>) -> String {
        serve_response(body, "200 OK")
    }

    fn serve_response(body: Vec<u8>, response_status: &'static str) -> String {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();
        std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(8);
            while Instant::now() < deadline {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        let _ = stream.set_nonblocking(false);
                        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                        let mut buf = [0u8; 4096];
                        let n = stream.read(&mut buf).unwrap_or(0);
                        let req = String::from_utf8_lossy(&buf[..n]);
                        let mut start = 0usize;
                        let mut end = body.len();
                        let mut partial = false;
                        if let Some(line) = req.lines().find(|line| {
                            response_status == "200 OK"
                                && line.to_ascii_lowercase().starts_with("range:")
                        })
                        {
                            if let Some(spec) = line.split('=').nth(1) {
                                if let Some((left, right)) = spec.trim().split_once('-') {
                                    if let Ok(value) = left.trim().parse::<usize>() {
                                        start = value.min(body.len());
                                        partial = true;
                                    }
                                    if let Ok(value) = right.trim().parse::<usize>() {
                                        end = (value + 1).min(body.len());
                                    }
                                }
                            }
                        }
                        if start > end {
                            start = end;
                        }
                        let chunk = &body[start..end];
                        let status = if partial {
                            "206 Partial Content"
                        } else {
                            response_status
                        };
                        let range_header = if partial {
                            format!(
                                "Content-Range: bytes {}-{}/{}\r\n",
                                start,
                                end.saturating_sub(1),
                                body.len()
                            )
                        } else {
                            String::new()
                        };
                        let header = format!(
                            "HTTP/1.1 {status}\r\nContent-Type: audio/wav\r\nContent-Length: {}\r\nAccept-Ranges: bytes\r\n{range_header}Connection: close\r\n\r\n",
                            chunk.len()
                        );
                        let _ = stream.write_all(header.as_bytes());
                        let _ = stream.write_all(chunk);
                    }
                    Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(15));
                    }
                    Err(_) => break,
                }
            }
        });
        format!("http://{addr}/tone.wav")
    }
}
