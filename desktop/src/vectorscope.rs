//! Plot paired PCM samples in mid/side coordinates. Mono is vertical; opposite phase is horizontal.
use slint::{Image, Rgb8Pixel, SharedPixelBuffer};

const TILE: usize = 16;
const LIT: u8 = 4;
// The low two bits indicate which RGB buffers no longer match the phosphor.
const DIRTY: u8 = 3;
const EXPOSURE: f32 = 32.0;
const DECAY_SECONDS: f32 = 0.14;
const RADIUS_FRACTION: f32 = 0.96;

pub(crate) struct Vectorscope {
    edge: usize,
    paper: Vec<u8>,
    ink: Vec<u8>,
    tiles: Vec<u8>,
    palette: [[Rgb8Pixel; 256]; 3],
    buffers: [SharedPixelBuffer<Rgb8Pixel>; 2],
    front: usize,
}

fn tile_rows(edge: usize, tile: usize) -> impl Iterator<Item = std::ops::Range<usize>> {
    let across = edge.div_ceil(TILE);
    let x = tile % across * TILE;
    let y = tile / across * TILE;
    (y..(y + TILE).min(edge)).map(move |row| row * edge + x..row * edge + (x + TILE).min(edge))
}

impl Vectorscope {
    pub(crate) fn new(edge: u32) -> Self {
        Self::from_ink(edge as usize, vec![0; (edge as usize).pow(2)])
    }

    fn from_ink(edge: usize, ink: Vec<u8>) -> Self {
        let palette = [[27, 27, 27], [41, 39, 37], [56, 51, 45]].map(|base: [u8; 3]| {
            std::array::from_fn(|level| {
                let t = level as f32 / 255.0;
                let blend = |start: u8, end: u8, weight: f32| {
                    (start as f32 + (end - start) as f32 * weight).round() as u8
                };
                Rgb8Pixel::new(blend(base[0], 255, t.sqrt()), blend(base[1], 211, t.sqrt()), blend(base[2], 123, t))
            })
        });
        let mut paper = vec![0; ink.len()];
        let mut tiles = vec![0; edge.div_ceil(TILE).pow(2)];
        let mut front = SharedPixelBuffer::new(edge as u32, edge as u32);
        let mid = (edge - 1) as f32 * 0.5;
        let radius = mid * RADIUS_FRACTION;
        for (i, pixel) in front.make_mut_slice().iter_mut().enumerate() {
            let (x, y) = (i % edge, i / edge);
            let (dx, dy) = ((x as f32 - mid).abs(), (y as f32 - mid).abs());
            let r = dx + dy;
            let grid = if (r - radius).abs() <= 0.6 || (r < radius && dx.min(dy) <= 0.5) {
                2
            } else if r < radius && ((dx - dy).abs() < 0.5 || (r - radius * 0.5).abs() <= 0.6) {
                1
            } else {
                0
            };
            paper[i] = grid as u8;
            *pixel = palette[grid][ink[i] as usize];
            if ink[i] != 0 {
                tiles[y / TILE * edge.div_ceil(TILE) + x / TILE] = LIT;
            }
        }
        Self { edge, paper, ink, tiles, palette, buffers: [front.clone(), front], front: 0 }
    }

    pub(crate) fn resize(&mut self, edge: u32) -> bool {
        let edge = edge as usize;
        if edge == self.edge { return false; }
        let mut ink = vec![0; edge * edge];
        if edge < self.edge {
            // Peak pooling keeps a one-pixel diagonal visible in the mini view.
            let scale = (edge - 1) as f32 / (self.edge - 1) as f32;
            for (tile, &flags) in self.tiles.iter().enumerate() {
                if flags & LIT == 0 { continue; }
                for row in tile_rows(self.edge, tile) {
                    let y = ((row.start / self.edge) as f32 * scale).round() as usize;
                    for i in row {
                        let x = ((i % self.edge) as f32 * scale).round() as usize;
                        let dst = &mut ink[y * edge + x];
                        *dst = (*dst).max(self.ink[i]);
                    }
                }
            }
        } else {
            let scale = (self.edge - 1) as f32 / (edge - 1) as f32;
            for (y, row) in ink.chunks_exact_mut(edge).enumerate() {
                let sy = y as f32 * scale;
                let y0 = sy as usize;
                let y1 = (y0 + 1).min(self.edge - 1);
                for (x, dst) in row.iter_mut().enumerate() {
                    let sx = x as f32 * scale;
                    let x0 = sx as usize;
                    let x1 = (x0 + 1).min(self.edge - 1);
                    let horizontal = |y: usize| {
                        let a = self.ink[y * self.edge + x0] as f32;
                        let b = self.ink[y * self.edge + x1] as f32;
                        a + (b - a) * sx.fract()
                    };
                    let a = horizontal(y0);
                    *dst = (a + (horizontal(y1) - a) * sy.fract()).round() as u8;
                }
            }
        }
        *self = Self::from_ink(edge, ink);
        true
    }

    pub(crate) fn draw(&mut self, samples: &[[f32; 2]], reference: f32, elapsed: f32) -> Image {
        self.fade(elapsed);
        let mid = (self.edge - 1) as f32 * 0.5;
        let scale = mid * RADIUS_FRACTION * 0.5 / reference;
        let project = |[l, r]: [f32; 2]| (mid + (r - l) * scale, mid - (r + l) * scale);
        if let Some((&first, rest)) = samples.split_first() {
            let mut last = project(first);
            let mut silent = first == [0.0; 2];
            if rest.is_empty() && !silent { self.stroke(last, last); }
            for &sample in rest {
                let next = project(sample);
                let next_silent = sample == [0.0; 2];
                if !(silent && next_silent) { self.stroke(last, next); }
                (last, silent) = (next, next_silent);
            }
        }
        let current = 1_u8 << self.front;
        if self.tiles.iter().any(|t| t & current != 0) {
            self.front ^= 1;
            let dirty = 1_u8 << self.front;
            let pixels = self.buffers[self.front].make_mut_slice();
            for (tile, flags) in self.tiles.iter_mut().enumerate() {
                if *flags & dirty == 0 { continue; }
                for row in tile_rows(self.edge, tile) {
                    for ((p, &grid), &level) in pixels[row.clone()].iter_mut()
                        .zip(&self.paper[row.clone()]).zip(&self.ink[row])
                    {
                        *p = self.palette[grid as usize][level as usize];
                    }
                }
                *flags &= !dirty;
            }
        }
        Image::from_rgb8(self.buffers[self.front].clone())
    }

    fn fade(&mut self, elapsed: f32) {
        let factor = (65536.0 * (-elapsed / DECAY_SECONDS).exp()).round() as u32;
        if factor >= 65536 { return; }
        for (tile, flags) in self.tiles.iter_mut().enumerate() {
            if *flags & LIT == 0 { continue; }
            let mut lit = false;
            for row in tile_rows(self.edge, tile) {
                for v in &mut self.ink[row] {
                    *v = ((u32::from(*v) * factor) >> 16) as u8;
                    lit |= *v != 0;
                }
            }
            *flags = DIRTY | if lit { LIT } else { 0 };
        }
    }

    fn hit(&mut self, x: usize, y: usize, coverage: f32) {
        let energy = (EXPOSURE * coverage + 0.5) as u8;
        if energy == 0 { return; }
        let level = &mut self.ink[y * self.edge + x];
        if *level == 255 { return; }
        *level = level.saturating_add(energy);
        self.tiles[y / TILE * self.edge.div_ceil(TILE) + x / TILE] = LIT | DIRTY;
    }

    fn stroke(&mut self, (x0, y0): (f32, f32), (x1, y1): (f32, f32)) {
        let steep = (y1 - y0).abs() > (x1 - x0).abs();
        let (mut a, mut b) = if steep { ((y0, x0), (y1, x1)) } else { ((x0, y0), (x1, y1)) };
        if a.0 > b.0 { std::mem::swap(&mut a, &mut b); }
        let length = b.0 - a.0;
        if length < 1.0 {
            // A short segment still represents one sampling interval. Distribute
            // its energy bilinearly instead of losing quiet audio to rounding.
            let (x, y) = ((x0 + x1) * 0.5, (y0 + y1) * 0.5);
            let (ix, iy) = (x as usize, y as usize);
            let (fx, fy) = (x.fract(), y.fract());
            self.hit(ix, iy, (1.0 - fx) * (1.0 - fy));
            self.hit(ix + 1, iy, fx * (1.0 - fy));
            self.hit(ix, iy + 1, (1.0 - fx) * fy);
            self.hit(ix + 1, iy + 1, fx * fy);
            return;
        }
        // Integrate the major-axis coverage of each pixel column, then split
        // that exposure between the two rows straddling the subpixel line.
        let slope = (b.1 - a.1) / length;
        for major in (a.0 + 0.5) as usize..=(b.0 + 0.5) as usize {
            let lo = a.0.max(major as f32 - 0.5);
            let hi = b.0.min(major as f32 + 0.5);
            let minor = a.1 + ((lo + hi) * 0.5 - a.0) * slope;
            let i = minor as usize;
            let high = (hi - lo) * minor.fract();
            let low = hi - lo - high;
            if steep {
                self.hit(i, major, low);
                self.hit(i + 1, major, high);
            } else {
                self.hit(major, i, low);
                self.hit(major, i + 1, high);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fractional_coverage_adds_light_instead_of_overwriting_it() {
        let mut plot = Vectorscope::new(64);
        let stroke = |p: &mut Vectorscope| p.stroke((10.0, 12.25), (20.0, 12.25));
        stroke(&mut plot);
        assert_eq!(plot.ink[12 * 64 + 15], 24);
        assert_eq!(plot.ink[13 * 64 + 15], 8);
        assert_eq!(plot.ink[12 * 64 + 10], 12); // fractional endpoint
        stroke(&mut plot);
        assert_eq!(plot.ink[12 * 64 + 15], 48);
        for _ in 0..40 { stroke(&mut plot); }
        assert_eq!(plot.ink[12 * 64 + 15], 255);
        assert_eq!(plot.ink[13 * 64 + 15], 255);
    }

    #[test]
    fn mono_is_vertical_opposite_phase_horizontal_and_silence_clears_both_buffers() {
        let mut plot = Vectorscope::new(129);
        let above = 24 * 129 + 64;
        let right = 64 * 129 + 104;
        plot.draw(&[[-0.8, -0.8], [0.8, 0.8]], 1.0, 0.016);
        assert_eq!((plot.ink[above], plot.ink[right]), (32, 0));
        plot.draw(&[[-0.8, 0.8], [0.8, -0.8]], 1.0, 0.05);
        assert!((1..32).contains(&plot.ink[above]));
        assert_eq!(plot.ink[right], 32);
        plot.draw(&[[0.0; 2]; 8], 1.0, 2.0);
        assert!(plot.ink.iter().all(|&v| v == 0));
        assert!(plot.tiles.iter().all(|t| t & LIT == 0));
        // A subsequent swap must not bring back stale pixels.
        plot.draw(&[[0.1, 0.1]], 1.0, 0.0);
        for i in [above, right] {
            assert_eq!(plot.buffers[plot.front].as_slice()[i], plot.palette[plot.paper[i] as usize][0]);
        }
    }

    #[test]
    fn fresh_batches_do_not_draw_connecting_segments() {
        let mut plot = Vectorscope::new(65);
        for sample in [[0.06, 0.06], [-0.06, -0.06]] {
            plot.draw(&[sample; 2], 0.32, 0.0);
        }
        assert_eq!(plot.ink[32 * 65 + 32], 0);
        assert!(plot.ink[26 * 65 + 32] > 0);
        assert!(plot.ink[38 * 65 + 32] > 0);
    }

    #[test]
    fn resizing_a_paused_trace_preserves_its_position_and_energy() {
        let mut plot = Vectorscope::new(257);
        plot.draw(&[[-0.8, -0.8], [0.8, 0.8]], 1.0, 0.0);
        for edge in [65, 129] {
            assert!(plot.resize(edge));
            let i = (edge / 4 * edge + edge / 2) as usize;
            assert_eq!(plot.ink[i], 32);
            let storage = plot.ink.as_ptr();
            assert!(!plot.resize(edge));
            let front = plot.front;
            plot.draw(&[], 1.0, 0.0);
            assert_eq!(plot.front, front);
            assert_eq!(plot.ink.as_ptr(), storage);
            assert_eq!(plot.buffers[front].as_slice()[i], plot.palette[plot.paper[i] as usize][32]);
        }
    }
}
