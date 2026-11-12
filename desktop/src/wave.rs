//! A detailed energy envelope for the seek bar. Playback stays in libmpv.

use std::io::{Cursor, Read, Seek, SeekFrom};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use symphonia::core::audio::{AudioBufferRef, SampleBuffer};
use symphonia::core::codecs::DecoderOptions;
use symphonia::core::formats::FormatOptions;
use symphonia::core::io::{MediaSource, MediaSourceStream};
use symphonia::core::meta::MetadataOptions;
use symphonia::core::probe::Hint;

const BARS: usize = 4096;
const HTTP_CAP: u64 = 20 * 1024 * 1024;
const FILE_CAP: u64 = 64 * 1024 * 1024;
const CHUNK: usize = 256;

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
            let bytes = read_limited(&target, &cookie);
            if hub.ticket.load(Ordering::Relaxed) != ticket {
                return;
            }
            let update = match bytes.and_then(peaks_from_bytes) {
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
        let current = self.ticket.load(Ordering::Relaxed);
        let mut slot = self.pending.lock().ok()?;
        match slot.take() {
            Some((ticket, update)) if ticket == current => Some(update),
            _ => None,
        }
    }
}


fn read_limited(target: &str, cookie: &str) -> Option<Vec<u8>> {
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
        let mut bytes = Vec::new();
        response
            .into_reader()
            .take(HTTP_CAP)
            .read_to_end(&mut bytes)
            .ok()?;
        if bytes.is_empty() {
            None
        } else {
            Some(bytes)
        }
    } else {
        let file = std::fs::File::open(target).ok()?;
        let mut bytes = Vec::new();
        file.take(FILE_CAP).read_to_end(&mut bytes).ok()?;
        if bytes.is_empty() {
            None
        } else {
            Some(bytes)
        }
    }
}

fn id3_audio_offset(bytes: &[u8]) -> usize {
    let mut offset = 0;
    while let Some(header) = bytes.get(offset..offset + 10) {
        if &header[..3] != b"ID3" || !(2..=4).contains(&header[3])
            || header[4] == 0xff || header[6..10].iter().any(|byte| byte & 0x80 != 0)
        {
            break;
        }
        let size = header[6..10].iter().fold(0usize, |size, byte| (size << 7) | usize::from(*byte));
        let footer = if header[3] == 4 && header[5] & 0x10 != 0 { 10 } else { 0 };
        let Some(end) = offset.checked_add(10 + size + footer) else { break };
        if end > bytes.len() {
            break;
        }
        offset = end;
    }
    offset
}

pub fn peaks_from_bytes(bytes: Vec<u8>) -> Option<Vec<f32>> {
    // The envelope needs no tags or embedded pictures. Skip bounded ID3v2 blocks:
    // Symphonia 0.5 rejects the valid empty ID3 tag used by some MP3 previews.
    let start = id3_audio_offset(&bytes);
    let mut cursor = Cursor::new(bytes);
    cursor.set_position(start as u64);
    let source = OwnedBytes { inner: cursor };
    let stream = MediaSourceStream::new(Box::new(source), Default::default());
    let probed = symphonia::default::get_probe()
        .format(
            &Hint::new(),
            stream,
            &FormatOptions::default(),
            &MetadataOptions::default(),
        )
        .ok()?;
    let mut format = probed.format;
    let (track_id, params) = {
        let track = format.default_track()?;
        (track.id, track.codec_params.clone())
    };
    let mut decoder = symphonia::default::get_codecs()
        .make(&params, &DecoderOptions::default())
        .ok()?;

    let capacity = params.n_frames.map(|frames| frames.div_ceil(CHUNK as u64).min(200_001) as usize).unwrap_or(0);
    let mut chunk_peaks = Vec::with_capacity(capacity);
    let mut chunk_energy = 0.0f64;
    let mut chunk_n = 0usize;
    let mut samples = None;
    loop {
        let packet = match format.next_packet() {
            Ok(packet) => packet,
            Err(_) => break,
        };
        if packet.track_id() != track_id {
            continue;
        }
        let decoded = match decoder.decode(&packet) {
            Ok(decoded) => decoded,
            Err(_) => continue,
        };
        push_buffer(decoded, &mut samples, &mut chunk_peaks, &mut chunk_energy, &mut chunk_n);
        if chunk_peaks.len() > 200_000 {
            break;
        }
    }
    if chunk_n > 0 {
        chunk_peaks.push((chunk_energy / chunk_n as f64).sqrt() as f32);
    }
    let peaks = peaks_from_samples(&chunk_peaks, BARS);
    if peaks.is_empty() {
        None
    } else {
        Some(peaks)
    }
}

fn push_buffer(
    decoded: AudioBufferRef<'_>,
    buffer: &mut Option<SampleBuffer<f32>>,
    chunk_peaks: &mut Vec<f32>,
    chunk_energy: &mut f64,
    chunk_n: &mut usize,
) {
    let spec = *decoded.spec();
    let channels = spec.channels.count().max(1);
    if buffer.as_ref().is_none_or(|samples| samples.capacity() < decoded.capacity() * channels) {
        *buffer = Some(SampleBuffer::new(decoded.capacity() as u64, spec));
    }
    let samples = buffer.as_mut().expect("sample buffer initialized");
    samples.copy_interleaved_ref(decoded);
    let channel_scale = 1.0 / channels as f64;
    for frame in samples.samples().chunks(channels) {
        // Energy, not a maximum: isolated clipped peaks must not flatten a whole interval.
        // Squaring each channel separately also preserves anti-phase stereo.
        let energy = frame.iter().map(|sample| f64::from(*sample).powi(2)).sum::<f64>();
        *chunk_energy += energy * channel_scale;
        *chunk_n += 1;
        if *chunk_n >= CHUNK {
            chunk_peaks.push((*chunk_energy / *chunk_n as f64).sqrt() as f32);
            *chunk_energy = 0.0;
            *chunk_n = 0;
        }
    }
}

/// RMS envelope at a fixed resolution, normalized once per track. Silence stays empty.
pub fn peaks_from_samples(samples: &[f32], bars: usize) -> Vec<f32> {
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

struct OwnedBytes {
    inner: Cursor<Vec<u8>>,
}

impl Read for OwnedBytes {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        self.inner.read(buffer)
    }
}

impl Seek for OwnedBytes {
    fn seek(&mut self, position: SeekFrom) -> std::io::Result<u64> {
        self.inner.seek(position)
    }
}

impl MediaSource for OwnedBytes {
    fn is_seekable(&self) -> bool {
        true
    }

    fn byte_len(&self) -> Option<u64> {
        Some(self.inner.get_ref().len() as u64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
