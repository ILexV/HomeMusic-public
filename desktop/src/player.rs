//! Playback through libmpv. The window polls `snapshot`; nothing here advances a timer.

use std::ffi::c_char;

use libmpv2::Mpv;

use crate::eq::{self, Settings};

#[derive(Clone, Debug)]
pub struct PlaybackState {
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
            init.set_option("msg-level", "all=error")?;
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
                user_stopped: false,
            },
        }
    }

    /// Status line for the window. The Navidrome line is used when mpv is quiet.
    pub fn status_or(&self, fallback: &str) -> String {
        self.notice.clone().unwrap_or_else(|| fallback.to_string())
    }

    pub fn load(&mut self, target: &str) -> Result<(), String> {
        if self.mpv.is_none() {
            let err = self
                .notice
                .clone()
                .unwrap_or_else(|| "Звук: libmpv недоступен".into());
            self.notice = Some(err.clone());
            return Err(err);
        }
        // Drop the previous file's end event before the new file starts.
        self.discard_events();
        self.user_stopped = false;
        let loaded = {
            let mpv = self.mpv.as_ref().unwrap();
            let _ = mpv.set_property("pause", false);
            mpv.command("loadfile", &[target, "replace"])
        };
        if let Err(err) = loaded {
            let text = format!("Не удалось открыть файл ({err})");
            self.notice = Some(text.clone());
            return Err(text);
        }
        if let Some(mpv) = self.mpv.as_ref() {
            let _ = mpv.set_property("pause", false);
        }
        self.notice = None;
        self.apply_eq();
        Ok(())
    }

    pub fn is_idle(&self) -> bool {
        let Some(mpv) = self.mpv.as_ref() else {
            return true;
        };
        flag(mpv, "idle-active").unwrap_or(true)
    }

    pub fn toggle(&mut self) {
        let Some(mpv) = self.mpv.as_ref() else {
            return;
        };
        // Nothing loaded, or the file already ended: play does not invent a track.
        if flag(mpv, "idle-active").unwrap_or(true) {
            return;
        }
        let paused = flag(mpv, "pause").unwrap_or(true);
        let _ = mpv.set_property("pause", !paused);
    }

    pub fn stop(&mut self) {
        // Mark this before the command, so an end event already in the queue is not a new track.
        self.user_stopped = true;
        let Some(mpv) = self.mpv.as_ref() else {
            return;
        };
        let _ = mpv.command("stop", &[]);
        let _ = mpv.set_property("pause", true);
    }

    /// Drain the event queue. True once when the file played through to the end.
    /// Stop, quit, and an end that arrives together with them do not count.
    pub fn take_natural_end(&mut self) -> bool {
        let mut eof = false;
        let mut user_stopped = self.user_stopped;
        if let Some(mpv) = self.mpv.as_ref() {
            for _ in 0..64 {
                match mpv.wait_event(0.0) {
                    None => break,
                    Some(Ok(event)) => match event {
                        libmpv2::events::Event::StartFile | libmpv2::events::Event::FileLoaded => {
                            user_stopped = false;
                            eof = false;
                        }
                        libmpv2::events::Event::EndFile(reason) => {
                            let stopped = reason == libmpv2::mpv_end_file_reason::Stop
                                || reason == libmpv2::mpv_end_file_reason::Quit;
                            if stopped {
                                user_stopped = true;
                                eof = false;
                            } else if reason == libmpv2::mpv_end_file_reason::Eof && !user_stopped {
                                eof = true;
                            }
                        }
                        _ => {}
                    },
                    Some(Err(_)) => {}
                }
            }
        } else {
            return false;
        }
        if eof {
            user_stopped = true;
        }
        self.user_stopped = user_stopped;
        eof
    }

    pub fn seek_ratio(&mut self, ratio: f32) {
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
        let Some(mpv) = self.mpv.as_ref() else {
            return PlaybackState {
                playing: false,
                position_s: 0.0,
                duration_s: 0.0,
                volume: self.volume,
            };
        };
        let idle = flag(mpv, "idle-active").unwrap_or(true);
        let paused = flag(mpv, "pause").unwrap_or(true);
        let duration_s = if idle {
            0.0
        } else {
            prop_f64(mpv, "duration").unwrap_or(0.0) as f32
        };
        let position_s = if idle || duration_s <= 0.0 {
            0.0
        } else {
            prop_f64(mpv, "time-pos").unwrap_or(0.0) as f32
        };
        PlaybackState {
            playing: !idle && !paused && duration_s > 0.0,
            position_s,
            duration_s,
            volume: self.volume_now(mpv),
        }
    }

    fn discard_events(&self) {
        let Some(mpv) = self.mpv.as_ref() else {
            return;
        };
        for _ in 0..64 {
            if mpv.wait_event(0.0).is_none() {
                break;
            }
        }
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

    fn wait_until(player: &Player, pred: impl Fn(&PlaybackState) -> bool) -> PlaybackState {
        let start = Instant::now();
        loop {
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
        let _ = wait_until(&player, |state| state.playing && state.duration_s > 0.4);
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
            let state = wait_until(&player, |state| state.duration_s > 1.0 && state.playing);
            assert!(
                state.duration_s < 2.2,
                "{name} duration {}",
                state.duration_s
            );
            player.seek_ratio(0.5);
            let half = state.duration_s * 0.5;
            wait_until(&player, |state| (state.position_s - half).abs() < 0.4);
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
        let state = wait_until(&player, |state| {
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
        let state = wait_until(&player, |state| {
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
        wait_until(&player, |state| state.playing && state.position_s > 0.05);
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
