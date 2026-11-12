//! Playback through libmpv. The window polls `snapshot`; nothing here advances a timer.

use std::ffi::c_char;

use libmpv2::Mpv;

use crate::eq::{self, Settings};

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
    /// True after a filter graph was installed, so a later flat curve can clear it.
    filter_active: bool,
    notice: Option<String>,
    phase: PlaybackPhase,
    /// Ignore the replaced file's trailing events until the next StartFile.
    awaiting_start: bool,
    /// Set by stop and by a natural end that was already handled.
    /// A following end-of-file event must not advance the queue.
    user_stopped: bool,
}

impl Player {
    pub fn open(options: PlayerOptions) -> Self {
        ensure_c_numeric();
        let opened = Mpv::with_initializer(|init| {
            init.set_option("idle", "yes")?;
            init.set_option("vid", "no")?;
            init.set_option("audio-display", "no")?;
            init.set_option("ytdl", "no")?;
            // Keep the stream URL, which carries the Subsonic token, out of the log.
            init.set_option("msg-level", "all=no")?;
            if options.null_audio {
                init.set_option("ao", "null")?;
            }
            Ok(())
        });
        match opened {
            Ok(mpv) => {
                let player = Self {
                    mpv: Some(mpv),
                    volume: 0.8,
                    eq: Settings::default(),
                    filter_active: false,
                    notice: None,
                    phase: PlaybackPhase::Idle,
                    awaiting_start: false,
                    user_stopped: false,
                };
                player.apply_volume();
                player
            }
            Err(err) => Self {
                mpv: None,
                volume: 0.8,
                eq: Settings::default(),
                filter_active: false,
                notice: Some(open_error(&err)),
                phase: PlaybackPhase::Error,
                awaiting_start: false,
                user_stopped: false,
            },
        }
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
    }

    pub fn stop(&mut self) {
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

    /// Drain asynchronous load and playback events before taking a snapshot.
    /// Return true exactly once for EOF, never for stop, replacement or errors.
    pub fn take_natural_end(&mut self) -> bool {
        use libmpv2::events::Event;
        use libmpv2::mpv_end_file_reason as reason;

        let Some(mpv) = self.mpv.as_ref() else {
            return false;
        };
        let mut eof = false;
        while let Some(event) = mpv.wait_event(0.0) {
            // Stop is authoritative even when StartFile/FileLoaded were queued.
            if self.user_stopped {
                continue;
            }
            match event {
                Ok(Event::StartFile) => {
                    self.awaiting_start = false;
                    self.phase = PlaybackPhase::Loading;
                    eof = false;
                }
                _ if self.awaiting_start => {}
                Ok(Event::FileLoaded) => {
                    self.phase = PlaybackPhase::Playing;
                    self.notice = None;
                    eof = false;
                }
                Ok(Event::EndFile(reason::Eof)) => {
                    self.phase = PlaybackPhase::Ended;
                    // Restoring an old position beyond a shortened file must not
                    // advance into an audible next track while pause is requested.
                    eof = !flag(mpv, "pause").unwrap_or(true);
                }
                Ok(Event::EndFile(reason::Stop | reason::Quit)) => {
                    self.phase = PlaybackPhase::Stopped;
                    eof = false;
                }
                Ok(Event::EndFile(reason::Redirect)) => {
                    self.phase = PlaybackPhase::Loading;
                    self.awaiting_start = true;
                    eof = false;
                }
                // libmpv2 converts EndFile.error into Err, not EndFile(Error).
                // Neither errors nor endpoint log messages are safe UI text.
                Err(_) | Ok(Event::EndFile(reason::Error) | Event::QueueOverflow) => {
                    self.phase = PlaybackPhase::Error;
                    self.notice = Some("Не удалось воспроизвести файл".into());
                    eof = false;
                }
                Ok(Event::Shutdown) => {
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
        eof
    }

    pub fn seek_ratio(&mut self, ratio: f32) {
        if !self.snapshot().seekable || !ratio.is_finite() {
            return;
        }
        let Some(mpv) = self.mpv.as_ref() else {
            return;
        };
        let Some(duration) = prop_f64(mpv, "duration") else {
            return;
        };
        if duration <= 0.0 {
            return;
        }
        let ratio = f64::from(ratio.clamp(0.0, 1.0));
        let target = format!("{:.3}", ratio * duration);
        let _ = mpv.command("seek", &[&target, "absolute"]);
    }

    pub fn set_volume(&mut self, volume: f32) {
        self.volume = volume.clamp(0.0, 1.0);
        self.apply_volume();
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
            seekable: false,
            playing: false,
            position_s: 0.0,
            duration_s: 0.0,
            volume: self.volume,
        };
        let Some(mpv) = self.mpv.as_ref() else {
            return state;
        };
        state.volume = self.volume_now(mpv);
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
            let _ = mpv.set_property("volume", f64::from(self.volume) * 100.0);
        }
    }

    fn volume_now(&self, mpv: &Mpv) -> f32 {
        match prop_f64(mpv, "volume") {
            Some(value) => (value / 100.0).clamp(0.0, 1.0) as f32,
            None => self.volume,
        }
    }

    fn apply_eq(&mut self) {
        let Some(mpv) = self.mpv.as_ref() else {
            return;
        };
        match eq::chain(self.eq) {
            Some(filter) => {
                if mpv.command("af", &["set", &filter]).is_ok()
                    || mpv.set_property("af", filter.as_str()).is_ok()
                {
                    self.filter_active = true;
                }
            }
            None if self.filter_active => {
                if mpv.command("af", &["clr"]).is_ok() || mpv.set_property("af", "").is_ok() {
                    self.filter_active = false;
                }
            }
            None => {}
        }
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

    fn tone_wav(seconds: f32) -> Vec<u8> {
        let rate = 44_100u32;
        let samples = (rate as f32 * seconds) as u32;
        let data_bytes = samples * 2;
        let mut bytes = Vec::with_capacity(44 + data_bytes as usize);
        bytes.extend(b"RIFF");
        bytes.extend((36 + data_bytes).to_le_bytes());
        bytes.extend(b"WAVEfmt ");
        bytes.extend(16u32.to_le_bytes());
        bytes.extend(1u16.to_le_bytes());
        bytes.extend(1u16.to_le_bytes());
        bytes.extend(rate.to_le_bytes());
        bytes.extend((rate * 2).to_le_bytes());
        bytes.extend(2u16.to_le_bytes());
        bytes.extend(16u16.to_le_bytes());
        bytes.extend(b"data");
        bytes.extend(data_bytes.to_le_bytes());
        for i in 0..samples {
            let t = i as f32 / rate as f32;
            let sample = (t * 440.0 * std::f32::consts::TAU).sin();
            bytes.extend(((sample * 8_000.0) as i16).to_le_bytes());
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
    fn missing_media_reports_error_without_advancing_and_recovers() {
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
        assert_eq!(player.notice(), Some("Не удалось воспроизвести файл"));

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
    fn malformed_http_media_does_not_expose_url_or_cookie() {
        let url = format!("{}?token=private-token", serve_wav(b"not audio".to_vec()));
        let mut player = open_null();
        player.load_cookie(&url, "private-session").unwrap();
        wait_until(&mut player, |state| state.phase == PlaybackPhase::Error);
        assert_eq!(player.notice(), Some("Не удалось воспроизвести файл"));
        assert!(!player.take_natural_end());
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
                        if let Some(line) = req
                            .lines()
                            .find(|line| line.to_ascii_lowercase().starts_with("range:"))
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
                            "200 OK"
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
