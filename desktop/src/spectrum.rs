//! The audible asplit output is a direct reference to the original frame. Only
//! the analysis copy is resampled, split into octaves and measured. mpv exposes
//! af-metadata for the *output* frame only; carry the side branch's numbers (no
//! audio) over a private loopback port instead of remixing it into the output.
use std::cell::RefCell;
use std::io;
use std::net::{Ipv4Addr, UdpSocket};
use std::time::{Duration, Instant};

const PACKET: usize = 1024;
const MAX_PACKETS: usize = 64;
const SILENCE: [f64; 10] = [f64::NEG_INFINITY; 10];

pub(super) struct SpectrumTap {
    socket: UdpSocket,
    frames: RefCell<Frames>,
}

struct Frames {
    // A 20-key astats record fits in one KiB, but AVIO can split it anywhere.
    bytes: [u8; PACKET * 3],
    len: usize,
    last: Option<(Instant, [f64; 10])>,
    discarding: bool,
}

impl SpectrumTap {
    pub(super) fn open() -> io::Result<(Self, String)> {
        let socket = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0))?;
        socket.set_nonblocking(true)?;
        let port = socket.local_addr()?.port();
        // Fourth-order Linkwitz-Riley crossovers at the geometric midpoints
        // between 31.25 Hz ... 16 kHz. Keep stereo through the bank: opposite
        // phase channels must not cancel; a right-only instrument is not silent.
        let graph = format!(concat!(
            "@meter:lavfi=[asplit=2[dry][wet];[wet]",
            "aformat=sample_rates=48000:sample_fmts=fltp:channel_layouts=stereo,",
            "asetnsamples=n=1920:p=0,acrossover=split=44 88 177 354 707 1414 2828 5657 11314:order=4th",
            "[a][b][c][d][e][f][g][h][i][j];[a][b][c][d][e][f][g][h][i][j]amerge=inputs=10,",
            "ametadata=delete,astats=metadata=1:reset=1:measure_perchannel=Peak_level:measure_overall=none,",
            "ametadata=print:file='udp\\://127.0.0.1\\:{}?pkt_size=1024',anullsink;[dry]anull]:o=[threads=1]"
        ), port);
        Ok((Self { socket, frames: RefCell::new(Frames {
            bytes: [0; PACKET * 3], len: 0, last: None, discarding: false,
        }) }, graph))
    }

    pub(super) fn clear(&self) {
        let mut frames = self.frames.borrow_mut();
        frames.last = None;
        frames.len = 0;
        frames.discarding = true;
        frames.receive(&self.socket);
    }

    pub(super) fn peaks(&self) -> [f64; 10] {
        let mut frames = self.frames.borrow_mut();
        frames.receive(&self.socket);
        frames.last.filter(|(time, _)| time.elapsed() < Duration::from_millis(400))
            .map_or(SILENCE, |(_, peaks)| peaks)
    }
}

impl Frames {
    fn receive(&mut self, socket: &UdpSocket) {
        let mut received = false;
        for _ in 0..MAX_PACKETS {
            if self.len > self.bytes.len() - PACKET {
                self.bytes.copy_within(self.len - PACKET..self.len, 0);
                self.len = PACKET;
            }
            match socket.recv(&mut self.bytes[self.len..self.len + PACKET]) {
                Ok(n) if !self.discarding => { self.len += n; received = true; }
                Ok(_) => {}
                Err(_) => {
                    self.discarding = false;
                    if received { self.decode_latest(); }
                    return;
                }
            }
        }
        // UI was suspended or packets arrived faster than we can display them.
        // Discard the backlog over bounded ticks, never replay old measurements.
        self.discarding = true;
        self.len = 0;
        self.last = None;
    }

    fn decode_latest(&mut self) {
        let Ok(text) = std::str::from_utf8(&self.bytes[..self.len]) else { self.len = 0; return; };
        let Some(end) = text.rfind("frame:") else { return; };
        let complete = &text[..end];
        if let Some((_, frame)) = complete.rsplit_once("frame:") {
            let mut lines = frame.lines().skip(1);
            let mut peaks = SILENCE;
            let mut count = 0;
            for (channel, line) in (1..=20).zip(&mut lines) {
                let Some((key, value)) = line.split_once(".Peak_level=") else { break; };
                if key.strip_prefix("lavfi.astats.").and_then(|s| s.parse().ok()) != Some(channel) { break; }
                let Ok(db) = value.parse::<f64>() else { break; };
                if db.is_finite() { peaks[(channel - 1) / 2] = peaks[(channel - 1) / 2].max(db); }
                count += 1;
            }
            if count == 20 { self.last = Some((Instant::now(), peaks)); }
        }
        self.bytes.copy_within(end..self.len, 0);
        self.len -= end;
    }
}
