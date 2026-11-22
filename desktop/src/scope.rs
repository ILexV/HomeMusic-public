//! A short, triggered time-domain trace from a post-EQ lavfi side branch.
use std::cell::RefCell;
use std::io;
use std::net::{Ipv4Addr, UdpSocket};
use std::time::{Duration, Instant};

pub(crate) const SCOPE_POINTS: usize = 96;
// 20 ms of display at 4.8 kHz, plus 30 ms in which to find a rising trigger.
pub(super) const HISTORY: usize = SCOPE_POINTS + 144;
const PACKET: usize = 1400;
const MAX_PACKETS: usize = 64;
const SAMPLE_KEY: &str = "lavfi.astats.1.DC_offset";

pub(super) struct ScopeTap {
    socket: UdpSocket,
    frames: RefCell<Frames>,
}

struct Frames {
    samples: [f32; HISTORY],
    next: usize,
    count: usize,
    last: Option<Instant>,
    // Buffered AVIO splits the two-line metadata records anywhere, including
    // within numbers. Carry only one unfinished line across packets/ticks.
    line: [u8; 128],
    len: usize,
    frame: Option<u64>,
    previous: Option<u64>,
    discarding: bool,
}

impl ScopeTap {
    pub(super) fn open() -> io::Result<(Self, String)> {
        let socket = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0))?;
        socket.set_nonblocking(true)?;
        let port = socket.local_addr()?.port();
        // Downmix and anti-alias only the private analysis branch. astats' DC
        // offset of a single normalized sample is its signed PCM value. Leave
        // AVIO buffered: each UDP datagram carries many samples, not one key.
        // mpv pulls the dry output, not an anullsink: an unconnected sink only
        // advances one analysis sample per decoder frame. Synchronize both
        // branches in 10 ms blocks, selecting the original dry frames verbatim.
        // Pad only the analysis input so its resampler cannot truncate dry EOF.
        let graph = format!(concat!(
            "@meter:lavfi=[asplit=2[dry][scope];[scope]",
            "apad=pad_dur=0.1,aformat=sample_rates=4800:sample_fmts=flt:channel_layouts=mono,",
            "asetnsamples=n=1:p=0,astats=length=0.001:metadata=1:reset=1:",
            "measure_perchannel=DC_offset:measure_overall=none,",
            "ametadata=print:key={}:file='udp\\://127.0.0.1\\:{}?pkt_size={}',",
            "asetnsamples=n=48:p=0,aresample[clock];[dry][clock]astreamselect=inputs=2:map=0]:o=[threads=1]"
        ), SAMPLE_KEY, port, PACKET);
        Ok((Self { socket, frames: RefCell::new(Frames {
            samples: [0.0; HISTORY], next: 0, count: 0, last: None, line: [0; 128],
            len: 0, frame: None, previous: None, discarding: false,
        }) }, graph))
    }

    pub(super) fn clear(&self) {
        let mut frames = self.frames.borrow_mut();
        frames.forget();
        frames.discarding = true;
        frames.receive(&self.socket);
    }

    pub(super) fn mix_into(&self, mixed: &mut [f32; HISTORY], gain: f32) {
        let mut frames = self.frames.borrow_mut();
        frames.receive(&self.socket);
        if frames.count != HISTORY || !frames.last.is_some_and(|t| t.elapsed() < Duration::from_millis(250)) { return; }
        let (head, tail) = frames.samples.split_at(frames.next);
        for (out, sample) in mixed.iter_mut().zip(tail.iter().chain(head)) { *out += sample * gain; }
    }
}

impl Frames {
    fn forget(&mut self) {
        self.count = 0;
        self.last = None;
        self.frame = None;
        self.previous = None;
        self.len = 0;
    }

    fn receive(&mut self, socket: &UdpSocket) {
        let mut packet = [0; PACKET];
        let now = Instant::now();
        for _ in 0..MAX_PACKETS {
            let Ok(n) = socket.recv(&mut packet) else { self.discarding = false; return; };
            if self.discarding { continue; }
            let mut bytes = &packet[..n];
            while !bytes.is_empty() {
                let newline = bytes.iter().position(|&b| b == b'\n');
                let end = newline.unwrap_or(bytes.len());
                let size = (self.line.len() - self.len).min(end);
                self.line[self.len..self.len + size].copy_from_slice(&bytes[..size]);
                self.len += size;
                if newline.is_none() { break; }
                if self.len < self.line.len() { self.finish_line(now); }
                self.len = 0;
                bytes = &bytes[end + 1..];
            }
        }
        // The UI was suspended. Catch up over bounded ticks without replaying
        // stale PCM or drawing across a gap in the stream.
        self.forget();
        self.discarding = true;
    }

    fn finish_line(&mut self, now: Instant) {
        let Ok(line) = std::str::from_utf8(&self.line[..self.len]) else { self.frame = None; return; };
        if let Some(header) = line.strip_prefix("frame:") {
            self.frame = header.split_ascii_whitespace().next().and_then(|v| v.parse().ok());
            return;
        }
        let Some(frame) = self.frame.take() else { return; };
        let Some((SAMPLE_KEY, value)) = line.split_once('=') else { return; };
        let Ok(value) = value.parse::<f32>() else { return; };
        if !value.is_finite() { return; }
        if self.previous.and_then(|n| n.checked_add(1)) != Some(frame) { self.count = 0; }
        self.previous = Some(frame);
        self.samples[self.next] = value;
        self.next = (self.next + 1) % HISTORY;
        self.count = (self.count + 1).min(HISTORY);
        self.last = Some(now);
    }
}

pub(super) fn triggered(history: &[f32; HISTORY]) -> [f32; SCOPE_POINTS] {
    let latest = HISTORY - SCOPE_POINTS;
    let (min, max) = history.iter().fold((f32::INFINITY, f32::NEG_INFINITY), |(lo, hi), &v| {
        (lo.min(v), hi.max(v))
    });
    // Trigger relative to the signal (not necessarily zero), but display the
    // measured samples including their actual DC offset and amplitude.
    let span = max - min;
    let center = min + 0.5 * span;
    let arm = min + 0.2 * span;
    let confirm = min + 0.65 * span;
    let mut crossing = None;
    if span > 0.00002 {
        // The buffer may begin partway out of a bass trough; don't discard the
        // only usable edge just because its arming excursion preceded the history.
        let mut armed = history[0] <= center;
        let mut pending = None;
        for (i, pair) in history.windows(2).enumerate() {
            if i >= latest && pending.is_none() { break; }
            let (a, b) = (pair[0], pair[1]);
            if a <= arm { armed = true; }
            if !armed { continue; }
            // A deep trough arms one edge. Shallow harmonic crossings cannot
            // rearm it, and chatter around center must clear the upper threshold.
            if b <= center { pending = None; }
            if i < latest && a <= center && b > center { pending = Some(i); }
            if b >= confirm {
                if let Some(edge) = pending.take() {
                    crossing = Some(edge);
                }
                armed = false;
            }
        }
    }
    let Some(i) = crossing else { return std::array::from_fn(|n| history[latest + n].clamp(-1.0, 1.0)); };
    let fraction = (center - history[i]) / (history[i + 1] - history[i]);
    std::array::from_fn(|n| {
        let a = history[i + n];
        (a + fraction * (history[i + n + 1] - a)).clamp(-1.0, 1.0)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::f32::consts::TAU;

    #[test]
    fn quiet_dc_biased_tone_locks_without_display_gain() {
        let signal = |n: f32| 0.42 + 0.001 * (TAU * n / 80.0).sin();
        for phase in [4.5, 28.5, 59.5] {
            let history = std::array::from_fn(|n| signal(n as f32 + phase));
            let trace = triggered(&history);
            for (n, &sample) in trace.iter().enumerate() {
                assert!((sample - signal(n as f32)).abs() < 0.000003);
            }
        }
    }

    #[test]
    fn locks_when_slow_bass_trough_precedes_history() {
        let signal = |n: usize| 0.5 * (TAU * (n % 160) as f32 / 160.0).sin();
        let trace = triggered(&std::array::from_fn(|n| signal(n + 156)));
        assert!(trace[0].abs() < 0.00001);
        assert!((trace[40] - 0.5).abs() < 0.00001);
    }

    #[test]
    fn locks_to_bass_cycle_not_shallow_secondary_edges() {
        let signal = |n: usize| {
            let phase = n % 80;
            let angle = TAU * phase as f32 / 80.0;
            let bump = (1.0 - (phase as f32 - 46.0).abs() / 3.0).max(0.0);
            0.8 * angle.sin() + 0.6 * bump + 0.005 * (14.0 * angle).sin()
        };
        for phase in [3, 21, 64] {
            let trace = triggered(&std::array::from_fn(|n| signal(n + phase)));
            for (n, &sample) in trace.iter().enumerate() {
                assert!((sample - signal(n)).abs() < 0.002);
            }
        }
    }

    #[test]
    fn untriggered_signal_uses_latest_real_samples() {
        let falling = std::array::from_fn(|n| 0.9 - n as f32 / HISTORY as f32);
        assert_eq!(&triggered(&falling)[..], &falling[HISTORY - SCOPE_POINTS..]);
        assert_eq!(triggered(&[0.0; HISTORY]), [0.0; SCOPE_POINTS]);
    }
}
