//! Where the window was last left. Also the mode, the size, the volume, and the equalizer.
//! A file that only has `x` and `y` still loads.

use std::path::{Path, PathBuf};

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
    pub mini_position: Option<(f32, f32)>,
    pub normal_position: Option<(f32, f32)>,
}

/// Compare persisted settings, ignoring subpixel window-manager rounding.
pub fn same(left: Place, right: Place) -> bool {
    fn near(left: Option<f32>, right: Option<f32>, tolerance: f32) -> bool {
        match (left, right) {
            (Some(left), Some(right)) => (left - right).abs() < tolerance,
            (None, None) => true,
            _ => false,
        }
    }
    fn same_position(left: Option<(f32, f32)>, right: Option<(f32, f32)>) -> bool {
        match (left, right) {
            (Some((lx, ly)), Some((rx, ry))) => (lx - rx).abs() < 0.5 && (ly - ry).abs() < 0.5,
            (None, None) => true,
            _ => false,
        }
    }
    (left.x - right.x).abs() < 0.5
        && (left.y - right.y).abs() < 0.5
        && left.mode == right.mode
        && same_position(left.mini_position, right.mini_position)
        && same_position(left.normal_position, right.normal_position)
        && near(left.width, right.width, 0.5)
        && near(left.height, right.height, 0.5)
        && near(left.volume, right.volume, 0.005)
        && left.eq_on == right.eq_on
        && left.eq_open == right.eq_open
        && near(left.preamp, right.preamp, 0.05)
        && match (left.bands, right.bands) {
            (Some(left), Some(right)) => left.iter().zip(right).all(|(a, b)| (a - b).abs() < 0.05),
            (None, None) => true,
            _ => false,
        }
}

pub fn path() -> PathBuf {
    crate::paths::dir().join("window-place.toml")
}

pub fn load() -> Option<Place> {
    let text = std::fs::read_to_string(path()).ok()?;
    parse(&text)
}

pub fn save(place: Place) {
    let _ = save_at(&path(), place);
}

fn save_at(path: &Path, place: Place) -> std::io::Result<bool> {
    // The timer filters unchanged snapshots in memory. Also deduplicate explicit
    // saves (notably shutdown) against the file, without touching its timestamp.
    if std::fs::read_to_string(path).ok().and_then(|text| parse(&text))
        .is_some_and(|saved| same(saved, place))
    {
        return Ok(false);
    }
    let mut body = format!("x = {}\ny = {}\n", place.x, place.y);
    if let Some((x, y)) = place.mini_position {
        body.push_str(&format!("mini-x = {x}\nmini-y = {y}\n"));
    }
    if let Some((x, y)) = place.normal_position {
        body.push_str(&format!("normal-x = {x}\nnormal-y = {y}\n"));
    }
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
    std::fs::write(path, body)?;
    Ok(true)
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
    let mut mini_x = None;
    let mut mini_y = None;
    let mut normal_x = None;
    let mut normal_y = None;
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
            "mini-x" => mini_x = parse_f32(value),
            "mini-y" => mini_y = parse_f32(value),
            "normal-x" => normal_x = parse_f32(value),
            "normal-y" => normal_y = parse_f32(value),
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
        mini_position: mini_x.zip(mini_y),
        normal_position: normal_x.zip(normal_y),
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
            mini_position: None,
            normal_position: None,
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

    #[test]
    fn mini_position_is_independent_and_accepts_a_left_hand_monitor() {
        let place = parse("x=80\ny=60\nmode=2\nmini-x=-520\nmini-y=430\n").unwrap();
        assert_eq!((place.x, place.y), (80.0, 60.0));
        assert_eq!(place.mini_position, Some((-520.0, 430.0)));
        let broken = parse("x=80\ny=60\nmini-x=NaN\nmini-y=430\n").unwrap();
        assert_eq!(broken.mini_position, None);
        assert_eq!((broken.x, broken.y), (80.0, 60.0));
    }

    #[test]
    fn normal_position_survives_a_session_closed_in_mini_mode() {
        let place = parse(
            "x=880\ny=920\nmode=0\nmini-x=880\nmini-y=920\nnormal-x=-1200\nnormal-y=60\n",
        )
        .unwrap();
        assert_eq!((place.x, place.y), (880.0, 920.0));
        assert_eq!(place.mini_position, Some((880.0, 920.0)));
        assert_eq!(place.normal_position, Some((-1200.0, 60.0)));
        for coordinates in ["normal-x=80", "normal-x=80\nnormal-y=NaN"] {
            let broken = parse(&format!("x=880\ny=920\n{coordinates}\n")).unwrap();
            assert_eq!(broken.normal_position, None);
            assert_eq!((broken.x, broken.y), (880.0, 920.0));
        }
    }

    #[test]
    fn unchanged_optional_settings_and_subpixel_positions_compare_equal() {
        let place = bare(80.0, 60.0);
        assert!(same(place, place));
        let mut place = place;
        place.mini_position = Some((-520.0, 430.0));
        place.normal_position = Some((80.0, 60.0));
        let mut rounded = place;
        rounded.x += 0.25;
        rounded.normal_position = Some((80.25, 60.25));
        rounded.mini_position = Some((-519.75, 430.25));
        assert!(same(place, rounded));
        rounded.mini_position = Some((-519.0, 430.0));
        assert!(!same(place, rounded));
        rounded = place;
        rounded.normal_position = Some((81.0, 60.0));
        assert!(!same(place, rounded));
    }

    #[test]
    fn explicit_saves_write_only_geometry_mode_or_settings_changes() {
        use std::sync::atomic::{AtomicU64, Ordering};
        static SERIAL: AtomicU64 = AtomicU64::new(1);
        let path = std::env::temp_dir().join(format!(
            "home-music-window-{}-{}.toml",
            std::process::id(), SERIAL.fetch_add(1, Ordering::Relaxed),
        ));
        let mut place = bare(80.0, 60.0);
        assert!(save_at(&path, place).unwrap());
        assert!(!save_at(&path, place).unwrap());
        for change in 0..11 {
            match change {
                0 => place.x = 100.0,
                1 => place.mode = Some(0),
                2 => place.width = Some(400.0),
                3 => place.height = Some(100.0),
                4 => place.volume = Some(0.4),
                5 => place.eq_on = Some(true),
                6 => place.eq_open = Some(true),
                7 => place.preamp = Some(-2.0),
                8 => place.bands = Some([1.0; 10]),
                9 => place.mini_position = Some((100.0, 60.0)),
                _ => place.normal_position = Some((-1200.0, 80.0)),
            }
            assert!(save_at(&path, place).unwrap(), "change {change}");
            assert!(!save_at(&path, place).unwrap(), "unchanged {change}");
            assert_eq!(parse(&std::fs::read_to_string(&path).unwrap()), Some(place));
        }
        std::fs::remove_file(path).unwrap();
    }
}
