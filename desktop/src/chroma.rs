//! Pitch-class energy from one window of 12 kHz mono analysis PCM.
use std::f32::consts::TAU;

const N: usize = 4096;
const HALF: usize = N / 2;
const BIN_HZ: f32 = 12_000.0 / N as f32;
// Half a semitone outside C1 and B7. Ignore DC, rumble and the high treble.
const LOW_HZ: f32 = 31.77;
const HIGH_HZ: f32 = 4066.84;

pub(crate) struct Chroma {
    window: Box<[f32]>,
    roots: Box<[(f32, f32)]>,
    bins: Box<[(f32, f32)]>,
}

impl Chroma {
    pub(crate) fn new() -> Self {
        // Blackman-Harris keeps a loud peak's sidelobes from lighting other notes.
        // Fold the window's coherent gain and real FFT scaling into these weights.
        let window = (0..N).map(|i| {
            let a = TAU * i as f32 / N as f32;
            (0.35875 - 0.48829 * a.cos() + 0.14128 * (2.0 * a).cos()
                - 0.01168 * (3.0 * a).cos()) / (N as f32 * 0.35875)
        }).collect();
        let roots = (0..HALF).map(|k| {
            let (sin, cos) = (-TAU * k as f32 / N as f32).sin_cos();
            (cos, sin)
        }).collect();
        Self { window, roots, bins: vec![(0.0, 0.0); HALF].into_boxed_slice() }
    }

    pub(crate) fn levels(&mut self, pcm: &[f32; N]) -> [f32; 12] {
        // One half-size complex FFT transforms the even and odd real sequences
        // together. Unpack only the positive-frequency bins in the note range.
        for (i, (pair, weights)) in pcm.chunks_exact(2).zip(self.window.chunks_exact(2)).enumerate() {
            let reversed = i.reverse_bits() >> (usize::BITS - HALF.ilog2());
            self.bins[reversed] = (pair[0] * weights[0], pair[1] * weights[1]);
        }
        let mut width = 2;
        while width <= HALF {
            for block in self.bins.chunks_exact_mut(width) {
                let (left, right) = block.split_at_mut(width / 2);
                for (j, (a, b)) in left.iter_mut().zip(right).enumerate() {
                    let (c, s) = self.roots[j * (N / width)];
                    let (re, im) = (c * b.0 - s * b.1, s * b.0 + c * b.1);
                    *b = (a.0 - re, a.1 - im);
                    *a = (a.0 + re, a.1 + im);
                }
            }
            width *= 2;
        }

        let first = (LOW_HZ / BIN_HZ) as usize;
        let last = (HIGH_HZ / BIN_HZ).ceil() as usize;
        let mut left = self.power(first - 1);
        let mut peak = self.power(first);
        let mut classes = [0.0; 12];
        for k in first..=last {
            let right = self.power(k + 1);
            if peak > 1.0e-7 && peak > left && peak >= right {
                // Estimate the peak between bins before quantizing: one FFT bin
                // spans more than a semitone in the bass. Log-power interpolation
                // is nearly parabolic around a Blackman-Harris-windowed peak.
                let (a, b, c) = (left.max(f32::MIN_POSITIVE).ln(), peak.ln(), right.max(f32::MIN_POSITIVE).ln());
                let offset = 0.5 * (a - c) / (a - 2.0 * b + c);
                let hz = (k as f32 + offset) * BIN_HZ;
                if (LOW_HZ..=HIGH_HZ).contains(&hz) {
                    let midi = (69.0 + 12.0 * (hz / 440.0).log2()).round() as usize;
                    classes[midi % 12] += (b + 0.25 * (c - a) * offset).exp();
                }
            }
            left = peak;
            peak = right;
        }
        // Fixed amplitude reference: quiet notes stay quiet; silence never gains up.
        classes.map(|energy| energy.sqrt().sqrt().min(1.0))
    }

    fn power(&self, k: usize) -> f32 {
        let (a, b) = self.bins[k];
        let (c, d) = self.bins[HALF - k];
        let (cos, sin) = self.roots[k];
        // 2 X[k] = Z[k] + conj(Z[-k]) - i W[k] (Z[k] - conj(Z[-k])).
        let re = (a + c) + cos * (b + d) + sin * (a - c);
        let im = (b - d) + sin * (b + d) - cos * (a - c);
        re * re + im * im
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn signal(tones: &[(f32, f32)]) -> [f32; N] {
        std::array::from_fn(|n| tones.iter().map(|&(hz, amp)| amp * (TAU * hz * n as f32 / 12_000.0).sin()).sum())
    }

    #[test]
    fn follows_equal_temperament_and_folds_octaves() {
        let mut analyzer = Chroma::new();
        for midi in (60..72).chain([24, 25, 40, 48, 83, 96, 107]) {
            // Offset by 20 cents toward the next note, still the same pitch class.
            let hz = 440.0 * 2f32.powf((midi as f32 + 0.2 - 69.0) / 12.0);
            let levels = analyzer.levels(&signal(&[(hz, 0.4)]));
            for (class, &level) in levels.iter().enumerate() {
                if class == midi % 12 { assert!(level > 0.6, "{midi}: {levels:?}"); }
                else { assert!(level < 0.02, "{midi}: {levels:?}"); }
            }
        }
    }

    #[test]
    fn resolves_simultaneous_notes_at_different_levels() {
        let mut analyzer = Chroma::new();
        let levels = analyzer.levels(&signal(&[(130.81, 0.4), (329.63, 0.12), (392.0, 0.06)]));
        assert!(levels[0] > levels[4] && levels[4] > levels[7] && levels[7] > 0.2);
        for (class, &level) in levels.iter().enumerate() {
            if ![0, 4, 7].contains(&class) { assert!(level < 0.02, "{levels:?}"); }
        }
    }

    #[test]
    fn silence_dc_and_out_of_range_signals_leave_no_notes() {
        let mut analyzer = Chroma::new();
        assert!(analyzer.levels(&signal(&[(440.0, 0.3)]))[9] > 0.5);
        assert_eq!(analyzer.levels(&[0.0; N]), [0.0; 12]);
        assert_eq!(analyzer.levels(&[0.4; N]), [0.0; 12]);
        assert_eq!(analyzer.levels(&signal(&[(12.0, 0.3), (4900.0, 0.2)])), [0.0; 12]);
    }
}
