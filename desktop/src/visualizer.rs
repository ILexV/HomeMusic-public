//! Display ballistics for live audio measurements. Hidden windows do not run either tap.
use std::rc::Rc;
use std::time::Instant;

use slint::{ComponentHandle, Model, VecModel};

use crate::{player::MeterMode, AppWindow, Player, VisualizationMode};

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

#[derive(Default)]
pub(crate) struct Meter {
    active: MeterMode,
    last_sample: Option<Instant>,
    channels: [Channel; 2],
    spectrum: Option<Rc<VecModel<f32>>>,
}

impl Meter {
    /// Called by the transport tick so a hidden window needs no animation timer.
    pub(crate) fn sync_mode(&mut self, ui: &AppWindow, player: &mut Player) -> bool {
        let mode = match ui.get_visualizer_mode() {
            VisualizationMode::Cover => MeterMode::Off,
            _ if !ui.window().is_visible() || ui.window().is_minimized() => MeterMode::Off,
            VisualizationMode::Stereo => MeterMode::Stereo,
            VisualizationMode::Spectrum => MeterMode::Spectrum,
        };
        if mode != self.active {
            player.set_meter_mode(mode);
            match self.active {
                MeterMode::Stereo => {
                    self.channels = Default::default();
                    self.publish_stereo(ui);
                }
                MeterMode::Spectrum => {
                    if let Some(bands) = &self.spectrum {
                        for i in 0..bands.row_count() { bands.set_row_data(i, 0.0); }
                    }
                }
                MeterMode::Off => {}
            }
            self.active = mode;
            self.last_sample = None;
        }
        mode != MeterMode::Off
    }

    pub(crate) fn update(&mut self, ui: &AppWindow, player: &Player) {
        let now = Instant::now();
        let elapsed = self.last_sample.replace(now).map_or(1.0 / 60.0, |last| (now - last).as_secs_f32());
        let envelope = Envelope::new(elapsed);
        match self.active {
            MeterMode::Stereo => {
                for (channel, sample) in self.channels.iter_mut().zip(player.meter_levels()) {
                    channel.sample(sample, elapsed, &envelope);
                }
                self.publish_stereo(ui);
            }
            MeterMode::Spectrum => self.update_bands(ui, player.spectrum_levels(), &envelope),
            MeterMode::Off => {}
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
