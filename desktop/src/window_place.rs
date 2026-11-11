//! Where the window was last left on the screen. Step 7 also stores the mode and the size.

use std::path::PathBuf;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Place {
    pub x: f32,
    pub y: f32,
}

pub fn path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("window-place.toml")
}

pub fn load() -> Option<Place> {
    let text = std::fs::read_to_string(path()).ok()?;
    parse(&text)
}

pub fn save(place: Place) {
    let body = format!("x = {}\ny = {}\n", place.x, place.y);
    let _ = std::fs::write(path(), body);
}

pub fn parse(text: &str) -> Option<Place> {
    let mut x = None;
    let mut y = None;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else { continue };
        let value = value.trim().trim_matches('"');
        let Ok(value) = value.parse::<f32>() else { continue };
        match key.trim() {
            "x" => x = Some(value),
            "y" => y = Some(value),
            _ => {}
        }
    }
    Some(Place { x: x?, y: y? })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_both_coordinates() {
        assert_eq!(parse("x = 120.5\ny = 40\n"), Some(Place { x: 120.5, y: 40.0 }));
    }

    #[test]
    fn rejects_a_file_missing_one_axis() {
        assert_eq!(parse("x = 10\n"), None);
    }
}
