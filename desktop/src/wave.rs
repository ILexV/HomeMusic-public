//! Peaks for the seek bar. Playback stays in libmpv; this only reads samples.

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

pub const BARS: usize = 48;
const HTTP_CAP: u64 = 20 * 1024 * 1024;
const FILE_CAP: u64 = 64 * 1024 * 1024;
const CHUNK: usize = 256;

pub struct WaveHub {
    ticket: AtomicU64,
    pending: Mutex<Option<(u64, Vec<f32>)>>,
}

impl WaveHub {
    pub fn shared() -> Arc<Self> {
        Arc::new(Self {
            ticket: AtomicU64::new(0),
            pending: Mutex::new(None),
        })
    }

    /// Drop any wave already queued and start a new one. An empty target clears the bar.
    pub fn load(self: &Arc<Self>, target: Option<String>) {
        let ticket = self.ticket.fetch_add(1, Ordering::Relaxed).wrapping_add(1);
        if let Ok(mut slot) = self.pending.lock() {
            *slot = None;
        }
        let Some(target) = target.filter(|target| !target.is_empty()) else {
            return;
        };
        let hub = Arc::clone(self);
        std::thread::spawn(move || {
            let peaks = load_peaks(&target).unwrap_or_default();
            if hub.ticket.load(Ordering::Relaxed) != ticket || peaks.is_empty() {
                return;
            }
            if let Ok(mut slot) = hub.pending.lock() {
                if hub.ticket.load(Ordering::Relaxed) == ticket {
                    *slot = Some((ticket, peaks));
                }
            }
        });
    }

    pub fn poll(&self) -> Option<Vec<f32>> {
        let current = self.ticket.load(Ordering::Relaxed);
        let mut slot = self.pending.lock().ok()?;
        match slot.take() {
            Some((ticket, peaks)) if ticket == current && !peaks.is_empty() => Some(peaks),
            _ => None,
        }
    }
}

pub fn load_peaks(target: &str) -> Option<Vec<f32>> {
    let bytes = read_limited(target)?;
    peaks_from_bytes(&bytes)
}

fn read_limited(target: &str) -> Option<Vec<u8>> {
    if target.starts_with("http://") || target.starts_with("https://") {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(5))
            .timeout_read(Duration::from_secs(20))
            .build();
        let response = agent.get(target).call().ok()?;
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

pub fn peaks_from_bytes(bytes: &[u8]) -> Option<Vec<f32>> {
    let source = OwnedBytes {
        inner: Cursor::new(bytes.to_vec()),
    };
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

    let mut chunk_peaks = Vec::new();
    let mut chunk_max = 0.0f32;
    let mut chunk_n = 0usize;
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
        push_buffer(decoded, &mut chunk_peaks, &mut chunk_max, &mut chunk_n);
        if chunk_peaks.len() > 200_000 {
            break;
        }
    }
    if chunk_n > 0 {
        chunk_peaks.push(chunk_max);
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
    chunk_peaks: &mut Vec<f32>,
    chunk_max: &mut f32,
    chunk_n: &mut usize,
) {
    let spec = *decoded.spec();
    let channels = spec.channels.count().max(1);
    let mut samples = SampleBuffer::<f32>::new(decoded.capacity() as u64, spec);
    samples.copy_interleaved_ref(decoded);
    for frame in samples.samples().chunks(channels) {
        let amp = frame.iter().copied().map(f32::abs).fold(0.0f32, f32::max);
        *chunk_max = chunk_max.max(amp);
        *chunk_n += 1;
        if *chunk_n >= CHUNK {
            chunk_peaks.push(*chunk_max);
            *chunk_max = 0.0;
            *chunk_n = 0;
        }
    }
}

/// Peak of each bar, scaled so the loudest bar is 1. Silence stays empty and the bar stays flat.
pub fn peaks_from_samples(samples: &[f32], bars: usize) -> Vec<f32> {
    if samples.is_empty() || bars == 0 {
        return Vec::new();
    }
    let mut out = vec![0.0f32; bars];
    let count = samples.len();
    if count >= bars {
        for (index, sample) in samples.iter().enumerate() {
            let bar = index * bars / count;
            let bar = bar.min(bars - 1);
            out[bar] = out[bar].max(sample.abs());
        }
    } else {
        for (index, sample) in samples.iter().enumerate() {
            let start = index * bars / count;
            let end = ((index + 1) * bars / count).max(start + 1).min(bars);
            let amp = sample.abs();
            for bar in start..end {
                out[bar] = out[bar].max(amp);
            }
        }
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
        assert!(peaks_from_bytes(br#"{"status":"failed"}"#).is_none());
    }

    #[test]
    fn a_wav_keeps_the_loud_half() {
        let mut samples = Vec::with_capacity(4800);
        samples.extend(std::iter::repeat(20_000i16).take(2400));
        samples.extend(std::iter::repeat(2_000i16).take(2400));
        let peaks = peaks_from_bytes(&wav_pcm16(&samples, 48_000)).expect("wav");
        assert!(peaks[0] > peaks[peaks.len() - 1] * 2.0);
        let loud: f32 = peaks[..peaks.len() / 2].iter().sum();
        let quiet: f32 = peaks[peaks.len() / 2..].iter().sum();
        assert!(loud > quiet);
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
