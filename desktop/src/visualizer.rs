//! Display ballistics for live audio measurements. Hidden windows do not run either tap.
use std::rc::Rc;
use std::time::Instant;

use slint::{ComponentHandle, Model, VecModel};

use crate::{player::MeterMode, AppWindow, Player, VisualizationMode};

#[derive(Default)]
struct Channel {
    level: f32,
    peak: f32,
    hold: f32,
}

impl Channel {
    fn sample(&mut self, value: f32, elapsed: f32) {
        self.level = value.max(self.level - 2.5 * elapsed);
        let release = (elapsed - self.hold).max(0.0);
        self.hold = (self.hold - elapsed).max(0.0);
        self.peak = (self.peak - 1.5 * release).max(self.level);
        if value >= self.peak && value > 0.0 {
            self.hold = 0.32;
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
    pub(crate) fn update(&mut self, ui: &AppWindow, player: &mut Player) {
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
                MeterMode::Spectrum => self.update_bands(ui, [0.0; 10], 1.0),
                MeterMode::Off => {}
            }
            self.active = mode;
            self.last_sample = None;
        }
        if mode == MeterMode::Off { return; }
        let now = Instant::now();
        let elapsed = self.last_sample.replace(now).map_or(0.0, |last| (now - last).as_secs_f32());
        match mode {
            MeterMode::Stereo => {
                for (channel, sample) in self.channels.iter_mut().zip(player.meter_levels()) {
                    channel.sample(sample, elapsed);
                }
                self.publish_stereo(ui);
            }
            MeterMode::Spectrum => self.update_bands(ui, player.spectrum_levels(), 2.5 * elapsed),
            MeterMode::Off => {}
        }
    }

    fn update_bands(&mut self, ui: &AppWindow, samples: [f32; 10], decay: f32) {
        // Bind once and notify only changed bars, without replacing the repeater every frame.
        let bands = self.spectrum.get_or_insert_with(|| {
            let model = Rc::new(VecModel::from(vec![0.0; samples.len()]));
            ui.set_spectrum_levels(model.clone().into());
            model
        });
        for (i, sample) in samples.into_iter().enumerate() {
            let old = bands.row_data(i).unwrap_or_default();
            let level = sample.max(old - decay);
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
