#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod commands;
mod eq;
mod hub;
mod media_keys;
mod navidrome;
mod paths;
mod player;
mod queue;
mod wave;
mod window_place;

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use player::{Player, PlayerOptions};
use slint::ComponentHandle;
use slint::Model;

slint::include_modules!();

enum HubEvent {
    Marks(hub::Marks),
    Note(String),
    Hits {
        ticket: u64,
        hits: Vec<hub::CatalogHit>,
    },
    Miss {
        ticket: u64,
        message: String,
    },
    SongStar {
        id: String,
        starred: bool,
    },
    AlbumStar {
        id: String,
        starred: bool,
    },
    SongDislike {
        id: String,
        disliked: bool,
    },
    Download {
        reference: String,
        message: String,
        failed: bool,
    },
}

struct HubState {
    hub: Arc<hub::Hub>,
    marks: RefCell<hub::Marks>,
    hits: RefCell<Vec<hub::CatalogHit>>,
    album_id: RefCell<String>,
    catalog_open: Cell<bool>,
    events: Arc<Mutex<Vec<HubEvent>>>,
    hub_note: RefCell<String>,
    search_generation: Rc<Cell<u64>>,
}

impl HubState {
    fn start(config: &navidrome::ServerConfig) -> Self {
        let hub = Arc::new(hub::Hub::from_config(config));
        let events = Arc::new(Mutex::new(Vec::new()));
        let client = Arc::clone(&hub);
        let pending = Arc::clone(&events);
        std::thread::spawn(move || {
            if let Ok(marks) = client.load_marks() {
                push_event(&pending, HubEvent::Marks(marks));
            }
        });
        Self {
            hub,
            marks: RefCell::new(hub::Marks::default()),
            hits: RefCell::new(Vec::new()),
            album_id: RefCell::new(String::new()),
            catalog_open: Cell::new(false),
            events,
            hub_note: RefCell::new(String::new()),
            search_generation: Rc::new(Cell::new(0)),
        }
    }
}

fn push_event(events: &Mutex<Vec<HubEvent>>, event: HubEvent) {
    events
        .lock()
        .unwrap_or_else(|poison| poison.into_inner())
        .push(event);
}

fn take_events(events: &Mutex<Vec<HubEvent>>) -> Vec<HubEvent> {
    let mut guard = events.lock().unwrap_or_else(|poison| poison.into_inner());
    std::mem::take(&mut *guard)
}

fn main() -> Result<(), slint::PlatformError> {
    let ui = AppWindow::new()?;
    let player = Rc::new(RefCell::new(Player::open(PlayerOptions {
        null_audio: false,
    })));
    let session = window_place::load();
    if let Some(place) = session.as_ref() {
        if let Some(volume) = place.volume {
            player.borrow_mut().set_volume(volume);
        }
        player.borrow_mut().restore_eq(eq::Settings {
            enabled: place.eq_on.unwrap_or(false),
            preamp: place.preamp.unwrap_or(0.0),
            bands: place.bands.unwrap_or([0.0; 10]),
        });
    }
    show_bands(&ui, &player.borrow().eq().bands);
    ui.set_preamp(player.borrow().eq().preamp);
    ui.set_eq_on(player.borrow().eq().enabled);
    let waves = wave::WaveHub::shared();
    let config = Rc::new(navidrome::ServerConfig::load());
    let hub_state = Rc::new(HubState::start(&config));
    let queue = Rc::new(RefCell::new(queue::Queue::from_tracks("", Vec::new())));
    let browser = Rc::new(RefCell::new(Browser::empty()));
    let playback_status = if let Some(target) = std::env::args().nth(1) {
        let (title, meta) = track_label(&target);
        *queue.borrow_mut() = queue::Queue::from_path(&target, &title, &meta);
        let _ = queue.borrow_mut().select(0);
        match play_current(&ui, &player, &queue, &config, &waves, &hub_state) {
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
                    match play_current(&ui, &player, &queue, &config, &waves, &hub_state) {
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
            show_browser(&ui, &mut browser, &hub_state.marks.borrow());
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
    show_status(&ui, &player.borrow(), &hub_state, &config_status);
    push_playback(&ui, &player.borrow().snapshot());
    apply_session(&ui, session);

    let ui_play = ui.as_weak();
    let player_play = player.clone();
    let queue_play = queue.clone();
    let config_play = config.clone();
    let status_play = config_status.clone();
    let waves_play = waves.clone();
    let hub_play = hub_state.clone();
    ui.on_play_pause(move || {
        let Some(ui) = ui_play.upgrade() else {
            return;
        };
        if player_play.borrow().is_idle() {
            let _ = play_current(
                &ui,
                &player_play,
                &queue_play,
                &config_play,
                &waves_play,
                &hub_play,
            );
        } else {
            player_play.borrow_mut().toggle();
        }
        let player = player_play.borrow();
        push_playback(&ui, &player.snapshot());
        show_status(&ui, &player, &hub_play, &status_play);
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

    let ui_eq = ui.as_weak();
    ui.on_toggle_eq(move || {
        if let Some(ui) = ui_eq.upgrade() {
            set_eq_open(&ui, !ui.get_eq_open());
        }
    });
    let ui_eq_on = ui.as_weak();
    let player_eq_on = player.clone();
    ui.on_set_eq_enabled(move |enabled| {
        player_eq_on.borrow_mut().set_eq_enabled(enabled);
        if let Some(ui) = ui_eq_on.upgrade() {
            ui.set_eq_on(player_eq_on.borrow().eq().enabled);
        }
    });
    let ui_preamp = ui.as_weak();
    let player_preamp = player.clone();
    ui.on_set_preamp(move |value| {
        player_preamp.borrow_mut().set_preamp(value);
        if let Some(ui) = ui_preamp.upgrade() {
            ui.set_preamp(player_preamp.borrow().eq().preamp);
        }
    });
    let ui_band = ui.as_weak();
    let player_band = player.clone();
    ui.on_set_band(move |index, value| {
        if index < 0 {
            return;
        }
        let index = index as usize;
        player_band.borrow_mut().set_band(index, value);
        let Some(gain) = player_band.borrow().eq().bands.get(index).copied() else {
            return;
        };
        if let Some(ui) = ui_band.upgrade() {
            set_band_row(&ui, index, gain);
        }
    });
    let ui_reset = ui.as_weak();
    let player_reset = player.clone();
    ui.on_reset_eq(move || {
        player_reset.borrow_mut().reset_eq();
        if let Some(ui) = ui_reset.upgrade() {
            let eq = player_reset.borrow().eq();
            ui.set_preamp(eq.preamp);
            show_bands(&ui, &eq.bands);
        }
    });

    let ui_prev = ui.as_weak();
    let player_prev = player.clone();
    let queue_prev = queue.clone();
    let config_prev = config.clone();
    let status_prev = config_status.clone();
    let waves_prev = waves.clone();
    let hub_prev = hub_state.clone();
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
            &waves_prev,
            &hub_prev,
            moved,
        );
    });

    let ui_next = ui.as_weak();
    let player_next = player.clone();
    let queue_next = queue.clone();
    let config_next = config.clone();
    let status_next = config_status.clone();
    let waves_next = waves.clone();
    let hub_next = hub_state.clone();
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
            &waves_next,
            &hub_next,
            moved,
        );
    });

    let ui_row = ui.as_weak();
    let player_row = player.clone();
    let queue_row = queue.clone();
    let config_row = config.clone();
    let status_row = config_status.clone();
    let waves_row = waves.clone();
    let hub_row = hub_state.clone();
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
            &waves_row,
            &hub_row,
            moved,
        );
    });

    let search_generation = hub_state.search_generation.clone();
    let search_timer = Rc::new(slint::Timer::default());
    let ui_search = ui.as_weak();
    let browser_search = browser.clone();
    let queue_search = queue.clone();
    let config_search = config.clone();
    let generation_search = search_generation.clone();
    let timer_search = search_timer.clone();
    let hub_search = hub_state.clone();
    ui.on_search_edited(move |text| {
        let text = text.to_string();
        let ticket = generation_search.get().wrapping_add(1);
        generation_search.set(ticket);
        let Some(ui) = ui_search.upgrade() else {
            return;
        };
        if hub_search.catalog_open.get() {
            if text.trim().is_empty() {
                clear_catalog(&ui, &hub_search);
                show_queue(&ui, &queue_search.borrow(), &hub_search);
            } else {
                let ui_later = ui_search.clone();
                let hub_later = hub_search.clone();
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
                        if hub_later.catalog_open.get() {
                            begin_catalog_query(&ui, &hub_later, ticket, &text_later);
                        }
                    },
                );
            }
            return;
        }
        if text.trim().is_empty() {
            apply_search(
                &ui,
                &browser_search,
                &queue_search,
                &config_search,
                &hub_search,
                "",
            );
            return;
        }
        let ui_later = ui_search.clone();
        let browser_later = browser_search.clone();
        let queue_later = queue_search.clone();
        let config_later = config_search.clone();
        let generation_later = generation_search.clone();
        let hub_later = hub_search.clone();
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
                if hub_later.catalog_open.get() {
                    begin_catalog_query(&ui, &hub_later, ticket, &text_later);
                } else {
                    apply_search(
                        &ui,
                        &browser_later,
                        &queue_later,
                        &config_later,
                        &hub_later,
                        &text_later,
                    );
                }
            },
        );
    });

    let ui_artist = ui.as_weak();
    let browser_artist = browser.clone();
    let hub_artist = hub_state.clone();
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
        let marks = hub_artist.marks.borrow().clone();
        show_browser(&ui, &mut browser, &marks);
    });

    let ui_album = ui.as_weak();
    let browser_album = browser.clone();
    let queue_album = queue.clone();
    let player_album = player.clone();
    let config_album = config.clone();
    let status_album = config_status.clone();
    let generation_album = search_generation.clone();
    let waves_album = waves.clone();
    let hub_album = hub_state.clone();
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
                    let marks = hub_album.marks.borrow().clone();
                    show_browser(&ui, &mut browser, &marks);
                }
                if hub_album.catalog_open.get() {
                    clear_catalog(&ui, &hub_album);
                }
                *hub_album.album_id.borrow_mut() = album.id;
                *queue_album.borrow_mut() = library_to_queue(library);
                let moved = queue_album.borrow_mut().select(0);
                jump(
                    &ui,
                    &player_album,
                    &queue_album,
                    &config_album,
                    &status_album,
                    &waves_album,
                    &hub_album,
                    moved,
                );
            }
            Err(err) => {
                ui.set_error(err.clone().into());
                ui.set_status(err.into());
            }
        }
    });

    let ui_like = ui.as_weak();
    let hub_like = hub_state.clone();
    let queue_like = queue.clone();
    let player_like = player.clone();
    let status_like = config_status.clone();
    ui.on_toggle_like(move || {
        let Some(ui) = ui_like.upgrade() else {
            return;
        };
        let item = queue_like.borrow().current().cloned();
        let Some(item) = item else {
            remember_note(
                &ui,
                &player_like.borrow(),
                &hub_like,
                &status_like,
                "Нет трека",
            );
            return;
        };
        if item.id.is_empty() {
            remember_note(
                &ui,
                &player_like.borrow(),
                &hub_like,
                &status_like,
                "Лайк только у треков библиотеки",
            );
            return;
        }
        let was = hub_like.marks.borrow().songs.contains(&item.id);
        {
            let mut marks = hub_like.marks.borrow_mut();
            if was {
                marks.songs.remove(&item.id);
            } else {
                marks.songs.insert(item.id.clone());
            }
        }
        paint_track_marks(&ui, &queue_like.borrow(), &hub_like);
        let id = item.id;
        let client = Arc::clone(&hub_like.hub);
        let events = Arc::clone(&hub_like.events);
        std::thread::spawn(move || {
            if let Err(message) = client.star_song(&id, was) {
                push_event(&events, HubEvent::SongStar { id, starred: was });
                push_event(&events, HubEvent::Note(message));
            }
        });
    });

    let ui_dislike = ui.as_weak();
    let hub_dislike = hub_state.clone();
    let queue_dislike = queue.clone();
    let player_dislike = player.clone();
    let config_dislike = config.clone();
    let status_dislike = config_status.clone();
    let waves_dislike = waves.clone();
    ui.on_toggle_dislike(move || {
        let Some(ui) = ui_dislike.upgrade() else {
            return;
        };
        let item = queue_dislike.borrow().current().cloned();
        let Some(item) = item else {
            remember_note(
                &ui,
                &player_dislike.borrow(),
                &hub_dislike,
                &status_dislike,
                "Нет трека",
            );
            return;
        };
        if item.id.is_empty() {
            remember_note(
                &ui,
                &player_dislike.borrow(),
                &hub_dislike,
                &status_dislike,
                "Дизлайк только у треков библиотеки",
            );
            return;
        }
        let was = hub_dislike.marks.borrow().disliked.contains(&item.id);
        {
            let mut marks = hub_dislike.marks.borrow_mut();
            if was {
                marks.disliked.remove(&item.id);
            } else {
                marks.disliked.insert(item.id.clone());
            }
        }
        let id = item.id.clone();
        let artist = item.artist.clone();
        let title = item.title.clone();
        if was {
            paint_track_marks(&ui, &queue_dislike.borrow(), &hub_dislike);
        } else {
            let moved = queue_dislike.borrow_mut().next();
            if moved {
                jump(
                    &ui,
                    &player_dislike,
                    &queue_dislike,
                    &config_dislike,
                    &status_dislike,
                    &waves_dislike,
                    &hub_dislike,
                    true,
                );
            } else {
                paint_track_marks(&ui, &queue_dislike.borrow(), &hub_dislike);
            }
        }
        let client = Arc::clone(&hub_dislike.hub);
        let events = Arc::clone(&hub_dislike.events);
        std::thread::spawn(move || {
            let result = if was {
                client.undislike_song(&id)
            } else {
                client.dislike_song(&id, &artist, &title)
            };
            if let Err(message) = result {
                push_event(&events, HubEvent::SongDislike { id, disliked: was });
                push_event(&events, HubEvent::Note(message));
            }
        });
    });

    let ui_album_like = ui.as_weak();
    let hub_album_like = hub_state.clone();
    let browser_album_like = browser.clone();
    let queue_album_like = queue.clone();
    ui.on_toggle_album_like(move || {
        let Some(ui) = ui_album_like.upgrade() else {
            return;
        };
        let id = hub_album_like.album_id.borrow().clone();
        if id.is_empty() {
            return;
        }
        let was = hub_album_like.marks.borrow().albums.contains(&id);
        {
            let mut marks = hub_album_like.marks.borrow_mut();
            if was {
                marks.albums.remove(&id);
            } else {
                marks.albums.insert(id.clone());
            }
        }
        let marks = hub_album_like.marks.borrow().clone();
        show_browser(&ui, &mut browser_album_like.borrow_mut(), &marks);
        paint_track_marks(&ui, &queue_album_like.borrow(), &hub_album_like);
        let client = Arc::clone(&hub_album_like.hub);
        let events = Arc::clone(&hub_album_like.events);
        std::thread::spawn(move || {
            if let Err(message) = client.star_album(&id, was) {
                push_event(&events, HubEvent::AlbumStar { id, starred: was });
                push_event(&events, HubEvent::Note(message));
            }
        });
    });

    let ui_star_row = ui.as_weak();
    let hub_star_row = hub_state.clone();
    let browser_star_row = browser.clone();
    let queue_star_row = queue.clone();
    ui.on_star_album(move |index| {
        if index < 0 {
            return;
        }
        let Some(ui) = ui_star_row.upgrade() else {
            return;
        };
        let album = browser_star_row
            .borrow()
            .visible_albums
            .get(index as usize)
            .cloned();
        let Some(album) = album else {
            return;
        };
        let was = hub_star_row.marks.borrow().albums.contains(&album.id);
        {
            let mut marks = hub_star_row.marks.borrow_mut();
            if was {
                marks.albums.remove(&album.id);
            } else {
                marks.albums.insert(album.id.clone());
            }
        }
        let marks = hub_star_row.marks.borrow().clone();
        show_browser(&ui, &mut browser_star_row.borrow_mut(), &marks);
        paint_track_marks(&ui, &queue_star_row.borrow(), &hub_star_row);
        let id = album.id;
        let client = Arc::clone(&hub_star_row.hub);
        let events = Arc::clone(&hub_star_row.events);
        std::thread::spawn(move || {
            if let Err(message) = client.star_album(&id, was) {
                push_event(&events, HubEvent::AlbumStar { id, starred: was });
                push_event(&events, HubEvent::Note(message));
            }
        });
    });

    let ui_catalog = ui.as_weak();
    let hub_catalog = hub_state.clone();
    let queue_catalog = queue.clone();
    ui.on_toggle_catalog(move || {
        let open = !hub_catalog.catalog_open.get();
        hub_catalog.catalog_open.set(open);
        let Some(ui) = ui_catalog.upgrade() else {
            return;
        };
        ui.set_catalog_open(open);
        if open {
            let text = ui.get_search().to_string();
            if text.trim().is_empty() {
                clear_catalog(&ui, &hub_catalog);
            } else {
                let ticket = hub_catalog.search_generation.get().wrapping_add(1);
                hub_catalog.search_generation.set(ticket);
                begin_catalog_query(&ui, &hub_catalog, ticket, &text);
            }
        }
        show_queue(&ui, &queue_catalog.borrow(), &hub_catalog);
    });

    let ui_hit = ui.as_weak();
    let hub_hit = hub_state.clone();
    let queue_hit = queue.clone();
    let player_hit = player.clone();
    let config_hit = config.clone();
    let status_hit = config_status.clone();
    let waves_hit = waves.clone();
    ui.on_pick_hit(move |index| {
        if index < 0 {
            return;
        }
        let Some(ui) = ui_hit.upgrade() else {
            return;
        };
        let hit = hub_hit.hits.borrow().get(index as usize).cloned();
        let Some(hit) = hit else {
            return;
        };
        match hit.kind {
            hub::HitKind::Album if hit.owned => {
                remember_note(
                    &ui,
                    &player_hit.borrow(),
                    &hub_hit,
                    &status_hit,
                    "Альбом уже в библиотеке",
                );
            }
            hub::HitKind::Album if hit.downloading => {
                remember_note(
                    &ui,
                    &player_hit.borrow(),
                    &hub_hit,
                    &status_hit,
                    "Уже качается",
                );
            }
            hub::HitKind::Album => {
                if hit.reference.is_empty() {
                    remember_note(
                        &ui,
                        &player_hit.borrow(),
                        &hub_hit,
                        &status_hit,
                        "Нет ссылки на альбом",
                    );
                    return;
                }
                {
                    let mut hits = hub_hit.hits.borrow_mut();
                    if let Some(row) = hits.iter_mut().find(|row| row.reference == hit.reference) {
                        row.downloading = true;
                    }
                }
                show_hits(&ui, &hub_hit.hits.borrow());
                remember_note(
                    &ui,
                    &player_hit.borrow(),
                    &hub_hit,
                    &status_hit,
                    "Отправляем альбом…",
                );
                let reference = hit.reference;
                let client = Arc::clone(&hub_hit.hub);
                let events = Arc::clone(&hub_hit.events);
                std::thread::spawn(move || match client.download_album(&reference) {
                    Ok(message) => push_event(
                        &events,
                        HubEvent::Download {
                            reference,
                            message,
                            failed: false,
                        },
                    ),
                    Err(message) => push_event(
                        &events,
                        HubEvent::Download {
                            reference,
                            message,
                            failed: true,
                        },
                    ),
                });
            }
            hub::HitKind::Track if hit.owned => {
                if hit.nd_song_id.is_empty() {
                    remember_note(
                        &ui,
                        &player_hit.borrow(),
                        &hub_hit,
                        &status_hit,
                        "Трек без номера в библиотеке",
                    );
                    return;
                }
                let item = queue::Item {
                    id: hit.nd_song_id,
                    path: String::new(),
                    title: hit.title,
                    artist: hit.artist,
                    album: hit.album,
                    meta: String::new(),
                    duration_s: hit.duration_s,
                };
                *hub_hit.album_id.borrow_mut() = String::new();
                let moved = queue_hit.borrow_mut().play_inserted(item);
                jump(
                    &ui,
                    &player_hit,
                    &queue_hit,
                    &config_hit,
                    &status_hit,
                    &waves_hit,
                    &hub_hit,
                    moved,
                );
            }
            hub::HitKind::Track if hit.downloading => {
                remember_note(
                    &ui,
                    &player_hit.borrow(),
                    &hub_hit,
                    &status_hit,
                    "Уже качается",
                );
            }
            hub::HitKind::Track => {
                if hit.reference.is_empty() {
                    remember_note(
                        &ui,
                        &player_hit.borrow(),
                        &hub_hit,
                        &status_hit,
                        "Нет ссылки на трек",
                    );
                    return;
                }
                {
                    let mut hits = hub_hit.hits.borrow_mut();
                    if let Some(row) = hits.iter_mut().find(|row| row.reference == hit.reference) {
                        row.downloading = true;
                    }
                }
                show_hits(&ui, &hub_hit.hits.borrow());
                remember_note(
                    &ui,
                    &player_hit.borrow(),
                    &hub_hit,
                    &status_hit,
                    "Отправляем…",
                );
                let reference = hit.reference;
                let client = Arc::clone(&hub_hit.hub);
                let events = Arc::clone(&hub_hit.events);
                std::thread::spawn(move || match client.download_track(&reference) {
                    Ok(message) => push_event(
                        &events,
                        HubEvent::Download {
                            reference,
                            message,
                            failed: false,
                        },
                    ),
                    Err(message) => push_event(
                        &events,
                        HubEvent::Download {
                            reference,
                            message,
                            failed: true,
                        },
                    ),
                });
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

    let ui_keys = ui.as_weak();
    let player_keys = player.clone();
    let queue_keys = queue.clone();
    let config_keys = config.clone();
    let status_keys = config_status.clone();
    let waves_keys = waves.clone();
    let hub_keys = hub_state.clone();
    ui.on_shortcut(move |text, control, alt, shift, meta| {
        let Some(command) = commands::shortcut_command(&text, control, alt, shift, meta) else {
            return false;
        };
        let Some(ui) = ui_keys.upgrade() else {
            return false;
        };
        dispatch(
            &ui,
            &player_keys,
            &queue_keys,
            &config_keys,
            &status_keys,
            &waves_keys,
            &hub_keys,
            command,
        );
        true
    });

    let (media_tx, media_rx) = mpsc::channel();
    // Linux attaches MPRIS now. Windows has no HWND until the event loop shows the window.
    #[cfg(target_os = "windows")]
    let media_tx_win = media_tx.clone();
    let media = Rc::new(RefCell::new(media_keys::attach(media_tx, None)));
    let ui_tick = ui.as_weak();
    let player_tick = player.clone();
    let queue_tick = queue.clone();
    let config_tick = config.clone();
    let status_tick = config_status.clone();
    let waves_tick = waves.clone();
    let hub_tick = hub_state.clone();
    let browser_tick = browser.clone();
    let media_tick = media.clone();
    let place_watch = Rc::new(RefCell::new(PlaceWatch {
        started: Instant::now(),
        last: None,
        loaded_xy: session.map(|place| (place.x, place.y)),
    }));
    let watch_tick = place_watch.clone();
    let media_watch = Rc::new(RefCell::new(MediaWatch::default()));
    let media_watch_tick = media_watch.clone();
    let timer = slint::Timer::default();
    #[cfg(target_os = "windows")]
    let mut windows_media_tried = false;
    timer.start(
        slint::TimerMode::Repeated,
        Duration::from_millis(200),
        move || {
            #[cfg(target_os = "windows")]
            if !windows_media_tried {
                if let Some(ui) = ui_tick.upgrade() {
                    if let Some(hwnd) = window_hwnd(&ui) {
                        windows_media_tried = true;
                        *media_tick.borrow_mut() =
                            media_keys::attach(media_tx_win.clone(), Some(hwnd));
                    }
                }
            }
            while let Ok(command) = media_rx.try_recv() {
                if let Some(ui) = ui_tick.upgrade() {
                    dispatch(
                        &ui,
                        &player_tick,
                        &queue_tick,
                        &config_tick,
                        &status_tick,
                        &waves_tick,
                        &hub_tick,
                        command,
                    );
                }
            }
            let moved = {
                let mut player = player_tick.borrow_mut();
                let mut queue = queue_tick.borrow_mut();
                take_queue_step(&mut player, &mut queue)
            };
            if moved {
                if let Some(ui) = ui_tick.upgrade() {
                    let _ = play_current(
                        &ui,
                        &player_tick,
                        &queue_tick,
                        &config_tick,
                        &waves_tick,
                        &hub_tick,
                    );
                }
            }
            if let Some(ui) = ui_tick.upgrade() {
                apply_hub_events(&ui, &hub_tick, &browser_tick, &queue_tick);
                if let Some(peaks) = waves_tick.poll() {
                    show_wave(&ui, &peaks);
                }
                let player = player_tick.borrow();
                push_playback(&ui, &player.snapshot());
                show_status(&ui, &player, &hub_tick, &status_tick);
                publish_media(
                    &mut media_tick.borrow_mut(),
                    &ui,
                    &player,
                    &mut media_watch_tick.borrow_mut(),
                );
                remember_place(&ui, &mut watch_tick.borrow_mut());
            }
        },
    );

    let tray = PlayerTray::new()?;
    tray.set_tray_icon(tray_icon());
    let click_origin = Instant::now();
    let last_click = Rc::new(Cell::new(None::<u128>));
    let ui_tray_click = ui.as_weak();
    tray.on_icon_clicked(move || {
        let now = click_origin.elapsed().as_millis();
        let (restore, next) = commands::tray_click(last_click.get(), now);
        last_click.set(next);
        if restore {
            if let Some(ui) = ui_tray_click.upgrade() {
                reveal(&ui);
            }
        }
    });
    let ui_show = ui.as_weak();
    tray.on_show_window(move || {
        if let Some(ui) = ui_show.upgrade() {
            reveal(&ui);
        }
    });
    let ui_quit = ui.as_weak();
    let tray_quit = tray.as_weak();
    tray.on_quit(move || shutdown(&ui_quit, &tray_quit));

    let ui_min = ui.as_weak();
    ui.on_minimize(move || {
        if let Some(ui) = ui_min.upgrade() {
            let _ = ui.hide();
        }
    });
    let ui_close_btn = ui.as_weak();
    let tray_close_btn = tray.as_weak();
    ui.on_close_window(move || shutdown(&ui_close_btn, &tray_close_btn));

    let ui_close = ui.as_weak();
    let tray_close = tray.as_weak();
    ui.window().on_close_requested(move || {
        shutdown(&ui_close, &tray_close);
        slint::CloseRequestResponse::HideWindow
    });

    ui.run()
}

struct PlaceWatch {
    started: Instant,
    last: Option<window_place::Place>,
    loaded_xy: Option<(f32, f32)>,
}

fn remember_place(ui: &AppWindow, watch: &mut PlaceWatch) {
    // The first moments report 0,0 before the window manager applies the restored position.
    if watch.started.elapsed() < Duration::from_millis(700) {
        return;
    }
    let Some(mut place) = current_place(ui) else {
        return;
    };
    let pos = ui.window().position();
    if pos.x == 0 && pos.y == 0 && watch.last.is_none() {
        match watch.loaded_xy {
            Some((x, y)) => {
                place.x = x;
                place.y = y;
            }
            None => return,
        }
    }
    if watch.last.is_some_and(|last| same_place(last, place)) {
        return;
    }
    watch.last = Some(place);
    window_place::save(place);
}

fn save_place(ui: &AppWindow) {
    if let Some(place) = current_place(ui) {
        window_place::save(place);
    }
}

fn current_place(ui: &AppWindow) -> Option<window_place::Place> {
    let scale = ui.window().scale_factor();
    if scale <= 0.0 {
        return None;
    }
    let pos = ui.window().position().to_logical(scale);
    let size = ui.window().size().to_logical(scale);
    if size.width < 1.0 || size.height < 1.0 {
        return None;
    }
    Some(window_place::Place {
        x: pos.x,
        y: pos.y,
        mode: Some(mode_index(ui.get_mode())),
        width: Some(size.width),
        height: Some(size.height),
        volume: Some(ui.get_volume()),
        eq_on: Some(ui.get_eq_on()),
        eq_open: Some(ui.get_eq_open()),
        preamp: Some(ui.get_preamp()),
        bands: Some(bands_from_ui(ui)),
    })
}

fn same_place(left: window_place::Place, right: window_place::Place) -> bool {
    (left.x - right.x).abs() < 0.5
        && (left.y - right.y).abs() < 0.5
        && left.mode == right.mode
        && match (left.width, left.height, right.width, right.height) {
            (Some(lw), Some(lh), Some(rw), Some(rh)) => {
                (lw - rw).abs() < 0.5 && (lh - rh).abs() < 0.5
            }
            _ => false,
        }
        && match (left.volume, right.volume) {
            (Some(lv), Some(rv)) => (lv - rv).abs() < 0.005,
            _ => false,
        }
        && left.eq_on == right.eq_on
        && left.eq_open == right.eq_open
        && match (left.preamp, right.preamp) {
            (Some(lp), Some(rp)) => (lp - rp).abs() < 0.05,
            _ => false,
        }
        && match (left.bands, right.bands) {
            (Some(lb), Some(rb)) => lb.iter().zip(rb).all(|(a, b)| (a - b).abs() < 0.05),
            _ => false,
        }
}

fn apply_session(ui: &AppWindow, session: Option<window_place::Place>) {
    let mode = session
        .and_then(|place| place.mode)
        .unwrap_or(0)
        .clamp(0, 2);
    ui.set_mode(mode_from_index(mode));
    if let Some(place) = session {
        ui.set_eq_open(place.eq_open.unwrap_or(false));
    }
    if let Some((width, height)) =
        session.and_then(|place| window_place::usable_size(place.width, place.height))
    {
        ui.window().set_size(slint::LogicalSize::new(width, height));
    } else {
        resize(ui, mode);
    }
    if let Some(place) = session {
        ui.window()
            .set_position(slint::LogicalPosition::new(place.x, place.y));
    }
}

fn mode_index(mode: Mode) -> i32 {
    match mode {
        Mode::Mini => 0,
        Mode::Medium => 1,
        Mode::Large => 2,
    }
}

fn mode_from_index(mode: i32) -> Mode {
    match mode {
        0 => Mode::Mini,
        1 => Mode::Medium,
        _ => Mode::Large,
    }
}

#[derive(Default)]
struct MediaWatch {
    volume: f32,
    playing: bool,
    idle: bool,
    title: String,
    ready: bool,
}

#[cfg(target_os = "windows")]
fn window_hwnd(ui: &AppWindow) -> Option<isize> {
    use raw_window_handle::HasWindowHandle;
    let handle = ui.window().window_handle();
    let raw = handle.window_handle().ok()?;
    match raw.as_raw() {
        raw_window_handle::RawWindowHandle::Win32(win) => Some(win.hwnd.get()),
        _ => None,
    }
}

fn publish_media(
    media: &mut Option<souvlaki::MediaControls>,
    ui: &AppWindow,
    player: &Player,
    watch: &mut MediaWatch,
) {
    let Some(media) = media.as_mut() else {
        return;
    };
    let volume = ui.get_volume();
    let playing = ui.get_playing();
    let idle = player.is_idle();
    let title = ui.get_track_title().to_string();
    if !watch.ready || (volume - watch.volume).abs() > 0.01 {
        if publish_volume(media, volume) {
            watch.volume = volume;
        }
    }
    if !watch.ready || playing != watch.playing || idle != watch.idle {
        let playback = if playing {
            souvlaki::MediaPlayback::Playing { progress: None }
        } else if idle {
            souvlaki::MediaPlayback::Stopped
        } else {
            souvlaki::MediaPlayback::Paused { progress: None }
        };
        if media.set_playback(playback).is_ok() {
            watch.playing = playing;
            watch.idle = idle;
        }
    }
    if title != watch.title && !title.is_empty() && title != "Нет трека" {
        let artist = ui.get_track_artist().to_string();
        let album = ui.get_track_album().to_string();
        let _ = media.set_metadata(souvlaki::MediaMetadata {
            title: Some(title.as_str()),
            artist: Some(artist.as_str()),
            album: Some(album.as_str()),
            cover_url: None,
            duration: None,
        });
        watch.title = title;
    }
    watch.ready = true;
}

/// souvlaki 0.8 publishes volume on MPRIS. Its Windows backend has no `set_volume`.
fn publish_volume(media: &mut souvlaki::MediaControls, volume: f32) -> bool {
    #[cfg(not(target_os = "windows"))]
    {
        media.set_volume(f64::from(volume)).is_ok()
    }
    #[cfg(target_os = "windows")]
    {
        let _ = (media, volume);
        true
    }
}

fn dispatch(
    ui: &AppWindow,
    player: &Rc<RefCell<Player>>,
    queue: &Rc<RefCell<queue::Queue>>,
    config: &navidrome::ServerConfig,
    status: &str,
    waves: &std::sync::Arc<wave::WaveHub>,
    hub: &HubState,
    command: commands::Command,
) {
    use commands::Command;
    match command {
        Command::Toggle => {
            if player.borrow().is_idle() {
                let _ = play_current(ui, player, queue, config, waves, hub);
            } else {
                player.borrow_mut().toggle();
            }
        }
        Command::Play => {
            let (idle, playing) = {
                let player = player.borrow();
                (player.is_idle(), player.snapshot().playing)
            };
            if idle {
                let _ = play_current(ui, player, queue, config, waves, hub);
            } else if !playing {
                player.borrow_mut().toggle();
            }
        }
        Command::Pause => {
            if player.borrow().snapshot().playing {
                player.borrow_mut().toggle();
            }
        }
        Command::Stop => player.borrow_mut().stop(),
        Command::Next => {
            let moved = queue.borrow_mut().next();
            jump(ui, player, queue, config, status, waves, hub, moved);
            return;
        }
        Command::Previous => {
            let moved = queue.borrow_mut().prev();
            jump(ui, player, queue, config, status, waves, hub, moved);
            return;
        }
        Command::VolumeUp | Command::VolumeDown | Command::SetVolume(_) => {
            let current = player.borrow().snapshot().volume;
            if let Some(volume) = commands::volume_after(current, command) {
                player.borrow_mut().set_volume(volume);
            }
        }
    }
    let player = player.borrow();
    push_playback(ui, &player.snapshot());
    show_status(ui, &player, hub, status);
}

fn shutdown(ui: &slint::Weak<AppWindow>, tray: &slint::Weak<PlayerTray>) {
    if let Some(ui) = ui.upgrade() {
        save_place(&ui);
        let _ = ui.hide();
    }
    if let Some(tray) = tray.upgrade() {
        let _ = tray.hide();
    }
    let _ = slint::quit_event_loop();
}

fn reveal(ui: &AppWindow) {
    let _ = ui.show();
    ui.window().set_minimized(false);
}

fn empty_wave() -> slint::ModelRc<f32> {
    slint::ModelRc::from(Rc::new(slint::VecModel::from(Vec::<f32>::new())))
}

fn show_wave(ui: &AppWindow, peaks: &[f32]) {
    ui.set_wave(slint::ModelRc::from(Rc::new(slint::VecModel::from(
        peaks.to_vec(),
    ))));
}

fn tray_icon() -> slint::Image {
    let png = include_bytes!("../assets/icon.png");
    let image = image::load_from_memory(png)
        .expect("embedded app icon")
        .resize(64, 64, image::imageops::FilterType::Lanczos3)
        .into_rgba8();
    let buffer = slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(
        image.as_raw(),
        image.width(),
        image.height(),
    );
    slint::Image::from_rgba8(buffer)
}

fn push_playback(ui: &AppWindow, state: &player::PlaybackState) {
    ui.set_playing(state.playing);
    ui.set_position(state.position_s);
    ui.set_duration(state.duration_s);
    ui.set_volume(state.volume);
    ui.set_time_label(clock(state.position_s).into());
    ui.set_duration_label(clock(state.duration_s).into());
}

const EQ_PANEL_HEIGHT: f32 = 132.0;

fn resize(ui: &AppWindow, mode: i32) {
    // The 28px title strip sits above the layouts tuned in the earlier steps.
    let (width, mut height) = match mode {
        0 => (520.0, 160.0),
        1 => (460.0, 668.0),
        _ => (1040.0, 708.0),
    };
    if ui.get_eq_open() {
        height += EQ_PANEL_HEIGHT;
    }
    ui.window().set_size(slint::LogicalSize::new(width, height));
}

fn set_eq_open(ui: &AppWindow, open: bool) {
    if ui.get_eq_open() == open {
        return;
    }
    let scale = ui.window().scale_factor();
    let size = ui.window().size().to_logical(scale);
    ui.set_eq_open(open);
    if scale <= 0.0 || size.height < 1.0 {
        return;
    }
    let delta = if open {
        EQ_PANEL_HEIGHT
    } else {
        -EQ_PANEL_HEIGHT
    };
    let height = (size.height + delta).clamp(80.0, 4000.0);
    ui.window()
        .set_size(slint::LogicalSize::new(size.width, height));
}

fn show_bands(ui: &AppWindow, bands: &[f32; 10]) {
    let model = ui.get_bands();
    if let Some(rows) = model.as_any().downcast_ref::<slint::VecModel<f32>>() {
        if rows.row_count() == bands.len() {
            for (index, gain) in bands.iter().enumerate() {
                rows.set_row_data(index, *gain);
            }
            return;
        }
    }
    ui.set_bands(slint::ModelRc::from(Rc::new(slint::VecModel::from(
        bands.to_vec(),
    ))));
}

fn set_band_row(ui: &AppWindow, index: usize, gain: f32) {
    let model = ui.get_bands();
    if let Some(rows) = model.as_any().downcast_ref::<slint::VecModel<f32>>() {
        rows.set_row_data(index, gain);
    }
}

fn bands_from_ui(ui: &AppWindow) -> [f32; 10] {
    let model = ui.get_bands();
    let mut bands = [0.0; 10];
    for (index, band) in bands.iter_mut().enumerate() {
        if let Some(gain) = model.row_data(index) {
            *band = eq::clamp_db(gain);
        }
    }
    bands
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

fn show_status(ui: &AppWindow, player: &Player, hub: &HubState, fallback: &str) {
    let text = if player.notice().is_some() {
        player.status_or(fallback)
    } else {
        let note = hub.hub_note.borrow();
        if note.is_empty() {
            fallback.to_string()
        } else {
            note.clone()
        }
    };
    ui.set_status(text.into());
}

fn remember_note(ui: &AppWindow, player: &Player, hub: &HubState, fallback: &str, text: &str) {
    *hub.hub_note.borrow_mut() = text.to_string();
    show_status(ui, player, hub, fallback);
}

fn begin_catalog_query(ui: &AppWindow, hub: &HubState, ticket: u64, query: &str) {
    hub.hub_note.borrow_mut().clear();
    ui.set_error(String::new().into());
    ui.set_catalog_note("Ищем…".into());
    spawn_catalog_search(hub, ticket, query.to_string());
}

fn spawn_catalog_search(hub: &HubState, ticket: u64, query: String) {
    let client = Arc::clone(&hub.hub);
    let events = Arc::clone(&hub.events);
    std::thread::spawn(move || match client.search(&query) {
        Ok(hits) => push_event(&events, HubEvent::Hits { ticket, hits }),
        Err(message) => push_event(&events, HubEvent::Miss { ticket, message }),
    });
}

fn clear_catalog(ui: &AppWindow, hub: &HubState) {
    hub.hits.borrow_mut().clear();
    hub.hub_note.borrow_mut().clear();
    ui.set_hits(slint::ModelRc::from(Rc::new(slint::VecModel::from(Vec::<
        CatalogHit,
    >::new(
    )))));
    ui.set_catalog_note("Поиск в сети".into());
    ui.set_error(String::new().into());
}

fn show_hits(ui: &AppWindow, hits: &[hub::CatalogHit]) {
    let rows = hits
        .iter()
        .map(|hit| CatalogHit {
            title: hit.title.clone().into(),
            artist: hit.artist.clone().into(),
            detail: hit.detail.clone().into(),
            action: hit.action().into(),
        })
        .collect::<Vec<_>>();
    ui.set_hits(slint::ModelRc::from(Rc::new(slint::VecModel::from(rows))));
    let note = if hits.is_empty() {
        "Ничего не найдено".to_string()
    } else {
        format!("{} найдено", hits.len())
    };
    ui.set_catalog_note(note.into());
}

fn paint_track_marks(ui: &AppWindow, queue: &queue::Queue, hub: &HubState) {
    let id = queue
        .current()
        .map(|item| item.id.clone())
        .unwrap_or_default();
    let marks = hub.marks.borrow();
    ui.set_liked(!id.is_empty() && marks.songs.contains(&id));
    ui.set_disliked(!id.is_empty() && marks.disliked.contains(&id));
    let album_id = hub.album_id.borrow();
    ui.set_album_known(!album_id.is_empty());
    ui.set_album_liked(!album_id.is_empty() && marks.albums.contains(album_id.as_str()));
}

fn apply_hub_events(
    ui: &AppWindow,
    hub: &HubState,
    browser: &RefCell<Browser>,
    queue: &RefCell<queue::Queue>,
) {
    let events = take_events(&hub.events);
    if events.is_empty() {
        return;
    }
    let mut refresh_marks = false;
    let mut refresh_hits = false;
    for event in events {
        match event {
            HubEvent::Marks(marks) => {
                *hub.marks.borrow_mut() = marks;
                refresh_marks = true;
            }
            HubEvent::Note(text) => {
                *hub.hub_note.borrow_mut() = text;
            }
            HubEvent::Hits { ticket, hits } => {
                if hub.catalog_open.get() && ticket == hub.search_generation.get() {
                    *hub.hits.borrow_mut() = hits;
                    hub.hub_note.borrow_mut().clear();
                    ui.set_error(String::new().into());
                    refresh_hits = true;
                }
            }
            HubEvent::Miss { ticket, message } => {
                if hub.catalog_open.get() && ticket == hub.search_generation.get() {
                    *hub.hub_note.borrow_mut() = message.clone();
                    ui.set_error(message.into());
                }
            }
            HubEvent::SongStar { id, starred } => {
                let mut marks = hub.marks.borrow_mut();
                if starred {
                    marks.songs.insert(id);
                } else {
                    marks.songs.remove(&id);
                }
                refresh_marks = true;
            }
            HubEvent::AlbumStar { id, starred } => {
                let mut marks = hub.marks.borrow_mut();
                if starred {
                    marks.albums.insert(id);
                } else {
                    marks.albums.remove(&id);
                }
                refresh_marks = true;
            }
            HubEvent::SongDislike { id, disliked } => {
                let mut marks = hub.marks.borrow_mut();
                if disliked {
                    marks.disliked.insert(id);
                } else {
                    marks.disliked.remove(&id);
                }
                refresh_marks = true;
            }
            HubEvent::Download {
                reference,
                message,
                failed,
            } => {
                *hub.hub_note.borrow_mut() = message;
                if failed {
                    if let Some(hit) = hub
                        .hits
                        .borrow_mut()
                        .iter_mut()
                        .find(|hit| hit.reference == reference)
                    {
                        hit.downloading = false;
                    }
                    refresh_hits = true;
                }
            }
        }
    }
    if refresh_marks {
        let marks = hub.marks.borrow().clone();
        show_browser(ui, &mut browser.borrow_mut(), &marks);
        paint_track_marks(ui, &queue.borrow(), hub);
    }
    if refresh_hits && hub.catalog_open.get() {
        show_hits(ui, &hub.hits.borrow());
    }
}

fn album_line(album: &navidrome::Album) -> String {
    if album.song_count <= 1 {
        return album.artist.clone();
    }
    if album.artist.is_empty() {
        return album.song_count.to_string();
    }
    format!("{} · {}", album.artist, album.song_count)
}

fn show_browser(ui: &AppWindow, browser: &mut Browser, marks: &hub::Marks) {
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
            artist: album_line(album).into(),
            starred: marks.albums.contains(&album.id),
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
    hub: &HubState,
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
        let marks = hub.marks.borrow().clone();
        show_browser(ui, &mut browser, &marks);
    }
    show_queue(ui, &queue.borrow(), hub);
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
    waves: &std::sync::Arc<wave::WaveHub>,
    hub: &HubState,
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
        show_queue(ui, &queue, hub);
    }
    ui.set_wave(empty_wave());
    waves.load(target.clone());
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
    waves: &std::sync::Arc<wave::WaveHub>,
    hub: &HubState,
    moved: bool,
) {
    if !moved {
        return;
    }
    let _ = play_current(ui, player, queue, config, waves, hub);
    let player = player.borrow();
    push_playback(ui, &player.snapshot());
    show_status(ui, &player, hub, status);
}

/// The timer and the end-of-file test share this. A stop leaves the index where it is.
fn take_queue_step(player: &mut Player, queue: &mut queue::Queue) -> bool {
    player.take_natural_end() && queue.next()
}

fn show_queue(ui: &AppWindow, queue: &queue::Queue, hub: &HubState) {
    let query = if ui.get_catalog_open() {
        String::new()
    } else {
        ui.get_search().to_string()
    };
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
    paint_track_marks(ui, queue, hub);
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
    fn app_icon_is_the_brand_mark() {
        let image = cover_image(include_bytes!("../assets/icon.png")).expect("icon");
        let pixels = image.to_rgba8().expect("pixels");
        assert_eq!((pixels.width(), pixels.height()), (512, 512));
        let raw = pixels.as_bytes();
        // Cream disc, left of the green play triangle.
        let cream = ((256 * 512 + 216) * 4) as usize;
        assert!(raw[cream] > 240 && raw[cream + 1] > 230 && raw[cream + 2] > 210);
        // Green field above the vinyl ring.
        let field = ((40 * 512 + 256) * 4) as usize;
        assert!(raw[field] < 40 && raw[field + 1] > 120 && raw[field + 2] < 100);
        assert_eq!(raw[field + 3], 255);
        // Rounded corner stays transparent.
        assert_eq!(raw[3], 0);
        let tray = tray_icon().to_rgba8().expect("tray");
        assert_eq!((tray.width(), tray.height()), (64, 64));
        let tray_raw = tray.as_bytes();
        let top = ((5 * 64 + 32) * 4) as usize;
        assert!(tray_raw[top + 1] > tray_raw[top] + 40);
    }

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
