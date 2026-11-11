mod navidrome;
mod player;
mod queue;
mod window_place;

use std::cell::{Cell, RefCell};
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
    let config = Rc::new(navidrome::ServerConfig::load());
    let queue = Rc::new(RefCell::new(queue::Queue::from_tracks("", Vec::new())));
    let browser = Rc::new(RefCell::new(Browser::empty()));
    let playback_status = if let Some(target) = std::env::args().nth(1) {
        let (title, meta) = track_label(&target);
        *queue.borrow_mut() = queue::Queue::from_path(&target, &title, &meta);
        let _ = queue.borrow_mut().select(0);
        match play_current(&ui, &player, &queue, &config) {
            Ok(()) => config.status_line(),
            Err(err) => err,
        }
    } else {
        match navidrome::load_queue(&config) {
            Ok(library) => {
                *queue.borrow_mut() = library_to_queue(library);
                if queue.borrow().is_empty() {
                    let err = "Navidrome: в библиотеке нет треков".to_string();
                    clear_track(&ui);
                    ui.set_error(err.clone().into());
                    err
                } else {
                    let _ = queue.borrow_mut().select(0);
                    match play_current(&ui, &player, &queue, &config) {
                        Ok(()) => "Navidrome подключён".into(),
                        Err(err) => err,
                    }
                }
            }
            Err(err) => {
                clear_track(&ui);
                ui.set_error(err.clone().into());
                err
            }
        }
    };
    let playback_failed = !ui.get_error().is_empty();
    let library_status = match navidrome::load_catalog(&config) {
        Ok(catalog) => {
            let mut browser = browser.borrow_mut();
            browser.catalog = catalog.clone();
            browser.shown = catalog;
            show_browser(&ui, &mut browser);
            playback_status
        }
        Err(err) => {
            if playback_failed {
                playback_status
            } else {
                ui.set_error(err.clone().into());
                err
            }
        }
    };
    let config_status = Rc::new(library_status);
    ui.set_status(player.borrow().status_or(&config_status).into());
    push_playback(&ui, &player.borrow().snapshot());
    resize(&ui, 0);
    if let Some(place) = window_place::load() {
        ui.window()
            .set_position(slint::LogicalPosition::new(place.x, place.y));
    }

    let ui_play = ui.as_weak();
    let player_play = player.clone();
    let queue_play = queue.clone();
    let config_play = config.clone();
    let status_play = config_status.clone();
    ui.on_play_pause(move || {
        let Some(ui) = ui_play.upgrade() else {
            return;
        };
        if player_play.borrow().is_idle() {
            let _ = play_current(&ui, &player_play, &queue_play, &config_play);
        } else {
            player_play.borrow_mut().toggle();
        }
        let player = player_play.borrow();
        push_playback(&ui, &player.snapshot());
        ui.set_status(player.status_or(&status_play).into());
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

    let ui_prev = ui.as_weak();
    let player_prev = player.clone();
    let queue_prev = queue.clone();
    let config_prev = config.clone();
    let status_prev = config_status.clone();
    ui.on_prev(move || {
        let moved = queue_prev.borrow_mut().prev();
        let Some(ui) = ui_prev.upgrade() else {
            return;
        };
        jump(
            &ui,
            &player_prev,
            &queue_prev,
            &config_prev,
            &status_prev,
            moved,
        );
    });

    let ui_next = ui.as_weak();
    let player_next = player.clone();
    let queue_next = queue.clone();
    let config_next = config.clone();
    let status_next = config_status.clone();
    ui.on_next(move || {
        let moved = queue_next.borrow_mut().next();
        let Some(ui) = ui_next.upgrade() else {
            return;
        };
        jump(
            &ui,
            &player_next,
            &queue_next,
            &config_next,
            &status_next,
            moved,
        );
    });

    let ui_row = ui.as_weak();
    let player_row = player.clone();
    let queue_row = queue.clone();
    let config_row = config.clone();
    let status_row = config_status.clone();
    ui.on_play_row(move |index| {
        if index < 0 {
            return;
        }
        let moved = queue_row.borrow_mut().select(index as usize);
        let Some(ui) = ui_row.upgrade() else {
            return;
        };
        jump(
            &ui,
            &player_row,
            &queue_row,
            &config_row,
            &status_row,
            moved,
        );
    });

    let search_generation = Rc::new(Cell::new(0u64));
    let search_timer = Rc::new(slint::Timer::default());
    let ui_search = ui.as_weak();
    let browser_search = browser.clone();
    let queue_search = queue.clone();
    let config_search = config.clone();
    let generation_search = search_generation.clone();
    let timer_search = search_timer.clone();
    ui.on_search_edited(move |text| {
        let text = text.to_string();
        let ticket = generation_search.get().wrapping_add(1);
        generation_search.set(ticket);
        if text.trim().is_empty() {
            let Some(ui) = ui_search.upgrade() else {
                return;
            };
            apply_search(&ui, &browser_search, &queue_search, &config_search, "");
            return;
        }
        let ui_later = ui_search.clone();
        let browser_later = browser_search.clone();
        let queue_later = queue_search.clone();
        let config_later = config_search.clone();
        let generation_later = generation_search.clone();
        let text_later = text;
        timer_search.start(
            slint::TimerMode::SingleShot,
            Duration::from_millis(300),
            move || {
                if generation_later.get() != ticket {
                    return;
                }
                let Some(ui) = ui_later.upgrade() else {
                    return;
                };
                apply_search(
                    &ui,
                    &browser_later,
                    &queue_later,
                    &config_later,
                    &text_later,
                );
            },
        );
    });

    let ui_artist = ui.as_weak();
    let browser_artist = browser.clone();
    ui.on_pick_artist(move |index| {
        if index < 0 {
            return;
        }
        let Some(ui) = ui_artist.upgrade() else {
            return;
        };
        let id = if index == 0 {
            String::new()
        } else {
            browser_artist
                .borrow()
                .visible_artists
                .get(index as usize - 1)
                .map(|artist| artist.id.clone())
                .unwrap_or_default()
        };
        let mut browser = browser_artist.borrow_mut();
        browser.artist_id = id;
        show_browser(&ui, &mut browser);
    });

    let ui_album = ui.as_weak();
    let browser_album = browser.clone();
    let queue_album = queue.clone();
    let player_album = player.clone();
    let config_album = config.clone();
    let status_album = config_status.clone();
    let generation_album = search_generation.clone();
    ui.on_open_album(move |index| {
        if index < 0 {
            return;
        }
        let album = browser_album
            .borrow()
            .visible_albums
            .get(index as usize)
            .cloned();
        let Some(album) = album else {
            return;
        };
        let Some(ui) = ui_album.upgrade() else {
            return;
        };
        match navidrome::album_tracks(&config_album, &album.id) {
            Ok(library) => {
                generation_album.set(generation_album.get().wrapping_add(1));
                ui.set_search(String::new().into());
                {
                    let mut browser = browser_album.borrow_mut();
                    browser.query.clear();
                    browser.shown = browser.catalog.clone();
                    show_browser(&ui, &mut browser);
                }
                *queue_album.borrow_mut() = library_to_queue(library);
                let moved = queue_album.borrow_mut().select(0);
                jump(
                    &ui,
                    &player_album,
                    &queue_album,
                    &config_album,
                    &status_album,
                    moved,
                );
            }
            Err(err) => {
                ui.set_error(err.clone().into());
                ui.set_status(err.into());
            }
        }
    });

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
    let queue_tick = queue.clone();
    let config_tick = config.clone();
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
            let moved = {
                let mut player = player_tick.borrow_mut();
                let mut queue = queue_tick.borrow_mut();
                take_queue_step(&mut player, &mut queue)
            };
            if moved {
                if let Some(ui) = ui_tick.upgrade() {
                    let _ = play_current(&ui, &player_tick, &queue_tick, &config_tick);
                }
            }
            if let Some(ui) = ui_tick.upgrade() {
                let player = player_tick.borrow();
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

struct Browser {
    catalog: navidrome::Catalog,
    shown: navidrome::Catalog,
    artist_id: String,
    query: String,
    visible_artists: Vec<navidrome::Artist>,
    visible_albums: Vec<navidrome::Album>,
}

impl Browser {
    fn empty() -> Self {
        let empty = navidrome::Catalog {
            artists: Vec::new(),
            albums: Vec::new(),
        };
        Self {
            catalog: empty.clone(),
            shown: empty,
            artist_id: String::new(),
            query: String::new(),
            visible_artists: Vec::new(),
            visible_albums: Vec::new(),
        }
    }

    fn artist_name(&self) -> String {
        self.catalog
            .artists
            .iter()
            .chain(self.shown.artists.iter())
            .find(|artist| artist.id == self.artist_id)
            .map(|artist| artist.name.clone())
            .unwrap_or_default()
    }
}

fn show_browser(ui: &AppWindow, browser: &mut Browser) {
    let name = browser.artist_name();
    browser.visible_albums =
        navidrome::albums_for_artist(&browser.shown.albums, &browser.artist_id, &name);
    browser.visible_artists = browser.shown.artists.clone();
    let mut artists = vec![ArtistItem {
        id: String::new().into(),
        name: "Все".into(),
        current: browser.artist_id.is_empty(),
    }];
    for artist in &browser.visible_artists {
        artists.push(ArtistItem {
            id: artist.id.clone().into(),
            name: artist.name.clone().into(),
            current: artist.id == browser.artist_id,
        });
    }
    let albums = browser
        .visible_albums
        .iter()
        .map(|album| AlbumItem {
            id: album.id.clone().into(),
            title: album.name.clone().into(),
            artist: album.artist.clone().into(),
        })
        .collect::<Vec<_>>();
    ui.set_artists(slint::ModelRc::from(Rc::new(slint::VecModel::from(
        artists,
    ))));
    ui.set_albums(slint::ModelRc::from(Rc::new(slint::VecModel::from(albums))));
}

fn apply_search(
    ui: &AppWindow,
    browser: &RefCell<Browser>,
    queue: &RefCell<queue::Queue>,
    config: &navidrome::ServerConfig,
    query: &str,
) {
    let query = query.trim();
    {
        let mut browser = browser.borrow_mut();
        browser.query = query.to_string();
        if query.is_empty() {
            browser.shown = browser.catalog.clone();
            ui.set_error(String::new().into());
        } else {
            match navidrome::search_catalog(config, query) {
                Ok(found) => {
                    ui.set_error(String::new().into());
                    browser.shown = found;
                }
                Err(err) => {
                    ui.set_error(err.clone().into());
                    ui.set_status(err.into());
                    let folded = query.to_lowercase();
                    browser.shown = browser.catalog.clone();
                    browser
                        .shown
                        .artists
                        .retain(|artist| artist.name.to_lowercase().contains(&folded));
                    browser.shown.albums.retain(|album| {
                        album.name.to_lowercase().contains(&folded)
                            || album.artist.to_lowercase().contains(&folded)
                    });
                }
            }
        }
        show_browser(ui, &mut browser);
    }
    show_queue(ui, &queue.borrow());
}

fn matches_query(item: &queue::Item, query: &str) -> bool {
    let query = query.trim();
    if query.is_empty() {
        return true;
    }
    let query = query.to_lowercase();
    item.title.to_lowercase().contains(&query)
        || item.artist.to_lowercase().contains(&query)
        || item.album.to_lowercase().contains(&query)
}

fn visible_sources(queue: &queue::Queue, query: &str) -> Vec<usize> {
    queue
        .items()
        .iter()
        .enumerate()
        .filter(|(_, item)| matches_query(item, query))
        .map(|(index, _)| index)
        .collect()
}

fn library_to_queue(library: navidrome::LibraryQueue) -> queue::Queue {
    let items = library
        .tracks
        .into_iter()
        .map(|track| queue::Item {
            id: track.id,
            path: String::new(),
            title: track.title,
            artist: track.artist,
            album: track.album,
            meta: track.meta,
            duration_s: track.duration_s,
        })
        .collect();
    queue::Queue::from_tracks(library.name, items)
}

fn play_current(
    ui: &AppWindow,
    player: &RefCell<Player>,
    queue: &RefCell<queue::Queue>,
    config: &navidrome::ServerConfig,
) -> Result<(), String> {
    let (target, cover_id) = {
        let queue = queue.borrow();
        let cover_id = queue
            .current()
            .map(|item| item.id.clone())
            .unwrap_or_default();
        (current_target(&queue, config), cover_id)
    };
    {
        let queue = queue.borrow();
        show_queue(ui, &queue);
    }
    let loaded = match target {
        Some(target) => player.borrow_mut().load(&target),
        None => Err("Нет трека".into()),
    };
    // The previous cover must not stay on the next track when this one has none.
    show_cover(ui, config, &cover_id);
    match loaded {
        Ok(()) => {
            ui.set_error(String::new().into());
            Ok(())
        }
        Err(err) => {
            ui.set_error(err.clone().into());
            Err(err)
        }
    }
}

fn current_target(queue: &queue::Queue, config: &navidrome::ServerConfig) -> Option<String> {
    let item = queue.current()?;
    if !item.id.is_empty() {
        Some(navidrome::stream_url(config, &item.id))
    } else if !item.path.is_empty() {
        Some(item.path.clone())
    } else {
        None
    }
}

fn jump(
    ui: &AppWindow,
    player: &RefCell<Player>,
    queue: &RefCell<queue::Queue>,
    config: &navidrome::ServerConfig,
    status: &str,
    moved: bool,
) {
    if !moved {
        return;
    }
    let _ = play_current(ui, player, queue, config);
    let player = player.borrow();
    push_playback(ui, &player.snapshot());
    ui.set_status(player.status_or(status).into());
}

/// The timer and the end-of-file test share this. A stop leaves the index where it is.
fn take_queue_step(player: &mut Player, queue: &mut queue::Queue) -> bool {
    player.take_natural_end() && queue.next()
}

fn show_queue(ui: &AppWindow, queue: &queue::Queue) {
    let query = ui.get_search().to_string();
    let current = queue.index();
    let rows: Vec<QueueItem> = queue
        .items()
        .iter()
        .enumerate()
        .filter(|(_, item)| matches_query(item, &query))
        .map(|(index, item)| QueueItem {
            source: index as i32,
            number: format!("{}", index + 1).into(),
            title: item.title.clone().into(),
            meta: String::new().into(),
            duration: row_duration(item).into(),
            current: current == Some(index),
        })
        .collect();
    ui.set_queue(slint::ModelRc::from(Rc::new(slint::VecModel::from(rows))));
    let title = if queue.name().is_empty() {
        "Очередь"
    } else {
        queue.name()
    };
    ui.set_queue_title(title.into());
    ui.set_queue_total(queue.total_label().into());
    let Some(item) = queue.current() else {
        return;
    };
    ui.set_track_title(item.title.clone().into());
    ui.set_track_artist(item.artist.clone().into());
    ui.set_track_album(item.album.clone().into());
    ui.set_track_meta(item.meta.clone().into());
}

fn show_cover(ui: &AppWindow, config: &navidrome::ServerConfig, song_id: &str) {
    if song_id.is_empty() {
        ui.set_has_cover(false);
        return;
    }
    match navidrome::cover_art(config, song_id)
        .as_deref()
        .and_then(cover_image)
    {
        Some(image) => {
            ui.set_cover(image);
            ui.set_has_cover(true);
        }
        None => ui.set_has_cover(false),
    }
}

fn clear_track(ui: &AppWindow) {
    ui.set_track_title("Нет трека".into());
    ui.set_track_artist(String::new().into());
    ui.set_track_album(String::new().into());
    ui.set_track_meta(String::new().into());
    ui.set_has_cover(false);
    ui.set_queue(slint::ModelRc::from(Rc::new(slint::VecModel::from(Vec::<
        QueueItem,
    >::new(
    )))));
    ui.set_queue_title("Очередь".into());
    ui.set_queue_total(String::new().into());
}

fn row_duration(item: &queue::Item) -> String {
    if item.duration_s == 0 && item.id.is_empty() {
        "—".into()
    } else {
        clock(item.duration_s as f32)
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

    fn tone_wav(seconds: f32) -> Vec<u8> {
        let rate = 44_100u32;
        let samples = (rate as f32 * seconds) as u32;
        let data_bytes = samples * 2;
        let mut bytes = Vec::with_capacity(44 + data_bytes as usize);
        bytes.extend(b"RIFF");
        bytes.extend((36 + data_bytes).to_le_bytes());
        bytes.extend(b"WAVEfmt ");
        bytes.extend(16u32.to_le_bytes());
        bytes.extend(1u16.to_le_bytes());
        bytes.extend(1u16.to_le_bytes());
        bytes.extend(rate.to_le_bytes());
        bytes.extend((rate * 2).to_le_bytes());
        bytes.extend(2u16.to_le_bytes());
        bytes.extend(16u16.to_le_bytes());
        bytes.extend(b"data");
        bytes.extend(data_bytes.to_le_bytes());
        bytes.resize(bytes.len() + data_bytes as usize, 0);
        bytes
    }

    #[test]
    fn end_of_file_moves_the_queue_index() {
        let dir = std::env::temp_dir();
        let first = dir.join("homemusic queue a.wav");
        let second = dir.join("homemusic queue b.wav");
        std::fs::write(&first, tone_wav(0.45)).unwrap();
        std::fs::write(&second, tone_wav(0.45)).unwrap();
        let mut queue = queue::Queue::from_tracks(
            "test",
            vec![
                queue::Item::file(&first.to_string_lossy(), "a"),
                queue::Item::file(&second.to_string_lossy(), "b"),
            ],
        );
        assert!(queue.select(0));
        let mut player = player::Player::open(player::PlayerOptions { null_audio: true });
        assert_eq!(player.status_or("ok"), "ok");
        player.load(queue.current().unwrap().path.as_str()).unwrap();
        let start = std::time::Instant::now();
        let mut moved = false;
        while start.elapsed() < std::time::Duration::from_secs(5) {
            if take_queue_step(&mut player, &mut queue) {
                player.load(queue.current().unwrap().path.as_str()).unwrap();
                moved = true;
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(30));
        }
        assert!(moved, "the next file did not start");
        assert_eq!(queue.index(), Some(1));
        assert_eq!(queue.current().unwrap().title, "b");
        assert!(!player.take_natural_end());
    }

    #[test]
    fn search_hides_rows_and_keeps_the_queue_index() {
        let queue = queue::Queue::from_tracks(
            "Любимое",
            vec![
                queue::Item {
                    id: "a".into(),
                    path: String::new(),
                    title: "Visions".into(),
                    artist: "Damned Anthem".into(),
                    album: "Visions".into(),
                    meta: "MP3".into(),
                    duration_s: 200,
                },
                queue::Item {
                    id: "b".into(),
                    path: String::new(),
                    title: "За речкою".into(),
                    artist: "FOLKBEAT".into(),
                    album: "За речкою".into(),
                    meta: "MP3".into(),
                    duration_s: 197,
                },
            ],
        );
        assert_eq!(visible_sources(&queue, ""), vec![0, 1]);
        assert_eq!(visible_sources(&queue, "речк"), vec![1]);
        assert_eq!(visible_sources(&queue, "anthem"), vec![0]);
        assert!(visible_sources(&queue, "нет такого").is_empty());
    }
}
