//! Ten-band graphic equalizer as one mpv `lavfi` graph.
//! A flat or disabled curve installs no filter, so playback stays untouched.

pub const MIN_DB: f32 = -12.0;
pub const MAX_DB: f32 = 12.0;

const BAND_HZ: [f32; 10] = [
    31.0, 62.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0,
];
const Q: f32 = 1.4;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Settings {
    pub enabled: bool,
    pub preamp: f32,
    pub bands: [f32; 10],
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            enabled: false,
            preamp: 0.0,
            bands: [0.0; 10],
        }
    }
}

pub fn clamp_db(value: f32) -> f32 {
    if value.is_finite() {
        value.clamp(MIN_DB, MAX_DB)
    } else {
        0.0
    }
}

/// `None` means the audio filter list should be cleared.
pub fn chain(settings: Settings) -> Option<String> {
    if !settings.enabled {
        return None;
    }
    let preamp = clamp_db(settings.preamp);
    let bands = settings.bands.map(clamp_db);
    let flat = preamp.abs() < 0.05 && bands.iter().all(|gain| gain.abs() < 0.05);
    if flat {
        return None;
    }
    let mut parts = Vec::with_capacity(11);
    if preamp.abs() >= 0.05 {
        parts.push(format!("volume={preamp:.1}dB"));
    }
    for (hz, gain) in BAND_HZ.into_iter().zip(bands) {
        parts.push(format!("equalizer=f={hz:.0}:t=q:w={Q}:g={gain:.1}"));
    }
    Some(format!("lavfi=[{}]", parts.join(",")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_disabled_or_flat_curve_adds_no_filter() {
        assert_eq!(chain(Settings::default()), None);
        let mut on = Settings {
            enabled: true,
            ..Settings::default()
        };
        assert_eq!(chain(on), None);
        on.bands[5] = 6.0;
        on.enabled = false;
        assert_eq!(chain(on), None);
    }

    #[test]
    fn a_raised_band_and_preamp_keep_every_band_in_the_graph() {
        let mut settings = Settings {
            enabled: true,
            preamp: -3.0,
            ..Settings::default()
        };
        settings.bands[5] = 6.0;
        settings.bands[0] = 40.0;
        let graph = chain(settings).unwrap();
        assert!(graph.starts_with("lavfi=[volume=-3.0dB,"));
        assert!(graph.contains("equalizer=f=31:t=q:w=1.4:g=12.0"));
        assert!(graph.contains("equalizer=f=1000:t=q:w=1.4:g=6.0"));
        assert!(graph.contains("equalizer=f=16000:t=q:w=1.4:g=0.0"));
        assert_eq!(graph.matches("equalizer=").count(), 10);
    }
}
