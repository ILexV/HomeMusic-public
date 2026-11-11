mod navidrome;
mod player;

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use player::SilentPlayer;
use slint::ComponentHandle;

slint::include_modules!();

fn main() -> Result<(), slint::PlatformError> {
    let ui = AppWindow::new()?;
    let player = Rc::new(RefCell::new(SilentPlayer::demo()));
    let config = navidrome::ServerConfig::load();

    ui.set_track_title("Каркас".into());
    ui.set_track_artist("HomeMusic".into());
    ui.set_track_album("демо, без звука".into());
    ui.set_track_meta("MP3 · пример".into());
    ui.set_status(config.status_line().into());
    ui.set_queue(slint::ModelRc::from(Rc::new(demo_queue())));
    ui.set_albums(slint::ModelRc::from(Rc::new(demo_albums())));
    push_playback(&ui, &player.borrow().state);
    resize(&ui, 0);

    let ui_play = ui.as_weak();
    let player_play = player.clone();
    ui.on_play_pause(move || {
        player_play.borrow_mut().toggle();
        if let Some(ui) = ui_play.upgrade() {
            push_playback(&ui, &player_play.borrow().state);
        }
    });

    let ui_stop = ui.as_weak();
    let player_stop = player.clone();
    ui.on_stop(move || {
        player_stop.borrow_mut().stop();
        if let Some(ui) = ui_stop.upgrade() {
            push_playback(&ui, &player_stop.borrow().state);
        }
    });

    let ui_seek = ui.as_weak();
    let player_seek = player.clone();
    ui.on_seek(move |ratio| {
        player_seek.borrow_mut().seek_ratio(ratio);
        if let Some(ui) = ui_seek.upgrade() {
            push_playback(&ui, &player_seek.borrow().state);
        }
    });

    let ui_volume = ui.as_weak();
    let player_volume = player.clone();
    ui.on_set_volume(move |volume| {
        player_volume.borrow_mut().set_volume(volume);
        if let Some(ui) = ui_volume.upgrade() {
            push_playback(&ui, &player_volume.borrow().state);
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
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(200), move || {
        let mut player = player_tick.borrow_mut();
        let was_playing = player.state.playing;
        player.tick(0.2);
        if was_playing {
            if let Some(ui) = ui_tick.upgrade() {
                push_playback(&ui, &player.state);
            }
        }
    });

    ui.run()
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

fn clock(seconds: f32) -> String {
    let total = seconds.max(0.0) as u32;
    format!("{}:{:02}", total / 60, total % 60)
}

fn demo_queue() -> slint::VecModel<QueueItem> {
    slint::VecModel::from(vec![
        QueueItem {
            title: "1. Каркас".into(),
            meta: "демо, без звука".into(),
            duration: "3:20".into(),
            current: true,
        },
        QueueItem {
            title: "2. Очередь с сервера".into(),
            meta: "появится на шаге 5".into(),
            duration: "4:01".into(),
            current: false,
        },
        QueueItem {
            title: "3. Следующий трек".into(),
            meta: "кнопки пока ничего не переключают".into(),
            duration: "2:48".into(),
            current: false,
        },
    ])
}

fn demo_albums() -> slint::VecModel<AlbumItem> {
    slint::VecModel::from(vec![
        AlbumItem { title: "Альбом 1".into(), artist: "пример".into() },
        AlbumItem { title: "Альбом 2".into(), artist: "пример".into() },
        AlbumItem { title: "Альбом 3".into(), artist: "пример".into() },
    ])
}
