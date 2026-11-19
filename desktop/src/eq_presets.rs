//! Local named curves are independent of the active EQ/bypass state in window-place.toml.
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::rc::Rc;

use slint::ComponentHandle;

use crate::{eq, playback_session, AppWindow, Player};

type Curve = (f32, [f32; 10]);
type Curves = BTreeMap<String, Curve>;

// From 31 Hz to 16 kHz. Lower preamp on boosted curves to leave headroom.
const FACTORY: &[(&str, Curve)] = &[
    ("Ровный", (0., [0.; 10])),
    ("Усиление баса", (-6., [5., 5., 3., 1., 0., 0., 0., 0., 0., 0.])),
    ("Вокал", (-4., [-3., -2., -1., 0., 2., 3., 3., 1., 0., -1.])),
    ("Рок", (-5., [3., 4., 2., 0., -2., -1., 1., 3., 3., 2.])),
    ("Поп", (-4., [-1., 1., 2., 1., 0., 2., 3., 2., 0., -1.])),
    ("Джаз", (-4., [2., 2., 1., 0., -1., 0., 1., 2., 3., 2.])),
    ("Классика", (-3., [1., 1., 0., 0., -1., -1., 0., 1., 2., 2.])),
    ("Электроника", (-5., [4., 3., 1., 0., -2., -1., 0., 2., 3., 3.])),
];

fn preset_at(curves: &Curves, index: usize) -> Option<(&str, Curve)> {
    if let Some(&preset) = FACTORY.get(index) {
        return Some(preset);
    }
    curves.iter().nth(index - FACTORY.len()).map(|(name, &curve)| (name.as_str(), curve))
}

fn read_curves(path: &Path) -> Result<Curves, String> {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(Curves::new()),
        Err(error) => return Err(format!("Не удалось прочитать eq-presets.json: {error}")),
    };
    let curves: Curves = serde_json::from_slice(&bytes)
        .map_err(|error| format!("Неверный формат eq-presets.json: {error}"))?;
    if curves.iter().any(|(name, (preamp, bands))| {
        name.trim().is_empty() || name != name.trim() || name.chars().any(char::is_control)
            || std::iter::once(preamp).chain(bands).any(|db| !(eq::MIN_DB..=eq::MAX_DB).contains(db))
    }) {
        return Err("Недопустимое имя или усиление в eq-presets.json".into());
    }
    Ok(curves)
}

fn save_curve(path: &Path, name: &str, current: eq::Settings) -> Result<(Curves, bool), String> {
    let name = name.trim();
    if name.is_empty() || name.chars().any(char::is_control) {
        return Err("Введите имя пресета в одну строку".into());
    }
    // Re-read on explicit save so an unreadable file isn't silently replaced by an empty bank.
    let mut curves = read_curves(path)?;
    let replaced = curves.insert(name.to_owned(), (current.preamp, current.bands)).is_some();
    let bytes = serde_json::to_vec_pretty(&curves).map_err(|error| error.to_string())?;
    playback_session::atomic_write(path, &bytes)
        .map_err(|error| format!("Не удалось сохранить eq-presets.json: {error}"))?;
    Ok((curves, replaced))
}

fn recall(curve: Curve, player: &mut Player) -> eq::Settings {
    let (preamp, bands) = curve;
    player.restore_eq(eq::Settings { preamp, bands, ..player.eq() });
    player.eq()
}

fn show_names(ui: &AppWindow, curves: &Curves) {
    let mut names = Vec::with_capacity(FACTORY.len() + curves.len());
    names.extend(FACTORY.iter().map(|&(name, _)| slint::SharedString::from(name)));
    names.extend(curves.keys().map(|name| slint::format!("Мой · {}", name)));
    ui.set_eq_preset_names(slint::ModelRc::new(slint::VecModel::from(names)));
}

pub(crate) fn connect(ui: &AppWindow, player: &Rc<RefCell<Player>>, path: PathBuf) {
    let curves = read_curves(&path).unwrap_or_else(|message| {
        ui.set_eq_preset_status(message.into());
        Curves::new()
    });
    show_names(ui, &curves);
    let curves = Rc::new(RefCell::new(curves));

    let weak = ui.as_weak();
    let player_save = player.clone();
    let saved = curves.clone();
    ui.on_save_eq_preset(move |name| {
        let Some(ui) = weak.upgrade() else { return };
        let name = name.trim();
        match save_curve(&path, name, player_save.borrow().eq()) {
            Ok((next, replaced)) => {
                let index = FACTORY.len() + next.keys().position(|key| key == name).unwrap();
                if !saved.borrow().keys().eq(next.keys()) {
                    show_names(&ui, &next);
                }
                *saved.borrow_mut() = next;
                ui.set_eq_preset_index(index as i32);
                ui.set_eq_preset_name(name.into());
                ui.set_eq_preset_status(if replaced { "Пресет обновлён" } else { "Пресет сохранён" }.into());
            }
            Err(message) => ui.set_eq_preset_status(message.into()),
        }
    });

    let weak = ui.as_weak();
    let player = player.clone();
    ui.on_apply_eq_preset(move |index| {
        let Some(ui) = weak.upgrade() else { return };
        let curves = curves.borrow();
        let Ok(index) = usize::try_from(index) else { return };
        let Some((name, curve)) = preset_at(&curves, index) else {
            ui.set_eq_preset_status("Пресет не найден".into());
            return;
        };
        let current = recall(curve, &mut player.borrow_mut());
        ui.set_preamp(current.preamp);
        crate::show_bands(&ui, &current.bands);
        ui.set_eq_preset_index(index as i32);
        // Save edits as a new personal curve instead of overwriting a namesake.
        ui.set_eq_preset_name(if index < FACTORY.len() { "" } else { name }.into());
        ui.set_eq_preset_status(if index < FACTORY.len() { "Встроенный пресет" } else { "Мой пресет загружен" }.into());
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};

    struct Temp(PathBuf);
    impl Temp {
        fn new() -> Self {
            static SERIAL: AtomicU32 = AtomicU32::new(0);
            let path = std::env::temp_dir().join(format!("homemusic-eq-presets-{}-{}", std::process::id(), SERIAL.fetch_add(1, Ordering::Relaxed)));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn path(&self) -> PathBuf { self.0.join("eq-presets.json") }
    }
    impl Drop for Temp {
        fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); }
    }

    #[test]
    fn names_round_trip_replace_only_the_same_curve_and_keep_bypass() {
        let temp = Temp::new();
        let path = temp.path();
        assert!(read_curves(&path).unwrap().is_empty());
        let curve = eq::Settings { enabled: true, preamp: -1.75, bands: std::array::from_fn(|i| i as f32 - 4.25) };
        assert!(!save_curve(&path, "  Наушники  ", curve).unwrap().1);
        assert!(!save_curve(&path, "Колонки", eq::Settings::default()).unwrap().1);
        let loaded = read_curves(&path).unwrap();
        let mut player = Player::open(crate::player::PlayerOptions { null_audio: true });
        player.set_eq_enabled(true);
        assert_eq!(recall(loaded["Наушники"], &mut player), curve);
        player.set_eq_enabled(false);
        assert_eq!(recall(loaded["Наушники"], &mut player), eq::Settings { enabled: false, ..curve });
        let replacement = eq::Settings { preamp: 2.0, ..curve };
        assert!(save_curve(&path, "Наушники", replacement).unwrap().1);
        let loaded = read_curves(&path).unwrap();
        assert_eq!(loaded.len(), 2);
        player.set_eq_enabled(true);
        assert_eq!(recall(loaded["Наушники"], &mut player), replacement);
        assert_eq!(recall(loaded["Колонки"], &mut player), eq::Settings { enabled: true, ..eq::Settings::default() });
        player.reset_eq();
        assert!(player.eq().enabled);
        assert!(preset_at(&loaded, FACTORY.len() + loaded.len()).is_none());
    }

    #[test]
    fn personal_namesake_and_factory_curve_remain_independent() {
        let temp = Temp::new();
        let path = temp.path();
        let bank = read_curves(&path).unwrap();
        let rock = FACTORY.iter().position(|(name, _)| *name == "Рок").unwrap();
        let (_, factory) = preset_at(&bank, rock).unwrap();
        assert!(!path.exists());
        let custom = eq::Settings { preamp: -1.5, bands: [-2.; 10], enabled: false };
        save_curve(&path, "Рок", custom).unwrap();
        let bank = read_curves(&path).unwrap();
        assert_eq!(bank.len(), 1); // Only personal curves belong on disk.
        assert_eq!(preset_at(&bank, FACTORY.len()), Some(("Рок", (custom.preamp, custom.bands))));
        assert_eq!(preset_at(&bank, rock), Some(("Рок", factory)));
        let mut player = Player::open(crate::player::PlayerOptions { null_audio: true });
        let loaded = recall(factory, &mut player);
        assert!(!loaded.enabled && loaded.preamp < 0. && loaded.bands[1] > loaded.bands[4]);
        assert_eq!(recall((custom.preamp, custom.bands), &mut player), custom);
    }

    #[test]
    fn invalid_names_and_unreadable_banks_never_overwrite_saved_data() {
        let temp = Temp::new();
        let path = temp.path();
        for name in [" \t ", "A\nB"] {
            assert!(save_curve(&path, name, eq::Settings::default()).is_err());
        }
        assert!(!path.exists());
        for bad in ["corrupt", r#"{"A":[1,[0,0]]}"#, r#"{"A":[19,[0,0,0,0,0,0,0,0,0,0]]}"#] {
            fs::write(&path, bad).unwrap();
            assert!(save_curve(&path, "B", eq::Settings::default()).is_err());
            assert_eq!(fs::read_to_string(&path).unwrap(), bad);
        }
        assert!(save_curve(&path.join("not-a-directory"), "B", eq::Settings::default()).is_err());
    }
}
