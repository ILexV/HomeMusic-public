//! Display ballistics for the live stereo meter; never synthesize a beat on silence.
use std::time::Instant;

use slint::ComponentHandle;

use crate::{AppWindow, Player};

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
    last_sample: Option<Instant>,
    channels: [Channel; 2],
}

impl Meter {
    pub(crate) fn update(&mut self, ui: &AppWindow, player: &mut Player) {
        if ui.get_visualizer_on() && ui.window().is_visible() && !ui.window().is_minimized() {
            let now = Instant::now();
            let elapsed = match self.last_sample.replace(now) {
                Some(last) => now.duration_since(last).as_secs_f32(),
                None => {
                    player.set_meter_enabled(true);
                    0.0
                }
            };
            for (channel, sample) in self.channels.iter_mut().zip(player.meter_levels()) {
                channel.sample(sample, elapsed);
            }
        } else if self.last_sample.is_some() {
            player.set_meter_enabled(false);
            *self = Self::default();
        } else {
            return;
        }
        let [left, right] = &self.channels;
        ui.set_meter_left(left.level);
        ui.set_meter_right(right.level);
        ui.set_peak_left(left.peak);
        ui.set_peak_right(right.peak);
    }
}
