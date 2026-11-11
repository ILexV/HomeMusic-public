//! Where the window was last left. Also the mode, the size, and the volume.
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
}

pub fn path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("window-place.toml")
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
    let _ = std::fs::write(path(), body);
}

pub fn parse(text: &str) -> Option<Place> {
    let mut x = None;
    let mut y = None;
    let mut mode = None;
    let mut width = None;
    let mut height = None;
    let mut volume = None;
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
}
