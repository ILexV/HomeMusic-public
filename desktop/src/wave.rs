//! The seek bar uses the same native decoders as playback, on a silent background player.

use std::io::{BufRead, BufReader, Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use libmpv2::{events::Event, mpv_end_file_reason, Mpv};
use tempfile::NamedTempFile;

const BARS: usize = 4096;
const CHUNK: usize = 256;
const ANALYSIS_TIMEOUT: Duration = Duration::from_secs(180);
// Bound scratch disk usage. Never present a prefix as the complete track.
const MAX_DOWNLOAD: u64 = 1024 * 1024 * 1024;

pub enum Update {
    Cleared,
    Loading,
    Ready(Vec<f32>),
    Unavailable,
}

pub struct WaveHub {
    ticket: AtomicU64,
    pending: Mutex<Option<(u64, Update)>>,
}

impl WaveHub {
    pub fn shared() -> Arc<Self> {
        Arc::new(Self {
            ticket: AtomicU64::new(0),
            pending: Mutex::new(None),
        })
    }

    /// Drop any wave already queued and start a new one. An empty target clears the bar.
    pub fn load(self: &Arc<Self>, target: Option<String>, cookie: &str) {
        let ticket = self.ticket.fetch_add(1, Ordering::Relaxed).wrapping_add(1);
        let target = target.filter(|target| !target.is_empty());
        if let Ok(mut slot) = self.pending.lock() {
            *slot = Some((ticket, if target.is_some() { Update::Loading } else { Update::Cleared }));
        }
        let Some(target) = target else { return };
        let hub = Arc::clone(self);
        let cookie = cookie.to_owned();
        std::thread::spawn(move || {
            let current = || hub.ticket.load(Ordering::Relaxed) == ticket;
            let update = match analyze(&target, &cookie, current) {
                Some(peaks) => Update::Ready(peaks),
                None => Update::Unavailable,
            };
            if let Ok(mut slot) = hub.pending.lock() {
                if hub.ticket.load(Ordering::Relaxed) == ticket {
                    *slot = Some((ticket, update));
                }
            }
        });
    }

    pub fn poll(&self) -> Option<Update> {
        let mut slot = self.pending.lock().ok()?;
        let current = self.ticket.load(Ordering::Relaxed);
        match slot.take() {
            Some((ticket, update)) if ticket == current => Some(update),
            _ => None,
        }
    }
}

fn analyze(target: &str, cookie: &str, current: impl Fn() -> bool) -> Option<Vec<f32>> {
    let deadline = std::time::Instant::now() + ANALYSIS_TIMEOUT;
    let active = || current() && std::time::Instant::now() < deadline;
    if !active() { return None; }
    if target.starts_with("http://") || target.starts_with("https://") {
        let mut builder = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(5))
            .timeout_read(Duration::from_secs(if cookie.is_empty() { 20 } else { 120 }));
        if !cookie.is_empty() {
            // Hub streams are proxied, not redirected. Never forward their session to another host.
            builder = builder.redirects(0);
        }
        let agent = builder.build();
        let mut request = agent.get(target);
        if !cookie.is_empty() {
            request = request
                .set("Cookie", &format!("hm_session={cookie}"))
                // Reading the waveform is not another listen or an economy-import download.
                .set("X-HomeMusic-Prefetch", "1");
        }
        let response = request.call().ok()?;
        if response.status() != 200 { return None; }
        let expected = response.header("Content-Length").and_then(|value| value.parse::<u64>().ok());
        // Remote MP4/M4A can keep their sample tables after the audio. Spool the
        // whole response to a private seekable file, not a truncated RAM prefix.
        let mut audio = NamedTempFile::new().ok()?;
        copy_complete(response.into_reader(), &mut audio, expected, MAX_DOWNLOAD, &active)?;
        analyze_file(audio.path(), active)
    } else {
        analyze_file(Path::new(target), active)
    }
}

fn copy_complete(
    mut source: impl Read, mut output: impl Write, expected: Option<u64>, limit: u64,
    active: impl Fn() -> bool,
) -> Option<()> {
    if expected.is_some_and(|bytes| bytes > limit) { return None; }
    let mut received = 0u64;
    let mut buffer = [0u8; 64 * 1024];
    while active() {
        let count = source.read(&mut buffer).ok()?;
        if count == 0 {
            return (received > 0 && expected.is_none_or(|size| received == size)).then_some(());
        }
        received += count as u64;
        if received > expected.unwrap_or(limit) { return None; }
        output.write_all(&buffer[..count]).ok()?;
    }
    None
}

// libavfilter parses a graph and then parses each filter's options. Quote a
// literal at both levels, including Windows drive letters and user-directory names.
fn av_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn analyze_file(path: &Path, active: impl Fn() -> bool) -> Option<Vec<f32>> {
    if !path.is_file() || !active() { return None; }
    let levels = NamedTempFile::new().ok()?.into_temp_path();
    let file = av_quote(&av_quote(levels.to_str()?));
    let graph = format!(
        "asetnsamples=n={CHUNK}:p=0,astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file={file}"
    );
    // Pass the graph verbatim through mpv's filter-option parser.
    let filter = format!("lavfi=graph=%{}%{graph}", graph.len());
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
        init.set_option("af", filter.as_str())?;
        Ok(())
    }).ok()?;
    mpv.command("loadfile", &[path.to_str()?, "replace"]).ok()?;
    loop {
        if !active() { return None; }
        match mpv.wait_event(0.025) {
            Some(Ok(Event::EndFile(mpv_end_file_reason::Eof))) => break,
            Some(Ok(Event::EndFile(_) | Event::Shutdown | Event::QueueOverflow)) | Some(Err(_)) => return None,
            _ => {}
        }
    }
    drop(mpv); // Flush the last filter frame and release its output file.
    let mut reader = BufReader::new(std::fs::File::open(&levels).ok()?);
    let mut line = String::new();
    let mut rms = Vec::new();
    while active() {
        line.clear();
        if reader.read_line(&mut line).ok()? == 0 {
            let peaks = peaks_from_samples(&rms, BARS);
            return (!peaks.is_empty()).then_some(peaks);
        }
        if let Some(db) = line.strip_prefix("lavfi.astats.Overall.RMS_level=") {
            let amplitude = 10f32.powf(db.trim_end().parse::<f32>().ok()? / 20.0);
            if !amplitude.is_finite() { return None; }
            rms.push(amplitude);
        }
    }
    None
}

/// RMS envelope at a fixed resolution, normalized once per track. Silence stays empty.
fn peaks_from_samples(samples: &[f32], bars: usize) -> Vec<f32> {
    if samples.is_empty() || bars == 0 {
        return Vec::new();
    }
    let mut out = vec![0.0f32; bars];
    let count = samples.len();
    for (bar, value) in out.iter_mut().enumerate() {
        let start = bar * count / bars;
        let end = ((bar + 1) * count / bars).max(start + 1).min(count);
        let energy = samples[start..end].iter().map(|sample| f64::from(*sample).powi(2)).sum::<f64>();
        *value = (energy / (end - start) as f64).sqrt() as f32;
    }
    let max = out.iter().copied().fold(0.0f32, f32::max);
    if max <= 0.0 {
        return Vec::new();
    }
    for value in &mut out {
        *value /= max;
    }
    out
}


#[cfg(test)]
mod tests {
    use super::*;

    fn peaks_from_bytes(bytes: Vec<u8>) -> Option<Vec<f32>> {
        let mut media = NamedTempFile::new().unwrap();
        media.write_all(&bytes).unwrap();
        analyze(media.path().to_str().unwrap(), "", || true)
    }

    #[test]
    fn opus_wave_reaches_the_rendered_seek_bar_in_each_layout() {
        use slint::{ComponentHandle, PhysicalSize};
        use slint::platform::{self, Platform, WindowAdapter};
        use slint::platform::software_renderer::{MinimalSoftwareWindow, RepaintBufferType};
        use std::rc::Rc;

        struct Offscreen;
        impl Platform for Offscreen {
            fn create_window_adapter(&self) -> Result<Rc<dyn WindowAdapter>, slint::PlatformError> {
                Ok(MinimalSoftwareWindow::new(RepaintBufferType::NewBuffer))
            }
        }
        platform::set_platform(Box::new(Offscreen)).unwrap();
        let ui = crate::AppWindow::new().unwrap();
        ui.show().unwrap();
        let waves = WaveHub::shared();
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/testdata/tone.opus");
        waves.load(Some(path.into()), "");
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        let peaks = loop {
            match waves.poll() {
                Some(Update::Ready(peaks)) => break peaks,
                Some(Update::Unavailable) => panic!("Opus produced no envelope"),
                _ => {}
            }
            assert!(std::time::Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        };
        assert_eq!(peaks.len(), BARS);
        for (mode, width, height) in [
            (crate::Mode::Mini, 600, 180),
            (crate::Mode::Medium, 540, 700),
            (crate::Mode::Large, 1200, 800),
        ] {
            ui.set_mode(mode);
            ui.window().set_size(PhysicalSize::new(width, height));
            ui.set_wave(crate::empty_wave());
            let empty = ui.window().take_snapshot().unwrap();
            crate::show_wave(&ui, Update::Ready(peaks.clone()));
            let rendered = ui.window().take_snapshot().unwrap();
            // Count newly painted unplayed waveform pixels, not a label or cursor.
            let columns = rendered.as_slice().iter().zip(empty.as_slice()).filter(|(new, old)|
                new != old && [new.r, new.g, new.b] == [0x7a; 3]
            ).count();
            assert!(columns > 100, "no waveform visible in {mode:?}: {columns} pixels");
        }
    }

    #[test]
    fn remote_analysis_requires_a_complete_response_within_budget() {
        let transfer = |expected, limit| {
            let mut data = Vec::new();
            copy_complete(b"abcd".as_slice(), &mut data, expected, limit, || true)?;
            Some(data)
        };
        assert_eq!(transfer(None, 4).unwrap(), b"abcd");
        assert_eq!(transfer(Some(4), 4).unwrap(), b"abcd");
        assert!(transfer(None, 3).is_none());     // chunked body over budget
        assert!(transfer(Some(4), 3).is_none());  // too large before first read
        assert!(transfer(Some(3), 4).is_none());  // advertised too little
        assert!(transfer(Some(5), 6).is_none());  // interrupted transfer
    }

    #[test]
    fn large_m4a_reads_the_sample_table_after_the_audio() {
        use std::io::{Seek, SeekFrom};
        let bytes = include_bytes!("../testdata/tone.m4a");
        // The fixture stores mdat before moov. Insert a valid sparse free atom
        // between them, without moving any audio samples or their chunk offsets.
        let table = bytes.windows(4).rposition(|tag| tag == b"moov").unwrap() - 4;
        let padding = 65 * 1024 * 1024u32;
        let mut media = NamedTempFile::new().unwrap();
        media.write_all(&bytes[..table]).unwrap();
        media.write_all(&padding.to_be_bytes()).unwrap();
        media.write_all(b"free").unwrap();
        media.seek(SeekFrom::Current(i64::from(padding - 8))).unwrap();
        media.write_all(&bytes[table..]).unwrap();
        let peaks = analyze(media.path().to_str().unwrap(), "", || true).expect("complete M4A");
        assert_eq!(peaks.len(), BARS);
    }

    #[test]
    fn loud_half_is_taller_than_the_quiet_half() {
        let mut samples = vec![0.2f32; 96];
        for sample in &mut samples[..48] {
            *sample = 1.0;
        }
        let peaks = peaks_from_samples(&samples, BARS);
        assert_eq!(peaks.len(), BARS);
        assert!(peaks[0] > 0.9, "loud bar {}", peaks[0]);
        assert!(peaks[BARS - 1] < 0.4, "quiet bar {}", peaks[BARS - 1]);
        let loud: f32 = peaks[..BARS / 2].iter().sum();
        let quiet: f32 = peaks[BARS / 2..].iter().sum();
        assert!(loud > quiet * 2.0, "loud {loud} quiet {quiet}");
    }

    #[test]
    fn silence_leaves_the_bar_empty() {
        assert!(peaks_from_samples(&[], BARS).is_empty());
        assert!(peaks_from_samples(&[0.0, 0.0, 0.0], BARS).is_empty());
        assert!(peaks_from_bytes(br#"{"status":"failed"}"#.to_vec()).is_none());
    }

    #[test]
    fn a_wav_keeps_the_loud_half() {
        let mut samples = Vec::with_capacity(4800);
        samples.extend(std::iter::repeat(20_000i16).take(2400));
        samples.extend(std::iter::repeat(2_000i16).take(2400));
        let peaks = peaks_from_bytes(wav_pcm16(&samples, 48_000)).expect("wav");
        assert!(peaks[0] > peaks[peaks.len() - 1] * 2.0);
        let loud: f32 = peaks[..peaks.len() / 2].iter().sum();
        let quiet: f32 = peaks[peaks.len() / 2..].iter().sum();
        assert!(loud > quiet);
    }

    #[test]
    fn equal_peak_transients_do_not_flatten_different_energy_levels() {
        let mut samples = vec![3_000i16; 64 * CHUNK];
        samples[32 * CHUNK..].fill(12_000);
        for chunk in samples.chunks_mut(CHUNK) {
            chunk[0] = 30_000;
        }
        let envelope = peaks_from_bytes(wav_pcm16(&samples, 48_000)).expect("wav");
        let quiet = envelope[..envelope.len() / 2].iter().sum::<f32>();
        let loud = envelope[envelope.len() / 2..].iter().sum::<f32>();
        assert!(quiet < loud * 0.5, "equal peaks hid the quiet section: {quiet} / {loud}");
        assert!(quiet > 0.0);
    }

    #[test]
    fn empty_id3_metadata_does_not_hide_the_audio() {
        let mut samples = vec![16_000i16; 8192];
        samples[4096..].fill(2_000);
        let mut tagged = b"ID3\x04\x00\x00\x00\x00\x00\x00".to_vec();
        tagged.extend(wav_pcm16(&samples, 48_000));
        let envelope = peaks_from_bytes(tagged).expect("audio after an empty tag");
        assert!(envelope[0] > envelope[envelope.len() - 1] * 2.0);
    }

    #[test]
    fn authenticated_stream_produces_a_wave_without_an_extra_listen() {
        use std::io::{Read, Write};
        use std::net::TcpListener;
        use std::time::Instant;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();
        let mut samples = vec![16_000i16; 8192];
        samples[4096..].fill(2_000);
        let audio = wav_pcm16(&samples, 48_000);
        let server = std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(5);
            let (mut socket, _) = loop {
                match listener.accept() {
                    Ok(connection) => break connection,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock && Instant::now() < deadline => {
                        std::thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("wave request was not received: {error}"),
                }
            };
            socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
            let mut request = Vec::new();
            let mut byte = [0];
            while !request.ends_with(b"\r\n\r\n") && request.len() < 8192 {
                socket.read_exact(&mut byte).unwrap();
                request.push(byte[0]);
            }
            let request = String::from_utf8(request).unwrap().to_ascii_lowercase();
            if !request.contains("\r\ncookie: hm_session=wave-test\r\n") {
                socket.write_all(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
                return 0;
            }
            let listens = usize::from(!request.contains("\r\nx-homemusic-prefetch: 1\r\n"));
            write!(socket, "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", audio.len()).unwrap();
            socket.write_all(&audio).unwrap();
            listens
        });
        let waves = WaveHub::shared();
        waves.load(Some(format!("http://{address}/api/listen/stream?ref=dz:test")), "wave-test");
        let deadline = Instant::now() + Duration::from_secs(5);
        let envelope = loop {
            if let Some(Update::Ready(envelope)) = waves.poll() {
                break envelope;
            }
            assert!(Instant::now() < deadline, "authenticated waveform did not arrive");
            std::thread::sleep(Duration::from_millis(5));
        };
        assert!(envelope[0] > envelope[envelope.len() - 1] * 2.0);
        assert_eq!(server.join().unwrap(), 0, "analysis counted as playback");
    }

    #[test]
    fn a_failed_load_does_not_leave_the_loading_message_forever() {
        let waves = WaveHub::shared();
        waves.load(Some(std::env::temp_dir().to_string_lossy().into_owned()), "");
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        loop {
            if let Some(Update::Unavailable) = waves.poll() {
                break;
            }
            assert!(std::time::Instant::now() < deadline, "wave load never finished");
            std::thread::sleep(Duration::from_millis(5));
        }
        waves.load(None, "");
        assert!(matches!(waves.poll(), Some(Update::Cleared)));
    }

    fn wav_pcm16(samples: &[i16], rate: u32) -> Vec<u8> {
        let data_bytes = (samples.len() * 2) as u32;
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
        for sample in samples {
            bytes.extend(sample.to_le_bytes());
        }
        bytes
    }
}
