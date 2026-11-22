//! Signed PCM from the playing filter graph, not a second decoder. Only the
//! side branch is downmixed, antialiased and quantized; mpv gets the dry frames.
use std::cell::RefCell;
use std::io;
use std::net::{Ipv4Addr, UdpSocket};
use std::time::{Duration, Instant};

const PACKET: usize = 1400;
const MAX_PACKETS: usize = 96;
const GROUP: usize = 4;
pub(crate) const VECTOR_RATE: usize = 6000;
const BITS: u32 = 13;
const MASK: u64 = (1 << BITS) - 1;

pub(super) struct PcmTap<const N: usize, const C: usize> {
    socket: UdpSocket,
    frames: RefCell<Frames<N, C>>,
}

struct Frames<const N: usize, const C: usize> {
    samples: [[f32; C]; N],
    next: usize,
    count: usize,
    last: Option<Instant>,
    unread: usize,
    discarding: bool,
    line: [u8; 128],
    len: usize,
    number: Option<u64>,
    previous: Option<u64>,
    words: [u64; C],
    channels: usize,
}

impl<const N: usize, const C: usize> PcmTap<N, C> {
    pub(super) fn open() -> io::Result<(Box<Self>, String)> {
        let socket = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0))?;
        socket.set_nonblocking(true)?;
        let port = socket.local_addr()?.port();
        let (rate, layout) = if C == 1 { (12000, "mono") } else { (VECTOR_RATE, "stereo") };
        // lavfi has no raw PCM file/socket sink. Four 13-bit samples fit exactly
        // in a double, so one min/max-only record transports four instants. Each
        // channel has its own accumulator; its last value is the group's maximum.
        // Keep ±2 headroom for post-EQ audio. Reblock the clock after printing:
        // a free-running anullsink need not consume the full side stream in mpv.
        let filter = format!(concat!(
            "@meter:lavfi=[asplit=2[dry][pcm];[pcm]apad=pad_dur=0.1,",
            "aresample,aformat=sample_rates={rate}:sample_fmts=dblp:channel_layouts={layout},",
            "aeval='st(0,if(mod(n,4),ld(0)*8192,0)+clip(round(val(ch)*2048)+4096,0,8191))':c=same,",
            "ametadata=delete,asetnsamples=n=4:p=0,",
            "astats=length=0:metadata=1:reset=1:measure_perchannel=Max_level:measure_overall=none,",
            "ametadata=print:file='udp\\://127.0.0.1\\:{port}?pkt_size={packet}',",
            "asetnsamples=n={block}:p=0,aresample[clock];",
            "[dry][clock]astreamselect=inputs=2:map=0]:o=[threads=1]"
        ), rate = rate, layout = layout, port = port, packet = PACKET, block = rate / 100);
        Ok((Box::new(Self { socket, frames: RefCell::new(Frames::new()) }), filter))
    }

    pub(super) fn clear(&self) {
        let mut frames = self.frames.borrow_mut();
        frames.forget();
        frames.discarding = true;
        frames.receive(&self.socket);
    }

    /// Visit the current complete window (oldest sample first); report whether
    /// it advanced since the last read. A second deck can still need the most
    /// recent valid window from this deck when only that second deck advanced.
    pub(super) fn read(&self, mut visit: impl FnMut(usize, [f32; C])) -> bool {
        let mut f = self.frames.borrow_mut();
        f.receive(&self.socket);
        if f.count != N || !f.last.is_some_and(|t| t.elapsed() < Duration::from_millis(250)) {
            return false;
        }
        let (head, tail) = f.samples.split_at(f.next);
        for (i, &s) in tail.iter().chain(head).enumerate() { visit(i, s); }
        std::mem::take(&mut f.unread) > 0
    }

    /// Phosphor integrates each captured sample once, not once per UI poll.
    /// Right-align the unread suffix so two decks can mix their newest samples.
    pub(super) fn read_new(&self, mut visit: impl FnMut(usize, [f32; C])) -> usize {
        let mut f = self.frames.borrow_mut();
        f.receive(&self.socket);
        let n = std::mem::take(&mut f.unread);
        if n == 0 || !f.last.is_some_and(|t| t.elapsed() < Duration::from_millis(250)) { return 0; }
        let start = (f.next + N - n) % N;
        let (head, tail) = f.samples.split_at(start);
        for (i, &s) in (N-n..N).zip(tail.iter().chain(head)) { visit(i, s); }
        n
    }
}

impl<const N: usize, const C: usize> Frames<N, C> {
    fn new() -> Self {
        Self { samples: [[0.0; C]; N], next: 0, count: 0, last: None, unread: 0,
            discarding: false, line: [0; 128], len: 0, number: None, previous: None,
            words: [0; C], channels: 0 }
    }

    fn forget(&mut self) {
        self.count = 0;
        self.last = None;
        self.unread = 0;
        self.len = 0;
        self.number = None;
        self.previous = None;
    }

    fn receive(&mut self, socket: &UdpSocket) {
        let mut packet = [0; PACKET];
        let now = Instant::now();
        for _ in 0..MAX_PACKETS {
            let Ok(n) = socket.recv(&mut packet) else { self.discarding = false; return; };
            if !self.discarding { self.ingest(&packet[..n], now); }
        }
        // A suspended UI must catch up rather than publish old buffered audio.
        self.forget();
        self.discarding = true;
    }

    fn ingest(&mut self, mut bytes: &[u8], now: Instant) {
        while !bytes.is_empty() {
            let end = bytes.iter().position(|&b| b == b'\n').unwrap_or(bytes.len());
            let size = end.min(self.line.len() - self.len);
            self.line[self.len..self.len + size].copy_from_slice(&bytes[..size]);
            self.len += size;
            if end == bytes.len() { break; }
            if self.len < self.line.len() { self.finish_line(now); } else { self.number = None; }
            self.len = 0;
            bytes = &bytes[end + 1..];
        }
    }

    fn finish_line(&mut self, now: Instant) {
        let Ok(line) = std::str::from_utf8(&self.line[..self.len]) else { self.number = None; return; };
        if let Some(s) = line.strip_prefix("frame:") {
            self.number = s.split_ascii_whitespace().next().and_then(|i| i.parse().ok());
            self.channels = 0;
            return;
        }
        let Some(number) = self.number else { return; };
        let key = if self.channels == 0 { "lavfi.astats.1.Max_level=" } else { "lavfi.astats.2.Max_level=" };
        let word = line.strip_prefix(key).and_then(|s| s.strip_suffix(".000000"))
            .and_then(|s| s.parse::<u64>().ok()).filter(|v| *v < 1 << (BITS * GROUP as u32));
        let Some(word) = word else { self.number = None; return; };
        self.words[self.channels] = word;
        self.channels += 1;
        if self.channels != C { return; }
        self.number = None;
        if self.previous.and_then(|p| p.checked_add(1)) != Some(number) {
            self.count = 0;
            self.unread = 0;
        }
        self.previous = Some(number);
        for k in (0..GROUP as u32).rev() {
            self.samples[self.next] = self.words.map(|w| (((w >> (k * BITS)) & MASK) as f32 - 4096.0) / 2048.0);
            self.next = (self.next + 1) % N;
        }
        self.count = (self.count + GROUP).min(N);
        self.last = Some(now);
        self.unread = (self.unread + GROUP).min(N);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(n: u64, samples: [i16; 4]) -> String {
        let word = samples.iter().fold(0u64, |word, &s| (word << BITS) | (4096 + i32::from(s)) as u64);
        format!("frame:{n} pts:0 pts_time:0\nlavfi.astats.1.Max_level={word}.000000\n")
    }

    #[test]
    fn vectorscope_never_reexposes_pcm_on_overlapping_or_stale_reads() {
        let (tap, _) = PcmTap::<8, 1>::open().unwrap();
        let push = |n, samples, now| tap.frames.borrow_mut().ingest(record(n, samples).as_bytes(), now);
        let read = || {
            let mut values = Vec::new();
            tap.read_new(|i, [v]| values.push((i, (v * 2048.0) as i16)));
            values
        };
        let now = Instant::now();
        push(0, [1, 2, 3, 4], now);
        assert_eq!(read(), [(4, 1), (5, 2), (6, 3), (7, 4)]);
        assert!(read().is_empty());
        push(1, [5; 4], now);
        push(2, [6; 4], now);
        assert_eq!(read(), [(0, 5), (1, 5), (2, 5), (3, 5), (4, 6), (5, 6), (6, 6), (7, 6)]);
        push(3, [7; 4], now);
        push(5, [8; 4], now); // packet 4 missing: no stroke across the gap
        assert_eq!(read(), [(4, 8), (5, 8), (6, 8), (7, 8)]);
        push(6, [9; 4], now - Duration::from_secs(1));
        assert!(read().is_empty());
    }

    #[test]
    fn fragmented_or_missing_records_never_join_noncontiguous_pitch_windows() {
        let (tap, _) = PcmTap::<8, 1>::open().unwrap();
        let now = Instant::now();
        let mut frame = [0.0; 8];
        let mut read = || tap.read(|i, [s]| frame[i] = s);
        {
            let mut f = tap.frames.borrow_mut();
            f.ingest(record(0, [1, 2, 3, 4]).as_bytes(), now);
            f.ingest(record(2, [5, 6, 7, 8]).as_bytes(), now);
        }
        assert!(!read());
        let next = record(3, [-1, -2, -3, -4]);
        let (head, tail) = next.as_bytes().split_at(next.len() - 5);
        tap.frames.borrow_mut().ingest(head, now);
        assert!(!read());
        tap.frames.borrow_mut().ingest(tail, now);
        assert!(read());
        assert!(!read(), "repeated poll must not represent fresh audio");
        assert_eq!(frame.map(|v| v * 2048.0), [5.0, 6.0, 7.0, 8.0, -1.0, -2.0, -3.0, -4.0]);
        tap.clear();
        assert!(!tap.read(|_, _| panic!("cleared PCM must not be exposed")));
    }
}
