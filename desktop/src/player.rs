//! Silent stand-in for the playback engine. Step 2 replaces it with libmpv.

#[derive(Clone, Debug)]
pub struct PlaybackState {
    pub playing: bool,
    pub position_s: f32,
    pub duration_s: f32,
    pub volume: f32,
}

pub struct SilentPlayer {
    pub state: PlaybackState,
}

impl SilentPlayer {
    pub fn demo() -> Self {
        Self {
            state: PlaybackState {
                playing: false,
                position_s: 12.0,
                duration_s: 200.0,
                volume: 0.8,
            },
        }
    }

    pub fn toggle(&mut self) {
        self.state.playing = !self.state.playing;
    }

    pub fn stop(&mut self) {
        self.state.playing = false;
        self.state.position_s = 0.0;
    }

    pub fn seek_ratio(&mut self, ratio: f32) {
        let ratio = ratio.clamp(0.0, 1.0);
        self.state.position_s = ratio * self.state.duration_s;
    }

    pub fn set_volume(&mut self, volume: f32) {
        self.state.volume = volume.clamp(0.0, 1.0);
    }

    /// Advances the fake playhead. Stops at the end of the demo track.
    pub fn tick(&mut self, dt_s: f32) {
        if !self.state.playing {
            return;
        }
        self.state.position_s = (self.state.position_s + dt_s).min(self.state.duration_s);
        if self.state.position_s >= self.state.duration_s {
            self.state.playing = false;
        }
    }
}
