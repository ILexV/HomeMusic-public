//! A short, triggered time-domain trace from a post-EQ lavfi side branch.
use std::cell::RefCell;
use std::io;
use std::net::{Ipv4Addr, UdpSocket};
use std::time::{Duration, Instant};

pub(crate) const SCOPE_POINTS: usize = 96;
// 20 ms of display at 4.8 kHz, plus 30 ms in which to find a rising zero crossing.
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
    let crossing = (0..latest).rev().find(|&i| history[i] <= 0.0 && history[i + 1] > 0.0);
    let Some(i) = crossing else { return std::array::from_fn(|n| history[latest + n].clamp(-1.0, 1.0)); };
    // Subsample alignment avoids the one-sample horizontal jitter of rounding
    // the trigger; interpolate the measured waveform, never synthesize a tone.
    let fraction = -history[i] / (history[i + 1] - history[i]);
    std::array::from_fn(|n| {
        let a = history[i + n];
        (a + fraction * (history[i + n + 1] - a)).clamp(-1.0, 1.0)
    })
}
