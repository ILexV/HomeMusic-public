//! Display live audio measurements only while the artwork is visible.
use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};

use slint::{ComponentHandle, Image, Model, ModelNotify, ModelTracker, Rgb8Pixel, SharedPixelBuffer, VecModel};

use crate::{chroma::Chroma, player::{MeterMode, SCOPE_POINTS}, vectorscope::Vectorscope, AppWindow, Player, VisualizationMode};

// mpv delivers measurements in decoder bursts. Ease toward the last real
// measurement on every display tick instead of stepping, then holding it.
struct Envelope { attack: f32, release: f32 }

impl Envelope {
    fn new(elapsed: f32) -> Self {
        Self { attack: 1.0 - (-elapsed / 0.025).exp(), release: 1.0 - (-elapsed / 0.064).exp() }
    }

    fn follow(&self, current: f32, target: f32) -> f32 {
        let delta = target - current;
        if delta.abs() < 0.001 { return target; }
        current + delta * if delta > 0.0 { self.attack } else { self.release }
    }
}

#[derive(Default)]
struct Channel {
    level: f32,
    peak: f32,
    hold: f32,
}

impl Channel {
    fn sample(&mut self, value: f32, elapsed: f32, envelope: &Envelope) {
        self.level = envelope.follow(self.level, value);
        if value > 0.0 && value >= self.peak {
            self.peak = value;
            self.hold = 0.12;
        } else {
            let falling = (elapsed - self.hold).max(0.0);
            self.hold = (self.hold - elapsed).max(0.0);
            self.peak = (self.peak - 2.8 * falling).max(self.level);
        }
    }
}

// A scope trace is one snapshot, not 96 independently changing meter bars.
// Keep the model bound and invalidate the plot once per new audio frame.
struct ScopeFrame {
    samples: RefCell<[f32; SCOPE_POINTS]>,
    notify: ModelNotify,
}

impl Model for ScopeFrame {
    type Data = f32;
    fn row_count(&self) -> usize { SCOPE_POINTS }
    fn row_data(&self, row: usize) -> Option<f32> { self.samples.borrow().get(row).copied() }
    fn model_tracker(&self) -> &dyn ModelTracker { &self.notify }
}

impl ScopeFrame {
    fn replace(&self, samples: [f32; SCOPE_POINTS]) {
        if *self.samples.borrow() != samples {
            *self.samples.borrow_mut() = samples;
            self.notify.reset();
        }
    }
}

// A row is either logarithmic frequency bands or the twelve pitch classes.
// Ping-pong the immutable Slint images instead of allocating pixel storage per tick.
const ROW_TIME: Duration = Duration::from_millis(40);
const HISTORY_ROWS: usize = 120;
const BANDS: usize = 10;

struct Waterfall<const N: usize> {
    front: SharedPixelBuffer<Rgb8Pixel>,
    back: SharedPixelBuffer<Rgb8Pixel>,
    peaks: [f32; N],
    last: Option<Instant>,
}

impl<const N: usize> Waterfall<N> {
    fn new() -> Self {
        let mut front = SharedPixelBuffer::new(N as u32, HISTORY_ROWS as u32);
        front.make_mut_slice().fill(Self::color(0.0));
        let back = SharedPixelBuffer::new(N as u32, HISTORY_ROWS as u32);
        Self { front, back, peaks: [0.0; N], last: None }
    }

    fn color(level: f32) -> Rgb8Pixel {
        const RAMP: [[u8; 3]; 5] = [
            [27, 27, 27], [49, 40, 84], [135, 57, 106], [232, 135, 42], [255, 240, 176],
        ];
        let pos = level.clamp(0.0, 1.0) * 4.0;
        let i = (pos as usize).min(3);
        let channel = |c: usize| {
            let a = f32::from(RAMP[i][c]);
            (a + (f32::from(RAMP[i + 1][c]) - a) * (pos - i as f32)).round() as u8
        };
        Rgb8Pixel::new(channel(0), channel(1), channel(2))
    }

    fn freeze(&mut self) {
        self.last = None;
        self.peaks.fill(0.0);
    }

    fn update(&mut self, now: Instant, samples: [f32; N]) -> Option<Image> {
        let steps = match self.last {
            None => 1,
            Some(last) => ((now - last).as_millis() / ROW_TIME.as_millis()).min(HISTORY_ROWS as u128) as usize,
        };
        // Preserve a short attack that lands between two raster rows.
        for (peak, level) in self.peaks.iter_mut().zip(samples) { *peak = peak.max(level); }
        if steps == 0 { return None; }
        self.last = Some(match self.last {
            Some(last) if steps < HISTORY_ROWS => last + ROW_TIME * steps as u32,
            _ => now,
        });
        let retained = (HISTORY_ROWS - steps) * N;
        let newest = (HISTORY_ROWS - 1) * N;
        let pixels = self.back.make_mut_slice();
        pixels[..retained].copy_from_slice(&self.front.as_slice()[steps * N..]);
        // A delayed UI frame is a gap, not a repeated audio measurement.
        pixels[retained..newest].fill(Self::color(0.0));
        for (p, peak) in pixels[newest..].iter_mut().zip(&mut self.peaks) {
            *p = Self::color(std::mem::take(peak));
        }
        std::mem::swap(&mut self.front, &mut self.back);
        Some(Image::from_rgb8(self.front.clone()))
    }
}

struct NoteWaterfall {
    plot: Waterfall<12>,
    analyzer: Chroma,
    pcm: [f32; 4096],
    levels: [f32; 12],
}

impl NoteWaterfall {
    fn new() -> Self {
        Self { plot: Waterfall::new(), analyzer: Chroma::new(), pcm: [0.0; 4096], levels: [0.0; 12] }
    }

    fn update(&mut self, now: Instant, player: &Player) -> Option<Image> {
        if self.plot.last.is_some_and(|last| now - last < ROW_TIME) { return None; }
        if player.chroma_samples(&mut self.pcm) {
            self.levels = self.analyzer.levels(&self.pcm);
        } else if !self.pcm.iter().any(|s| s.abs() > 1e-6) {
            self.levels.fill(0.0);
        }
        // Decoder reads arrive in bursts. If the last complete PCM window is
        // still valid, retain its analysis rather than drawing a false silence.
        self.plot.update(now, self.levels)
    }
}

#[derive(Default)]
pub(crate) struct Meter {
    active: VisualizationMode,
    last_sample: Option<Instant>,
    channels: [Channel; 2],
    spectrum: Option<Rc<VecModel<f32>>>,
    scope: Option<Rc<ScopeFrame>>,
    waterfall: Option<Waterfall<BANDS>>,
    notes: Option<Box<NoteWaterfall>>,
    vectorscope: Option<Vectorscope>,
}

impl Meter {
    /// Called by the transport tick so a hidden window needs no animation timer.
    pub(crate) fn sync_mode(&mut self, ui: &AppWindow, player: &mut Player) -> bool {
        let mode = if ui.window().is_visible() && !ui.window().is_minimized() {
            ui.get_visualizer_mode()
        } else {
            VisualizationMode::Cover
        };
        if mode != self.active {
            player.set_meter_mode(match mode {
                VisualizationMode::Cover => MeterMode::Off,
                VisualizationMode::Stereo => MeterMode::Stereo,
                VisualizationMode::Spectrum | VisualizationMode::Waterfall => MeterMode::Spectrum,
                VisualizationMode::Scope => MeterMode::Scope,
                VisualizationMode::Chroma => MeterMode::Chroma,
                VisualizationMode::Vectorscope => MeterMode::Vectorscope,
            });
            match self.active {
                VisualizationMode::Stereo => {
                    self.channels = Default::default();
                    self.publish_stereo(ui);
                }
                VisualizationMode::Spectrum => {
                    if let Some(bands) = &self.spectrum {
                        for i in 0..bands.row_count() { bands.set_row_data(i, 0.0); }
                    }
                }
                VisualizationMode::Scope => {
                    if let Some(frame) = &self.scope { frame.replace([0.0; SCOPE_POINTS]); }
                }
                VisualizationMode::Waterfall | VisualizationMode::Chroma | VisualizationMode::Vectorscope => {
                    self.waterfall = None;
                    self.notes = None;
                    self.vectorscope = None;
                    ui.set_visualizer_image(Image::default());
                }
                VisualizationMode::Cover => {}
            }
            self.active = mode;
            self.last_sample = None;
        }
        mode != VisualizationMode::Cover
    }

    pub(crate) fn update(&mut self, ui: &AppWindow, player: &Player) {
        match self.active {
            VisualizationMode::Waterfall => {
                if ui.get_playing() {
                    let waterfall = self.waterfall.get_or_insert_with(Waterfall::new);
                    if let Some(image) = waterfall.update(Instant::now(), player.spectrum_levels()) {
                        ui.set_visualizer_image(image);
                    }
                } else if let Some(waterfall) = &mut self.waterfall {
                    waterfall.freeze();
                }
            }
            VisualizationMode::Chroma => {
                if ui.get_playing() {
                    let notes = self.notes.get_or_insert_with(|| Box::new(NoteWaterfall::new()));
                    if let Some(image) = notes.update(Instant::now(), player) { ui.set_visualizer_image(image); }
                } else if let Some(notes) = &mut self.notes { notes.plot.freeze(); }
            }
            VisualizationMode::Vectorscope => {
                if ui.get_playing() || self.vectorscope.is_none() {
                    let elapsed = self.elapsed();
                    let plot = self.vectorscope.get_or_insert_with(Vectorscope::new);
                    ui.set_visualizer_image(plot.draw(&player.vectorscope_samples(), elapsed));
                } else {
                    self.last_sample = None;
                }
            }
            VisualizationMode::Scope => self.update_scope(ui, player.scope_samples()),
            VisualizationMode::Stereo => {
                let elapsed = self.elapsed();
                let envelope = Envelope::new(elapsed);
                for (channel, sample) in self.channels.iter_mut().zip(player.meter_levels()) {
                    channel.sample(sample, elapsed, &envelope);
                }
                self.publish_stereo(ui);
            }
            VisualizationMode::Spectrum => {
                let envelope = Envelope::new(self.elapsed());
                self.update_bands(ui, player.spectrum_levels(), &envelope);
            }
            VisualizationMode::Cover => {}
        }
    }

    fn elapsed(&mut self) -> f32 {
        let now = Instant::now();
        self.last_sample.replace(now).map_or(1.0 / 60.0, |last| (now - last).as_secs_f32())
    }

    fn update_scope(&mut self, ui: &AppWindow, samples: [f32; SCOPE_POINTS]) {
        if let Some(frame) = &self.scope {
            frame.replace(samples);
        } else {
            let frame = Rc::new(ScopeFrame { samples: RefCell::new(samples), notify: Default::default() });
            ui.set_scope_samples(frame.clone().into());
            self.scope = Some(frame);
        }
    }

    fn update_bands(&mut self, ui: &AppWindow, samples: [f32; 10], envelope: &Envelope) {
        // Bind once and notify only changed bars, without replacing the repeater every frame.
        let bands = self.spectrum.get_or_insert_with(|| {
            let model = Rc::new(VecModel::from(vec![0.0; samples.len()]));
            ui.set_spectrum_levels(model.clone().into());
            model
        });
        for (i, sample) in samples.into_iter().enumerate() {
            let old = bands.row_data(i).unwrap_or_default();
            let level = envelope.follow(old, sample);
            if level != old { bands.set_row_data(i, level); }
        }
    }

    fn publish_stereo(&self, ui: &AppWindow) {
        let [left, right] = &self.channels;
        ui.set_meter_left(left.level);
        ui.set_meter_right(right.level);
        ui.set_peak_left(left.peak);
        ui.set_peak_right(right.peak);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn waterfall_scrolls_old_energy_up_and_preserves_attacks_between_rows() {
        let mut plot = Waterfall::new();
        let start = Instant::now();
        let mut bass = [0.0; BANDS];
        bass[0] = 0.75;
        let mut high = [0.0; BANDS];
        high[BANDS - 1] = 1.0;
        plot.update(start, bass);
        assert!(plot.update(start + ROW_TIME / 2, high).is_none());
        plot.update(start + ROW_TIME, [0.0; BANDS]);
        // Skip one row: a delayed tick must not stretch the burst across the gap.
        plot.update(start + ROW_TIME * 3, bass);
        let rows = plot.front.as_slice().chunks_exact(BANDS).rev().take(4).collect::<Vec<_>>();
        assert_eq!(rows[0][0], Waterfall::<BANDS>::color(0.75));
        assert!(rows[1].iter().all(|&pixel| pixel == Waterfall::<BANDS>::color(0.0)));
        assert_eq!(rows[2][BANDS - 1], Waterfall::<BANDS>::color(1.0));
        assert_eq!(rows[2][0], Waterfall::<BANDS>::color(0.0));
        assert_eq!(rows[3][0], Waterfall::<BANDS>::color(0.75));
    }
}
