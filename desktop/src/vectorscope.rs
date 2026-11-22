//! Plot paired PCM samples in mid/side coordinates. Mono is vertical; opposite phase is horizontal.
use slint::{Image, Rgb8Pixel, SharedPixelBuffer};

const EDGE: usize = 241;
const MID: i32 = EDGE as i32 / 2;
const RADIUS: i32 = MID - 5;

pub(crate) struct Vectorscope {
    // One byte each for the static graticule and the fading phosphor.
    paper: Vec<u8>,
    ink: Vec<u8>,
    palette: [[Rgb8Pixel; 256]; 3],
    front: SharedPixelBuffer<Rgb8Pixel>,
    back: SharedPixelBuffer<Rgb8Pixel>,
}

impl Vectorscope {
    pub(crate) fn new() -> Self {
        let mut paper = vec![0; EDGE * EDGE];
        for (i, pixel) in paper.iter_mut().enumerate() {
            let x = (i % EDGE) as i32 - MID;
            let y = (i / EDGE) as i32 - MID;
            let radius = x.abs() + y.abs();
            *pixel = if radius == RADIUS || (radius < RADIUS && (x == 0 || y == 0)) { 2 }
                else if radius < RADIUS && (x.abs() == y.abs() || radius == RADIUS / 2) { 1 }
                else { 0 };
        }
        let palette = [[27, 27, 27], [41, 39, 37], [56, 51, 45]].map(|base: [u8; 3]| {
            std::array::from_fn(|level| {
                let t = level as f32 / 255.0;
                let blend = |start: u8, end: u8, weight: f32| (start as f32 + (end - start) as f32 * weight) as u8;
                Rgb8Pixel::new(blend(base[0], 255, t.sqrt()), blend(base[1], 211, t), blend(base[2], 123, t*t))
            })
        });
        Self { paper, ink: vec![0; EDGE * EDGE], palette,
            front: SharedPixelBuffer::new(EDGE as u32, EDGE as u32),
            back: SharedPixelBuffer::new(EDGE as u32, EDGE as u32) }
    }

    pub(crate) fn draw(&mut self, samples: &[[f32; 2]], elapsed: f32) -> Image {
        let fade = (256.0 * (-elapsed / 0.095).exp()).round().clamp(0.0, 255.0) as u16;
        for v in &mut self.ink { *v = ((u16::from(*v) * fade) >> 8) as u8; }
        // One gain for both channels: quiet passages stay legible without changing stereo width.
        let peak = samples.iter().fold(0.0_f32, |p, &[l, r]| p.max(l.abs()).max(r.abs()));
        if peak > 0.0005 {
            let scale = RADIUS as f32 * 0.5 / peak.max(0.32);
            let mut points = samples.iter().map(|&[l, r]| {
                (MID + ((r - l) * scale).round() as i32, MID - ((r + l) * scale).round() as i32)
            });
            if let Some(mut last) = points.next() {
                for next in points { self.stroke(last, next); last = next; }
            }
        }
        for ((p, &grid), &ink) in self.back.make_mut_slice().iter_mut().zip(&self.paper).zip(&self.ink) {
            *p = self.palette[grid as usize][ink as usize];
        }
        std::mem::swap(&mut self.front, &mut self.back);
        Image::from_rgb8(self.front.clone())
    }

    fn stroke(&mut self, (mut x, mut y): (i32, i32), (ex, ey): (i32, i32)) {
        let dx = (ex - x).abs(); let sx = (ex - x).signum();
        let dy = -(ey - y).abs(); let sy = (ey - y).signum();
        let mut err = dx + dy;
        // Mid/side coordinates stay inside the diamond, with room for a soft edge.
        loop {
            let i = y as usize * EDGE + x as usize;
            self.ink[i] = 255;
            let side = if dx > -dy { EDGE } else { 1 };
            self.ink[i - side] = self.ink[i - side].max(95);
            self.ink[i + side] = self.ink[i + side].max(95);
            if (x, y) == (ex, ey) { break; }
            let e2 = err * 2;
            if e2 >= dy { err += dy; x += sx; }
            if e2 <= dx { err += dx; y += sy; }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vectorscope_separates_mono_and_opposite_phase_and_fades_history() {
        let mut plot = Vectorscope::new();
        plot.draw(&[[-0.8, -0.8], [0.8, 0.8]], 0.016);
        let above = (MID as usize - 40) * EDGE + MID as usize;
        let right = MID as usize * EDGE + MID as usize + 40;
        assert_eq!((plot.ink[above], plot.ink[right]), (255, 0));
        plot.draw(&[[-0.8, 0.8], [0.8, -0.8]], 0.05);
        assert!((1..255).contains(&plot.ink[above]));
        assert_eq!(plot.ink[right], 255);
        plot.draw(&[[0.0, 0.0]; 2], 1.0);
        assert!(plot.ink.iter().all(|&level| level == 0));
    }
}
