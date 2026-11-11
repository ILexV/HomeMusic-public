mod navidrome;
mod player;
mod window_place;

use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};

use player::{Player, PlayerOptions};
use slint::ComponentHandle;

slint::include_modules!();

fn main() -> Result<(), slint::PlatformError> {
    let ui = AppWindow::new()?;
    let player = Rc::new(RefCell::new(Player::open(PlayerOptions {
        null_audio: false,
    })));
    let config = navidrome::ServerConfig::load();
    let mut queue = Vec::new();
    let library_status = if let Some(target) = std::env::args().nth(1) {
        let (title, meta) = track_label(&target);
        ui.set_track_title(title.clone().into());
        ui.set_track_artist(String::new().into());
        ui.set_track_album(String::new().into());
        ui.set_track_meta(meta.into());
        queue.push(playing_row("1", &title, "—"));
        if let Err(err) = player.borrow_mut().load(&target) {
            ui.set_error(err.clone().into());
            err
        } else {
            config.status_line()
        }
    } else {
        match navidrome::first_track(&config) {
            Ok(track) => {
                ui.set_track_title(track.title.clone().into());
                ui.set_track_artist(track.artist.into());
                ui.set_track_album(track.album.into());
                ui.set_track_meta(track.meta.into());
                queue.push(playing_row(
                    "1",
                    &track.title,
                    &clock(track.duration_s as f32),
                ));
                if let Some(bytes) = navidrome::cover_art(&config, &track.id) {
                    if let Some(image) = cover_image(&bytes) {
                        ui.set_cover(image);
                        ui.set_has_cover(true);
                    }
                }
                match player.borrow_mut().load(&track.stream_url) {
                    Ok(()) => "Navidrome подключён".into(),
                    Err(err) => {
                        ui.set_error(err.clone().into());
                        err
                    }
                }
            }
            Err(err) => {
                ui.set_track_title("Нет трека".into());
                ui.set_track_artist(String::new().into());
                ui.set_track_album(String::new().into());
                ui.set_track_meta(String::new().into());
                ui.set_error(err.clone().into());
                err
            }
        }
    };
    let config_status = Rc::new(library_status);
    ui.set_status(player.borrow().status_or(&config_status).into());
    ui.set_queue(slint::ModelRc::from(Rc::new(slint::VecModel::from(queue))));
    ui.set_albums(slint::ModelRc::from(Rc::new(demo_albums())));
    push_playback(&ui, &player.borrow().snapshot());
    resize(&ui, 0);
    if let Some(place) = window_place::load() {
        ui.window()
            .set_position(slint::LogicalPosition::new(place.x, place.y));
    }

    let ui_play = ui.as_weak();
    let player_play = player.clone();
    let status_play = config_status.clone();
    ui.on_play_pause(move || {
        player_play.borrow_mut().toggle();
        if let Some(ui) = ui_play.upgrade() {
            let player = player_play.borrow();
            push_playback(&ui, &player.snapshot());
            ui.set_status(player.status_or(&status_play).into());
        }
    });

    let ui_stop = ui.as_weak();
    let player_stop = player.clone();
    let status_stop = config_status.clone();
    ui.on_stop(move || {
        player_stop.borrow_mut().stop();
        if let Some(ui) = ui_stop.upgrade() {
            let player = player_stop.borrow();
            push_playback(&ui, &player.snapshot());
            ui.set_status(player.status_or(&status_stop).into());
        }
    });

    let ui_seek = ui.as_weak();
    let player_seek = player.clone();
    let status_seek = config_status.clone();
    ui.on_seek(move |ratio| {
        player_seek.borrow_mut().seek_ratio(ratio);
        if let Some(ui) = ui_seek.upgrade() {
            let player = player_seek.borrow();
            push_playback(&ui, &player.snapshot());
            ui.set_status(player.status_or(&status_seek).into());
        }
    });

    let ui_volume = ui.as_weak();
    let player_volume = player.clone();
    let status_volume = config_status.clone();
    ui.on_set_volume(move |volume| {
        player_volume.borrow_mut().set_volume(volume);
        if let Some(ui) = ui_volume.upgrade() {
            let player = player_volume.borrow();
            push_playback(&ui, &player.snapshot());
            ui.set_status(player.status_or(&status_volume).into());
        }
    });

    // Previous and next do nothing until a real queue exists.
    ui.on_prev(|| {});
    ui.on_next(|| {});

    let ui_mode = ui.as_weak();
    ui.on_set_mode(move |mode| {
        let mode = mode.clamp(0, 2);
        if let Some(ui) = ui_mode.upgrade() {
            ui.set_mode(match mode {
                0 => Mode::Mini,
                1 => Mode::Medium,
                _ => Mode::Large,
            });
            resize(&ui, mode);
        }
    });

    let ui_tick = ui.as_weak();
    let player_tick = player.clone();
    let status_tick = config_status.clone();
    let place_watch = Rc::new(RefCell::new(PlaceWatch {
        started: Instant::now(),
        last: None,
    }));
    let watch_tick = place_watch.clone();
    let timer = slint::Timer::default();
    timer.start(
        slint::TimerMode::Repeated,
        Duration::from_millis(200),
        move || {
            let player = player_tick.borrow();
            if let Some(ui) = ui_tick.upgrade() {
                push_playback(&ui, &player.snapshot());
                ui.set_status(player.status_or(&status_tick).into());
                remember_place(&ui, &mut watch_tick.borrow_mut());
            }
        },
    );

    let ui_close = ui.as_weak();
    ui.window().on_close_requested(move || {
        if let Some(ui) = ui_close.upgrade() {
            save_place(&ui);
        }
        slint::CloseRequestResponse::HideWindow
    });

    ui.run()
}

struct PlaceWatch {
    started: Instant,
    last: Option<(i32, i32)>,
}

fn remember_place(ui: &AppWindow, watch: &mut PlaceWatch) {
    // The first moments report 0,0 before the window manager applies the restored position.
    if watch.started.elapsed() < Duration::from_millis(700) {
        return;
    }
    let pos = ui.window().position();
    let key = (pos.x, pos.y);
    if watch.last == Some(key) || (key == (0, 0) && watch.last.is_none()) {
        return;
    }
    watch.last = Some(key);
    save_place(ui);
}

fn save_place(ui: &AppWindow) {
    let scale = ui.window().scale_factor();
    if scale <= 0.0 {
        return;
    }
    let pos = ui.window().position().to_logical(scale);
    window_place::save(window_place::Place { x: pos.x, y: pos.y });
}

fn push_playback(ui: &AppWindow, state: &player::PlaybackState) {
    ui.set_playing(state.playing);
    ui.set_position(state.position_s);
    ui.set_duration(state.duration_s);
    ui.set_volume(state.volume);
    ui.set_time_label(clock(state.position_s).into());
    ui.set_duration_label(clock(state.duration_s).into());
}

fn resize(ui: &AppWindow, mode: i32) {
    let (width, height) = match mode {
        0 => (520.0, 132.0),
        1 => (460.0, 640.0),
        _ => (1040.0, 680.0),
    };
    ui.window().set_size(slint::LogicalSize::new(width, height));
}

fn track_label(target: &str) -> (String, String) {
    let tail = target.rsplit(['/', '\\']).next().unwrap_or(target);
    let tail = tail.split('?').next().unwrap_or(tail);
    let title = if tail.is_empty() { target } else { tail };
    let ext = title
        .rsplit_once('.')
        .map(|(_, ext)| ext.to_ascii_uppercase())
        .unwrap_or_default();
    let meta =
        if !ext.is_empty() && ext.len() <= 5 && ext.chars().all(|ch| ch.is_ascii_alphanumeric()) {
            ext
        } else {
            String::new()
        };
    (title.to_string(), meta)
}

fn clock(seconds: f32) -> String {
    let total = seconds.max(0.0) as u32;
    format!("{}:{:02}", total / 60, total % 60)
}

fn playing_row(number: &str, title: &str, duration: &str) -> QueueItem {
    QueueItem {
        number: number.into(),
        title: title.into(),
        meta: String::new().into(),
        duration: duration.into(),
        current: true,
    }
}

fn cover_image(bytes: &[u8]) -> Option<slint::Image> {
    let image = image::load_from_memory(bytes).ok()?.into_rgba8();
    let (width, height) = image.dimensions();
    if width == 0 || height == 0 {
        return None;
    }
    let buffer = slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(
        image.as_raw(),
        width,
        height,
    );
    Some(slint::Image::from_rgba8(buffer))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_a_png_cover() {
        use image::ImageEncoder;
        let mut bytes = Vec::new();
        image::codecs::png::PngEncoder::new(&mut bytes)
            .write_image(&[10, 20, 30, 255], 1, 1, image::ExtendedColorType::Rgba8)
            .unwrap();
        let image = cover_image(&bytes).expect("png");
        let pixels = image.to_rgba8().expect("pixels");
        assert_eq!((pixels.width(), pixels.height()), (1, 1));
    }

    #[test]
    fn a_json_error_is_not_a_cover() {
        assert!(cover_image(br#"{"status":"failed"}"#).is_none());
    }
}

fn demo_albums() -> slint::VecModel<AlbumItem> {
    slint::VecModel::from(vec![
        AlbumItem {
            title: "Альбом 1".into(),
            artist: "пример".into(),
        },
        AlbumItem {
            title: "Альбом 2".into(),
            artist: "пример".into(),
        },
        AlbumItem {
            title: "Альбом 3".into(),
            artist: "пример".into(),
        },
    ])
}
