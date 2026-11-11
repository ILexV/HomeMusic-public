//! Where the window was last left. Also the mode, the size, the volume, and the equalizer.
//! A file that only has `x` and `y` still loads.

use std::path::PathBuf;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Place {
    pub x: f32,
    pub y: f32,
    pub mode: Option<i32>,
    pub width: Option<f32>,
    pub height: Option<f32>,
    pub volume: Option<f32>,
    pub eq_on: Option<bool>,
    pub eq_open: Option<bool>,
    pub preamp: Option<f32>,
    pub bands: Option<[f32; 10]>,
}

pub fn path() -> PathBuf {
    crate::paths::dir().join("window-place.toml")
}

pub fn load() -> Option<Place> {
    let text = std::fs::read_to_string(path()).ok()?;
    parse(&text)
}

pub fn save(place: Place) {
    let mut body = format!("x = {}\ny = {}\n", place.x, place.y);
    if let Some(mode) = place.mode {
        body.push_str(&format!("mode = {mode}\n"));
    }
    if let Some(width) = place.width {
        body.push_str(&format!("width = {width}\n"));
    }
    if let Some(height) = place.height {
        body.push_str(&format!("height = {height}\n"));
    }
    if let Some(volume) = place.volume {
        body.push_str(&format!("volume = {volume}\n"));
    }
    if let Some(eq_on) = place.eq_on {
        body.push_str(&format!("eq = {}\n", if eq_on { "true" } else { "false" }));
    }
    if let Some(eq_open) = place.eq_open {
        body.push_str(&format!(
            "eq-open = {}\n",
            if eq_open { "true" } else { "false" }
        ));
    }
    if let Some(preamp) = place.preamp {
        body.push_str(&format!("preamp = {preamp}\n"));
    }
    if let Some(bands) = place.bands {
        let text = bands
            .iter()
            .map(|gain| format!("{gain:.1}"))
            .collect::<Vec<_>>()
            .join(",");
        body.push_str(&format!("bands = {text}\n"));
    }
    let _ = std::fs::write(path(), body);
}

pub fn parse(text: &str) -> Option<Place> {
    let mut x = None;
    let mut y = None;
    let mut mode = None;
    let mut width = None;
    let mut height = None;
    let mut volume = None;
    let mut eq_on = None;
    let mut eq_open = None;
    let mut preamp = None;
    let mut bands = None;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let value = value.trim().trim_matches('"');
        match key.trim() {
            "x" => x = parse_f32(value),
            "y" => y = parse_f32(value),
            "mode" => {
                if let Ok(parsed) = value.parse::<i32>() {
                    if (0..=2).contains(&parsed) {
                        mode = Some(parsed);
                    }
                }
            }
            "width" => width = positive(value),
            "height" => height = positive(value),
            "volume" => {
                if let Some(parsed) = parse_f32(value) {
                    if (0.0..=1.0).contains(&parsed) {
                        volume = Some(parsed);
                    }
                }
            }
            "eq" => eq_on = parse_bool(value),
            "eq-open" => eq_open = parse_bool(value),
            "preamp" => preamp = parse_f32(value).map(crate::eq::clamp_db),
            "bands" => bands = parse_bands(value),
            _ => {}
        }
    }
    Some(Place {
        x: x?,
        y: y?,
        mode,
        width,
        height,
        volume,
        eq_on,
        eq_open,
        preamp,
        bands,
    })
}

/// A saved size the window can actually open with. Anything else falls back to the mode preset.
pub fn usable_size(width: Option<f32>, height: Option<f32>) -> Option<(f32, f32)> {
    let width = width?;
    let height = height?;
    if width.is_finite()
        && height.is_finite()
        && (200.0..=4000.0).contains(&width)
        && (80.0..=4000.0).contains(&height)
    {
        Some((width, height))
    } else {
        None
    }
}

fn parse_f32(value: &str) -> Option<f32> {
    let parsed = value.parse::<f32>().ok()?;
    parsed.is_finite().then_some(parsed)
}

fn positive(value: &str) -> Option<f32> {
    let parsed = parse_f32(value)?;
    (parsed > 0.0).then_some(parsed)
}

fn parse_bool(value: &str) -> Option<bool> {
    match value {
        "true" => Some(true),
        "false" => Some(false),
        _ => None,
    }
}

fn parse_bands(value: &str) -> Option<[f32; 10]> {
    let mut bands = [0.0; 10];
    let mut count = 0;
    for part in value.split(',') {
        if count >= bands.len() {
            return None;
        }
        bands[count] = crate::eq::clamp_db(parse_f32(part.trim())?);
        count += 1;
    }
    (count == bands.len()).then_some(bands)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bare(x: f32, y: f32) -> Place {
        Place {
            x,
            y,
            mode: None,
            width: None,
            height: None,
            volume: None,
            eq_on: None,
            eq_open: None,
            preamp: None,
            bands: None,
        }
    }

    #[test]
    fn parses_both_coordinates() {
        assert_eq!(parse("x = 120.5\ny = 40\n"), Some(bare(120.5, 40.0)));
    }

    #[test]
    fn rejects_a_file_missing_one_axis() {
        assert_eq!(parse("x = 10\n"), None);
    }

    #[test]
    fn an_old_file_still_parses_without_mode_or_size() {
        let place = parse("x = 12\ny = 8\n").unwrap();
        assert_eq!(place.mode, None);
        assert_eq!(place.width, None);
        assert_eq!(place.volume, None);
    }

    #[test]
    fn parses_mode_size_and_volume() {
        let place =
            parse("x = 3\ny = 4\nmode = 2\nwidth = 1040\nheight = 708\nvolume = 0.35\n").unwrap();
        assert_eq!(place.mode, Some(2));
        assert_eq!(place.width, Some(1040.0));
        assert_eq!(place.height, Some(708.0));
        assert_eq!(place.volume, Some(0.35));
        assert_eq!(
            usable_size(place.width, place.height),
            Some((1040.0, 708.0))
        );
    }

    #[test]
    fn ignores_a_bad_mode_volume_and_tiny_size() {
        let place =
            parse("x = 3\ny = 4\nmode = 9\nvolume = 1.5\nwidth = 10\nheight = 10\n").unwrap();
        assert_eq!(place.mode, None);
        assert_eq!(place.volume, None);
        assert_eq!(place.width, Some(10.0));
        assert_eq!(usable_size(place.width, place.height), None);
    }

    #[test]
    fn parses_the_equalizer_and_clamps_a_wild_band() {
        let place = parse(
            "x = 1\ny = 2\neq = true\neq-open = false\npreamp = -2.5\nbands = 0,0,0,0,0,6,0,0,0,40\n",
        )
        .unwrap();
        assert_eq!(place.eq_on, Some(true));
        assert_eq!(place.eq_open, Some(false));
        assert_eq!(place.preamp, Some(-2.5));
        let bands = place.bands.unwrap();
        assert_eq!(bands[5], 6.0);
        assert_eq!(bands[9], 12.0);
    }
}
