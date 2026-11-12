#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod album_view;
mod commands;
mod eq;
mod home;
mod hub;
mod media_keys;
mod navidrome;
mod paths;
mod playback_session;
mod mix_actions;
mod player;
mod queue;
mod queue_actions;
mod wave;
mod window_place;

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, HashSet, VecDeque};
use std::rc::Rc;
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use player::{Player, PlayerOptions};
use slint::ComponentHandle;
use slint::Model;
use album_view::{apply_album_result, close_album, connect_album_actions, open_album};
use queue_actions::connect_queue_actions;
use mix_actions::{fetch_mix, poll_mix, rebuild_mix, save_taste, set_mix_mode, toggle_taste};

slint::include_modules!();

enum HubEvent {
    CatalogLoaded(Result<navidrome::Catalog, String>),
    LibraryFound {
        ticket: u64,
        result: Result<navidrome::Catalog, String>,
    },
    AlbumLoaded {
        ticket: u64,
        result: Result<navidrome::LibraryQueue, String>,
    },
    QueueLoaded {
        ticket: u64,
        result: Result<navidrome::LibraryQueue, String>,
    },
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
    Home(hub::HomeFeed),
    HomeError(String),
    Mix(mix_actions::Event),
    WaveStarted {
        ticket: u64,
        batch: hub::WaveBatch,
        personal: bool,
        origin: String,
    },
    WaveStartFailed { ticket: u64, message: String },
    WaveMore(hub::WaveBatch),
    WaveFailed {
        message: String,
        played: Vec<serde_json::Value>,
    },
    Lists(Vec<hub::PlaylistLine>),
    Liked(hub::LikedPage),
    Cover { id: String, pixels: Option<CoverPixels> },
}

struct AlbumPreview {
    id: String,
    cover_id: String,
    tracks: Vec<queue::Item>,
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
    album_generation: Cell<u64>,
    album_preview: RefCell<Option<AlbumPreview>>,
    queue_generation: Cell<u64>,
    catalog_loading: Cell<bool>,
    catalog_error: RefCell<String>,
    playback_store: RefCell<playback_session::Store>,
    playback_ready: Cell<bool>,
    mix_control: mix_actions::State,
    mini_position: Cell<Option<(f32, f32)>>,
    user_name: String,
    pane: Cell<home::Pane>,
    feed: RefCell<hub::HomeFeed>,
    mix: RefCell<hub::MixSnapshot>,
    rows: RefCell<Vec<home::Row>>,
    covers: RefCell<CoverCache>,
    cover_requests: mpsc::Sender<String>,
    home_error: RefCell<String>,
    wave: RefCell<hub::WaveLive>,
    wave_busy: Cell<bool>,
    was_playing: Cell<bool>,
    playlists: RefCell<Vec<hub::PlaylistLine>>,
    liked_songs: RefCell<Vec<hub::Playable>>,
    liked_albums: RefCell<Vec<hub::HomeAlbum>>,
    taste: RefCell<Vec<hub::TasteArtist>>,
    taste_picked: RefCell<HashSet<String>>,
    mix_poll_at: Cell<Option<Instant>>,
    mix_fetching: Cell<bool>,
}

impl HubState {
    fn start(config: &navidrome::ServerConfig) -> Self {
        let hub = Arc::new(hub::Hub::from_config(config));
        let events = Arc::new(Mutex::new(Vec::new()));
        let (cover_requests, cover_jobs) = mpsc::channel::<String>();
        let cover_config = config.clone();
        let cover_events = Arc::clone(&events);
        std::thread::spawn(move || {
            while let Ok(id) = cover_jobs.recv() {
                let pixels = navidrome::cover_art(&cover_config, &id)
                    .as_deref()
                    .and_then(decode_cover);
                push_event(&cover_events, HubEvent::Cover { id, pixels });
            }
        });
        let client = Arc::clone(&hub);
        let pending = Arc::clone(&events);
        std::thread::spawn(move || {
            if let Ok(marks) = client.load_marks() {
                push_event(&pending, HubEvent::Marks(marks));
            }
            match client.home() {
                Ok(feed) => push_event(&pending, HubEvent::Home(feed)),
                Err(message) => push_event(&pending, HubEvent::HomeError(message)),
            }
            push_event(&pending, HubEvent::Mix(mix_actions::Event::Fetched {
                ticket: 0, result: client.mix_today(),
            }));
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
            album_generation: Cell::new(0),
            album_preview: RefCell::new(None),
            queue_generation: Cell::new(0),
            catalog_loading: Cell::new(true),
            catalog_error: RefCell::new(String::new()),
            playback_store: RefCell::new(playback_session::Store::new(config)),
            playback_ready: Cell::new(false),
            mix_control: mix_actions::State::default(),
            mini_position: Cell::new(None),
            user_name: config.user.clone(),
            pane: Cell::new(home::Pane::Home),
            feed: RefCell::new(hub::HomeFeed::default()),
            mix: RefCell::new(hub::MixSnapshot::default()),
            rows: RefCell::new(Vec::new()),
            covers: RefCell::new(CoverCache::default()),
            cover_requests,
            home_error: RefCell::new(String::new()),
            wave: RefCell::new(hub::WaveLive::default()),
            wave_busy: Cell::new(false),
            was_playing: Cell::new(false),
            playlists: RefCell::new(Vec::new()),
            liked_songs: RefCell::new(Vec::new()),
            liked_albums: RefCell::new(Vec::new()),
            taste: RefCell::new(Vec::new()),
            taste_picked: RefCell::new(HashSet::new()),
            mix_poll_at: Cell::new(Some(Instant::now())),
            mix_fetching: Cell::new(true),
        }
    }
}

const COVER_CACHE_LIMIT: usize = 256;

struct CoverPixels {
    bytes: Vec<u8>,
    width: u32,
    height: u32,
}

#[derive(Default)]
struct CoverCache {
    images: HashMap<String, Option<slint::Image>>,
    pending: HashSet<String>,
    order: VecDeque<String>,
}

impl CoverCache {
    fn reserve(&mut self, id: &str) -> bool {
        if id.is_empty() || self.images.contains_key(id) || self.pending.contains(id) {
            return false;
        }
        self.pending.insert(id.to_string());
        true
    }

    fn finish(&mut self, id: String, image: Option<slint::Image>) {
        self.pending.remove(&id);
        if self.images.len() >= COVER_CACHE_LIMIT {
            if let Some(oldest) = self.order.pop_front() {
                self.images.remove(&oldest);
            }
        }
        self.order.push_back(id.clone());
        self.images.insert(id, image);
    }
}

fn request_cover(hub: &HubState, id: &str) -> Option<slint::Image> {
    let mut cache = hub.covers.borrow_mut();
    if let Some(image) = cache.images.get(id) {
        return image.clone();
    }
    if cache.reserve(id) {
        if hub.cover_requests.send(id.to_string()).is_err() {
            cache.pending.remove(id);
        }
    }
    None
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
    ui.set_home_title(home::greeting(home::local_hour(), &config.user).into());
    let hub_state = Rc::new(HubState::start(&config));
    hub_state.mini_position.set(session.and_then(|place| {
        place.mini_position.or_else(|| (place.mode.unwrap_or(0) == 0).then_some((place.x, place.y)))
    }));
    let queue = Rc::new(RefCell::new(queue::Queue::from_tracks("", Vec::new())));
    let browser = Rc::new(RefCell::new(Browser::empty()));
    clear_track(&ui);
    let saved_playback = match hub_state.playback_store.borrow_mut().load() {
        Ok(saved) => saved,
        Err(message) => {
            *hub_state.hub_note.borrow_mut() = message;
            None
        }
    };
    if let Some(target) = std::env::args().nth(1) {
        let (title, meta) = track_label(&target);
        *queue.borrow_mut() = queue::Queue::from_path(&target, &title, &meta);
        queue.borrow_mut().select(0);
        let _ = play_current(&ui, &player, &queue, &config, &waves, &hub_state);
    } else if let Some(saved) = saved_playback {
        *queue.borrow_mut() = saved.queue;
        *hub_state.album_id.borrow_mut() = saved.album_id;
        *hub_state.wave.borrow_mut() = saved.wave.unwrap_or_default();
        hub_state.playback_ready.set(true);
        if queue.borrow().current().is_some() {
            let _ = play_current_at(
                &ui, &player, &queue, &config, &waves, &hub_state, saved.position_s, true,
            );
        } else {
            show_queue(&ui, &queue.borrow(), &hub_state);
        }
    } else {
        ui.set_queue_busy(true);
        let config = (*config).clone();
        let events = Arc::clone(&hub_state.events);
        std::thread::spawn(move || {
            let result = navidrome::load_queue(&config);
            push_event(&events, HubEvent::QueueLoaded { ticket: 0, result });
        });
    }
    ui.set_library_busy(true);
    let catalog_config = (*config).clone();
    let catalog_events = Arc::clone(&hub_state.events);
    std::thread::spawn(move || {
        let result = navidrome::load_catalog(&catalog_config);
        push_event(&catalog_events, HubEvent::CatalogLoaded(result));
    });
    let config_status = Rc::new("Библиотека Navidrome".to_string());
    show_status(&ui, &player.borrow(), &hub_state, &config_status);
    refresh_home(&ui, &hub_state, player.borrow().snapshot().playing);
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
        if queue_play.borrow().is_empty() {
            return;
        }
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
    let hub_stop = hub_state.clone();
    let queue_stop = queue.clone();
    ui.on_stop(move || {
        if let Some(ui) = ui_stop.upgrade() {
            record_playback(&ui, &hub_stop, &queue_stop.borrow(), &player_stop.borrow().snapshot(), false);
        }
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
        if let Some(item) = queue_prev.borrow().current().cloned() {
            note_wave_leave(&player_prev.borrow(), &item, &hub_prev, false);
        }
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
        if let Some(item) = queue_next.borrow().current().cloned() {
            note_wave_leave(&player_next.borrow(), &item, &hub_next, false);
        }
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
        let index = index as usize;
        let same = queue_row.borrow().index() == Some(index);
        if !same {
            if let Some(item) = queue_row.borrow().current().cloned() {
                note_wave_leave(&player_row.borrow(), &item, &hub_row, false);
            }
        }
        let moved = queue_row.borrow_mut().select(index);
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

    let search_timer = Rc::new(slint::Timer::default());
    let ui_search = ui.as_weak();
    let browser_search = browser.clone();
    let config_search = config.clone();
    let timer_search = search_timer.clone();
    let hub_search = hub_state.clone();
    ui.on_search_edited(move |text| {
        let ticket = hub_search.search_generation.get().wrapping_add(1);
        hub_search.search_generation.set(ticket);
        timer_search.stop();
        let Some(ui) = ui_search.upgrade() else {
            return;
        };
        let query = text.trim().to_string();
        if hub_search.pane.get() == home::Pane::Taste && !hub_search.catalog_open.get() {
            if hub_search.mix_control.taste_saving.get() { return; }
            let taste_ticket = mix_actions::prepare_taste_search(&hub_search);
            refresh_home(&ui, &hub_search, hub_search.was_playing.get());
            let hub = hub_search.clone();
            timer_search.start(slint::TimerMode::SingleShot, Duration::from_millis(300), move || {
                if hub.pane.get() == home::Pane::Taste
                    && hub.mix_control.taste_generation.get() == taste_ticket {
                    mix_actions::fetch_taste(&hub, taste_ticket, query.clone());
                }
            });
            return;
        }
        close_album(&ui, &hub_search);
        if hub_search.catalog_open.get() {
            clear_catalog(&ui, &hub_search);
            if query.is_empty() {
                return;
            }
            ui.set_catalog_busy(true);
            ui.set_catalog_note("Поиск в сети…".into());
        } else {
            show_library_pane(&ui, &hub_search);
            let mut browser = browser_search.borrow_mut();
            browser.query = query.clone();
            browser.artist_id.clear();
            browser.shown = if query.is_empty() {
                browser.catalog.clone()
            } else {
                navidrome::Catalog { artists: Vec::new(), albums: Vec::new() }
            };
            ui.set_library_busy(!query.is_empty() || hub_search.catalog_loading.get());
            ui.set_library_error(if query.is_empty() {
                hub_search.catalog_error.borrow().clone().into()
            } else {
                String::new().into()
            });
            show_browser(&ui, &mut browser, &hub_search);
            if query.is_empty() {
                return;
            }
        }
        let hub = hub_search.clone();
        let config = config_search.clone();
        timer_search.start(
            slint::TimerMode::SingleShot,
            Duration::from_millis(300),
            move || {
                if hub.search_generation.get() != ticket {
                    return;
                }
                if hub.catalog_open.get() {
                    spawn_catalog_search(&hub, ticket, query.clone());
                } else {
                    let config = (*config).clone();
                    let events = Arc::clone(&hub.events);
                    let query = query.clone();
                    std::thread::spawn(move || {
                        let result = navidrome::search_catalog(&config, &query);
                        push_event(&events, HubEvent::LibraryFound { ticket, result });
                    });
                }
            },
        );
    });

    let ui_filter = ui.as_weak();
    let queue_filter = queue.clone();
    let hub_filter = hub_state.clone();
    ui.on_queue_search_edited(move |text| {
        if let Some(ui) = ui_filter.upgrade() {
            ui.set_queue_search(text);
            show_queue(&ui, &queue_filter.borrow(), &hub_filter);
        }
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
        show_browser(&ui, &mut browser, &hub_artist);
    });

    let ui_album = ui.as_weak();
    let browser_album = browser.clone();
    let config_album = config.clone();
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
        open_album(
            &ui, &hub_album, &config_album,
            &album.id, &album.name, &album.artist, &album.cover_id,
        );
    });

    connect_album_actions(&ui, &hub_state, &player, &queue, &config, &config_status, &waves);
    connect_queue_actions(&ui, &hub_state, &player, &queue, &config, &config_status, &waves);

    let ui_nav = ui.as_weak();
    let hub_nav = hub_state.clone();
    let browser_nav = browser.clone();
    let queue_nav = queue.clone();
    let player_nav = player.clone();
    ui.on_navigate(move |key| {
        if let Some(ui) = ui_nav.upgrade() {
            navigate(&ui, &hub_nav, &browser_nav, &queue_nav, &key, player_nav.borrow().snapshot().playing);
        }
    });

    let ui_pick = ui.as_weak();
    let hub_pick = hub_state.clone();
    let player_pick = player.clone();
    let queue_pick = queue.clone();
    let config_pick = config.clone();
    let status_pick = config_status.clone();
    let waves_pick = waves.clone();
    ui.on_pick_home(move |index| {
        if index < 0 {
            return;
        }
        let Some(row) = hub_pick.rows.borrow().get(index as usize).cloned() else {
            return;
        };
        let Some(ui) = ui_pick.upgrade() else {
            return;
        };
        pick_home_row(
            &ui,
            &hub_pick,
            &player_pick,
            &queue_pick,
            &config_pick,
            &status_pick,
            &waves_pick,
            &row,
        );
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
        note_wave_mark(&item, &hub_like, if was { "unlike" } else { "like" });
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
            note_wave_leave(&player_dislike.borrow(), &item, &hub_dislike, false);
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
        show_browser(&ui, &mut browser_album_like.borrow_mut(), &hub_album_like);
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
        show_browser(&ui, &mut browser_star_row.borrow_mut(), &hub_star_row);
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
    ui.on_toggle_catalog(move || {
        let Some(ui) = ui_catalog.upgrade() else { return };
        let open = !hub_catalog.catalog_open.get();
        hub_catalog.catalog_open.set(open);
        ui.set_catalog_open(open);
        ui.invoke_search_edited(ui.get_search());
    });

    let ui_hit = ui.as_weak();
    let hub_hit = hub_state.clone();
    let queue_hit = queue.clone();
    let player_hit = player.clone();
    let config_hit = config.clone();
    let status_hit = config_status.clone();
    let waves_hit = waves.clone();
    let browser_hit = browser.clone();
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
                let album = {
                    let browser = browser_hit.borrow();
                    let mut matching = browser.catalog.albums.iter().filter(|album| {
                        album.name.trim().eq_ignore_ascii_case(hit.title.trim())
                            && album.artist.trim().eq_ignore_ascii_case(hit.artist.trim())
                    });
                    let first = matching.next().cloned();
                    if matching.next().is_none() { first } else { None }
                };
                if let Some(album) = album {
                    open_album(&ui, &hub_hit, &config_hit, &album.id, &album.name, &album.artist, &album.cover_id);
                } else {
                    // Missing or ambiguous local editions: show library results, never guess.
                    hub_hit.catalog_open.set(false);
                    ui.set_catalog_open(false);
                    ui.set_search(hit.title.clone().into());
                    ui.invoke_set_mode(2);
                    ui.invoke_search_edited(hit.title.into());
                }
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
                    listen_ref: String::new(),
                    title: hit.title,
                    artist: hit.artist,
                    album: hit.album,
                    meta: String::new(),
                    duration_s: hit.duration_s,
                };
                *hub_hit.album_id.borrow_mut() = String::new();
                cancel_queue_load(&ui, &hub_hit);
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
    let hub_mode = hub_state.clone();
    ui.on_set_mode(move |mode| {
        let mode = mode.clamp(0, 2);
        if let Some(ui) = ui_mode.upgrade() {
            if ui.get_mode() == Mode::Mini {
                let position = ui.window().position().to_logical(ui.window().scale_factor());
                hub_mode.mini_position.set(Some((position.x, position.y)));
            }
            ui.set_mode(match mode {
                0 => Mode::Mini,
                1 => Mode::Medium,
                _ => Mode::Large,
            });
            resize(&ui, mode);
            if mode == 0 {
                if let Some((x, y)) = hub_mode.mini_position.get() {
                    ui.window().set_position(slint::LogicalPosition::new(x, y));
                }
            }
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
                let ended = player.take_natural_end();
                let left = if ended {
                    queue_tick.borrow().current().cloned()
                } else {
                    None
                };
                if let Some(item) = left.as_ref() {
                    note_wave_leave(&player, item, &hub_tick, true);
                }
                let moved = ended && queue_tick.borrow_mut().next();
                if ended {
                    maybe_extend_wave(&hub_tick, &queue_tick.borrow());
                }
                moved
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
                apply_hub_events(
                    &ui,
                    &hub_tick,
                    &browser_tick,
                    &queue_tick,
                    &player_tick,
                    &config_tick,
                    &waves_tick,
                    &status_tick,
                );
                poll_mix(&hub_tick);
                if let Some(peaks) = waves_tick.poll() {
                    show_wave(&ui, &peaks);
                }
                let player = player_tick.borrow();
                let snapshot = player.snapshot();
                push_playback(&ui, &snapshot);
                show_status(&ui, &player, &hub_tick, &status_tick);
                let playing = snapshot.playing;
                if playing != hub_tick.was_playing.get() {
                    hub_tick.was_playing.set(playing);
                    refresh_home(&ui, &hub_tick, playing);
                }
                publish_media(
                    &mut media_tick.borrow_mut(),
                    &ui,
                    &player,
                    &mut media_watch_tick.borrow_mut(),
                );
                record_playback(&ui, &hub_tick, &queue_tick.borrow(), &snapshot, false);
                remember_place(&ui, &mut watch_tick.borrow_mut(), &hub_tick);
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
    let hub_quit = hub_state.clone();
    tray.on_quit(move || shutdown(&ui_quit, &tray_quit, &hub_quit));

    let ui_min = ui.as_weak();
    ui.on_minimize(move || {
        if let Some(ui) = ui_min.upgrade() {
            let _ = ui.hide();
        }
    });
    let ui_close_btn = ui.as_weak();
    let tray_close_btn = tray.as_weak();
    let hub_close_btn = hub_state.clone();
    ui.on_close_window(move || shutdown(&ui_close_btn, &tray_close_btn, &hub_close_btn));

    let ui_close = ui.as_weak();
    let tray_close = tray.as_weak();
    let hub_close = hub_state.clone();
    ui.window().on_close_requested(move || {
        shutdown(&ui_close, &tray_close, &hub_close);
        slint::CloseRequestResponse::HideWindow
    });

    let result = ui.run();
    record_playback(&ui, &hub_state, &queue.borrow(), &player.borrow().snapshot(), true);
    result
}

struct PlaceWatch {
    started: Instant,
    last: Option<window_place::Place>,
    loaded_xy: Option<(f32, f32)>,
}

fn remember_place(ui: &AppWindow, watch: &mut PlaceWatch, hub: &HubState) {
    // The first moments report 0,0 before the window manager applies the restored position.
    if watch.started.elapsed() < Duration::from_millis(700) {
        return;
    }
    let Some(mut place) = current_place(ui, hub) else {
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

fn save_place(ui: &AppWindow, hub: &HubState) {
    if let Some(place) = current_place(ui, hub) {
        window_place::save(place);
    }
}

fn current_place(ui: &AppWindow, hub: &HubState) -> Option<window_place::Place> {
    let scale = ui.window().scale_factor();
    if scale <= 0.0 {
        return None;
    }
    let pos = ui.window().position().to_logical(scale);
    let size = ui.window().size().to_logical(scale);
    if size.width < 1.0 || size.height < 1.0 {
        return None;
    }
    if ui.get_mode() == Mode::Mini {
        hub.mini_position.set(Some((pos.x, pos.y)));
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
        mini_position: hub.mini_position.get(),
    })
}

fn same_place(left: window_place::Place, right: window_place::Place) -> bool {
    (left.x - right.x).abs() < 0.5
        && (left.y - right.y).abs() < 0.5
        && left.mode == right.mode
        && left.mini_position == right.mini_position
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
        Command::Stop => {
            record_playback(ui, hub, &queue.borrow(), &player.borrow().snapshot(), false);
            player.borrow_mut().stop();
        }
        Command::Next => {
            if let Some(item) = queue.borrow().current().cloned() {
                note_wave_leave(&player.borrow(), &item, hub, false);
            }
            let moved = queue.borrow_mut().next();
            jump(ui, player, queue, config, status, waves, hub, moved);
            return;
        }
        Command::Previous => {
            if let Some(item) = queue.borrow().current().cloned() {
                note_wave_leave(&player.borrow(), &item, hub, false);
            }
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

fn shutdown(ui: &slint::Weak<AppWindow>, tray: &slint::Weak<PlayerTray>, hub: &HubState) {
    if let Some(ui) = ui.upgrade() {
        save_place(&ui, hub);
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
    use player::PlaybackPhase;
    ui.set_playing(state.playing);
    ui.set_position(state.position_s);
    ui.set_duration(state.duration_s);
    ui.set_volume(state.volume);
    ui.set_seek_enabled(state.seekable);
    ui.set_playback_busy(matches!(state.phase, PlaybackPhase::Loading | PlaybackPhase::Buffering));
    ui.set_playback_error(state.phase == PlaybackPhase::Error);
    let message = match state.phase {
        PlaybackPhase::Idle if ui.get_queue_busy() => "Загрузка очереди…",
        PlaybackPhase::Idle => "Выберите трек",
        PlaybackPhase::Loading => "Загрузка аудио…",
        PlaybackPhase::Buffering => "Буферизация…",
        PlaybackPhase::Playing => "",
        PlaybackPhase::Paused => "Пауза",
        PlaybackPhase::Stopped => "Остановлено",
        PlaybackPhase::Ended => "Конец очереди",
        PlaybackPhase::Error => "Не удалось воспроизвести трек",
    };
    ui.set_playback_message(message.into());
    let position_known = matches!(state.phase,
        PlaybackPhase::Playing | PlaybackPhase::Paused | PlaybackPhase::Buffering);
    ui.set_time_label(if position_known { clock(state.position_s) } else { "—:—".into() }.into());
    let duration = if state.duration_s > 0.0 { state.duration_s } else { ui.get_track_duration() };
    ui.set_duration_label(if duration > 0.0 { clock(duration) } else { "—:—".into() }.into());
}

const EQ_PANEL_HEIGHT: f32 = 132.0;

fn resize(ui: &AppWindow, mode: i32) {
    // Presets include the title strip; the equalizer expands the same window.
    let (width, mut height) = match mode {
        0 => (560.0, 150.0),
        1 => (460.0, 668.0),
        _ => (1180.0, 760.0),
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
    if ui.get_playback_error() {
        ui.set_playback_message(player.notice().unwrap_or("Не удалось воспроизвести трек").into());
    }
    ui.set_status(text.into());
}

fn remember_note(ui: &AppWindow, player: &Player, hub: &HubState, fallback: &str, text: &str) {
    *hub.hub_note.borrow_mut() = text.to_string();
    show_status(ui, player, hub, fallback);
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
    ui.set_hits(slint::ModelRc::default());
    ui.set_catalog_busy(false);
    ui.set_catalog_note("Введите название или исполнителя".into());
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
    player: &RefCell<Player>,
    config: &navidrome::ServerConfig,
    waves: &std::sync::Arc<wave::WaveHub>,
    status: &str,
) {
    let events = take_events(&hub.events);
    if events.is_empty() {
        return;
    }
    let mut refresh_marks = false;
    let mut refresh_hits = false;
    let mut refresh_rows = false;
    for event in events {
        match event {
            HubEvent::CatalogLoaded(result) => {
                hub.catalog_loading.set(false);
                let mut browser = browser.borrow_mut();
                match result {
                    Ok(catalog) => {
                        browser.catalog = catalog;
                        hub.catalog_error.borrow_mut().clear();
                    }
                    Err(message) => *hub.catalog_error.borrow_mut() = message,
                }
                if browser.query.is_empty() {
                    browser.shown = browser.catalog.clone();
                    ui.set_library_busy(false);
                    ui.set_library_error(hub.catalog_error.borrow().clone().into());
                    show_browser(ui, &mut browser, hub);
                }
            }
            HubEvent::LibraryFound { ticket, result } => {
                if ticket != hub.search_generation.get() || hub.catalog_open.get() {
                    continue;
                }
                ui.set_library_busy(false);
                match result {
                    Ok(catalog) => {
                        browser.borrow_mut().shown = catalog;
                        ui.set_library_error(String::new().into());
                        show_browser(ui, &mut browser.borrow_mut(), hub);
                    }
                    Err(message) => ui.set_library_error(message.into()),
                }
            }
            HubEvent::AlbumLoaded { ticket, result } => {
                apply_album_result(ui, hub, ticket, result);
            }
            HubEvent::QueueLoaded { ticket, result } => {
                if ticket != hub.queue_generation.get() {
                    continue;
                }
                ui.set_queue_busy(false);
                match result {
                    Ok(library) => {
                        if let Some(item) = queue.borrow().current() {
                            note_wave_leave(&player.borrow(), item, hub, false);
                        }
                        hub.album_id.borrow_mut().clear();
                        clear_wave(hub);
                        *queue.borrow_mut() = library_to_queue(library);
                        let moved = queue.borrow_mut().select(0);
                        if moved {
                            jump(ui, player, queue, config, status, waves, hub, true);
                        } else {
                            player.borrow_mut().stop();
                            waves.load(None);
                            ui.set_wave(empty_wave());
                            clear_track(ui);
                            show_queue(ui, &queue.borrow(), hub);
                        }
                    }
                    Err(message) => ui.set_queue_error(message.into()),
                }
            }
            HubEvent::Cover { id, pixels } => {
                let image = pixels.map(image_from_pixels);
                hub.covers.borrow_mut().finish(id.clone(), image.clone());
                if let Some(image) = image {
                    apply_cover(ui, hub, &browser.borrow(), &queue.borrow(), &id, &image);
                }
                continue;
            }
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
                    ui.set_catalog_busy(false);
                    refresh_hits = true;
                }
            }
            HubEvent::Miss { ticket, message } => {
                if hub.catalog_open.get() && ticket == hub.search_generation.get() {
                    ui.set_catalog_busy(false);
                    ui.set_catalog_note(message.into());
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
            HubEvent::Home(feed) => {
                *hub.feed.borrow_mut() = feed;
                hub.home_error.borrow_mut().clear();
            }
            HubEvent::HomeError(message) => {
                *hub.home_error.borrow_mut() = message;
            }
            HubEvent::Mix(event) => mix_actions::apply_event(ui, hub, event),
            HubEvent::WaveStartFailed { ticket, message } => {
                if ticket == hub.queue_generation.get() {
                    hub.wave_busy.set(false);
                    *hub.hub_note.borrow_mut() = message;
                }
            }
            HubEvent::WaveStarted {
                ticket,
                batch,
                personal,
                origin,
            } => {
                if ticket != hub.queue_generation.get() {
                    continue;
                }
                hub.wave_busy.set(false);
                if batch.tracks.is_empty() || batch.session_id.is_empty() {
                    *hub.hub_note.borrow_mut() = "Волна пока пустая: попробуйте чуть позже".into();
                } else {
                    cancel_queue_load(ui, hub);
                    *hub.wave.borrow_mut() = hub::WaveLive {
                        id: batch.session_id.clone(),
                        personal,
                        origin,
                        pending: Vec::new(),
                        asking: false,
                    };
                    *hub.album_id.borrow_mut() = String::new();
                    let name = if batch.title.is_empty() {
                        "Моя волна".into()
                    } else {
                        batch.title.clone()
                    };
                    *queue.borrow_mut() = queue_from_playables(&name, &batch.tracks);
                    let moved = queue.borrow_mut().select(0);
                    jump(ui, player, queue, config, status, waves, hub, moved);
                }
            }
            HubEvent::WaveMore(batch) => {
                if batch.session_id != hub.wave.borrow().id || !hub.wave.borrow().on() {
                    continue;
                }
                hub.wave.borrow_mut().asking = false;
                let at_end = {
                    let queue = queue.borrow();
                    queue.index().map(|index| index + 1 >= queue.items().len()).unwrap_or(false)
                };
                let ended = player.borrow().snapshot().phase == player::PlaybackPhase::Ended;
                append_playables(queue, &batch.tracks);
                if ended && at_end {
                    let moved = queue.borrow_mut().next();
                    jump(ui, player, queue, config, status, waves, hub, moved);
                } else {
                    show_queue(ui, &queue.borrow(), hub);
                }
            }
            HubEvent::WaveFailed { message, played } => {
                hub.wave_busy.set(false);
                hub.wave.borrow_mut().asking = false;
                hub.wave.borrow_mut().pending.extend(played);
                *hub.hub_note.borrow_mut() = message;
            }
            HubEvent::Lists(lists) => {
                *hub.playlists.borrow_mut() = lists;
                // A completed request updates data, never steals newer navigation.
            }
            HubEvent::Liked(page) => {
                *hub.liked_songs.borrow_mut() = page.songs;
                *hub.liked_albums.borrow_mut() = page.albums;
                // The requested pane is selected before dispatching the fetch.
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
        refresh_rows = true;
    }
    if refresh_marks {
        show_browser(ui, &mut browser.borrow_mut(), hub);
        paint_track_marks(ui, &queue.borrow(), hub);
    }
    if refresh_hits && hub.catalog_open.get() {
        show_hits(ui, &hub.hits.borrow());
    }
    if refresh_rows {
        refresh_home(ui, hub, player.borrow().snapshot().playing);
    }
}

fn clear_wave(hub: &HubState) {
    *hub.wave.borrow_mut() = hub::WaveLive::default();
    hub.wave_busy.set(false);
}

fn note_wave_leave(player: &Player, item: &queue::Item, hub: &HubState, natural_end: bool) {
    let (session, origin) = {
        let wave = hub.wave.borrow();
        if !wave.on() {
            return;
        }
        (wave.id.clone(), wave.origin.clone())
    };
    let snap = player.snapshot();
    let finish = natural_end || (snap.duration_s > 0.5 && snap.position_s / snap.duration_s >= 0.9);
    let kind = if finish { "finish" } else { "skip" };
    if let Some(event) = hub::wave_event(
        kind,
        &item.id,
        &item.listen_ref,
        snap.position_s,
        snap.duration_s,
        &session,
        &origin,
    ) {
        hub.wave.borrow_mut().pending.push(event);
    }
}

fn note_wave_mark(item: &queue::Item, hub: &HubState, kind: &str) {
    let (session, origin) = {
        let wave = hub.wave.borrow();
        if !wave.on() {
            return;
        }
        (wave.id.clone(), wave.origin.clone())
    };
    if let Some(event) = hub::wave_event(kind, &item.id, &item.listen_ref, 0.0, 0.0, &session, &origin)
    {
        hub.wave.borrow_mut().pending.push(event);
    }
}

fn maybe_extend_wave(hub: &HubState, queue: &queue::Queue) {
    let Some(index) = queue.index() else { return };
    let left = queue.items().len().saturating_sub(index + 1);
    if left > 1 {
        return;
    }
    let queued: Vec<String> = queue.items()[index + 1..]
        .iter()
        .filter(|item| !item.listen_ref.is_empty())
        .map(|item| item.listen_ref.clone())
        .collect();
    let (id, played) = {
        let mut wave = hub.wave.borrow_mut();
        if !wave.on() || wave.asking {
            return;
        }
        wave.asking = true;
        (wave.id.clone(), std::mem::take(&mut wave.pending))
    };
    let client = Arc::clone(&hub.hub);
    let events = Arc::clone(&hub.events);
    std::thread::spawn(move || match client.wave_next(&id, &played, &queued) {
        Ok(batch) => push_event(&events, HubEvent::WaveMore(batch)),
        Err(message) => push_event(
            &events,
            HubEvent::WaveFailed {
                message,
                played,
            },
        ),
    });
}

fn spawn_home(hub: &HubState) {
    fetch_mix(hub);
    let client = Arc::clone(&hub.hub);
    let events = Arc::clone(&hub.events);
    std::thread::spawn(move || {
        match client.home() {
            Ok(feed) => push_event(&events, HubEvent::Home(feed)),
            Err(message) => push_event(&events, HubEvent::HomeError(message)),
        }
    });
}


fn spawn_fetch(hub: &HubState, job: impl FnOnce(&hub::Hub) -> HubEvent + Send + 'static) {
    let client = Arc::clone(&hub.hub);
    let events = Arc::clone(&hub.events);
    std::thread::spawn(move || {
        push_event(&events, job(&client));
    });
}

fn navigate(
    ui: &AppWindow,
    hub: &HubState,
    browser: &RefCell<Browser>,
    queue: &RefCell<queue::Queue>,
    key: &str,
    playing: bool,
) {
    let pane = match key {
        "home" => home::Pane::Home,
        "liked" => home::Pane::Liked,
        "playlists" => home::Pane::Playlists,
        "artists" | "albums" => home::Pane::Library,
        _ => return,
    };
    close_album(ui, hub);
    ui.set_library_busy(hub.catalog_loading.get());
    ui.set_library_error(hub.catalog_error.borrow().clone().into());
    hub.search_generation.set(hub.search_generation.get().wrapping_add(1));
    hub.catalog_open.set(false);
    ui.set_catalog_open(false);
    clear_catalog(ui, hub);
    ui.set_search(String::new().into());
    hub.pane.set(pane);
    {
        let mut browser = browser.borrow_mut();
        browser.query.clear();
        browser.artist_id.clear();
        browser.shown = browser.catalog.clone();
        show_browser(ui, &mut browser, hub);
    }
    show_queue(ui, &queue.borrow(), hub);
    refresh_home(ui, hub, playing);
    match pane {
        home::Pane::Home => spawn_home(hub),
        home::Pane::Liked => spawn_fetch(hub, |client| match client.liked() {
            Ok(page) => HubEvent::Liked(page),
            Err(message) => HubEvent::Note(message),
        }),
        home::Pane::Playlists => spawn_fetch(hub, |client| match client.playlists() {
            Ok(lists) => HubEvent::Lists(lists),
            Err(message) => HubEvent::Note(message),
        }),
        _ => {}
    }
}

fn show_library_pane(ui: &AppWindow, hub: &HubState) {
    hub.pane.set(home::Pane::Library);
    hub.catalog_open.set(false);
    ui.set_catalog_open(false);
    ui.set_home_open(false);
    ui.set_home_page("library".into());
    ui.set_home_title("Исполнители и альбомы".into());
}

fn refresh_home(ui: &AppWindow, hub: &HubState, playing: bool) {
    let pane = hub.pane.get();
    if pane == home::Pane::Library {
        ui.set_home_open(false);
        ui.set_home_page("library".into());
        ui.set_home_title("Исполнители и альбомы".into());
        return;
    }
    ui.set_home_open(true);
    let (page, title) = match pane {
        home::Pane::Home => ("home", home::greeting(home::local_hour(), &hub.user_name)),
        home::Pane::Playlists => ("playlists", "Плейлисты".into()),
        home::Pane::Liked => ("liked", "Понравившиеся".into()),
        home::Pane::Mix => {
            let mix = hub.mix.borrow();
            ("mix", if mix.title.is_empty() { "Микс дня".into() } else { mix.title.clone() })
        }
        home::Pane::Taste => ("taste", "Ваш музыкальный вкус".into()),
        home::Pane::Library => unreachable!(),
    };
    ui.set_home_page(page.into());
    ui.set_home_title(title.into());
    let personal = {
        let wave = hub.wave.borrow();
        wave.personal && wave.on()
    };
    let caption = home::wave_caption(hub.wave_busy.get(), personal, playing);
    let mut rows = match pane {
        home::Pane::Library => unreachable!(),
        home::Pane::Home => home::home_rows(caption, &hub.mix.borrow(), &hub.feed.borrow()),
        home::Pane::Playlists => home::playlist_rows(&hub.playlists.borrow()),
        home::Pane::Liked => home::liked_rows(&hub.liked_songs.borrow(), &hub.liked_albums.borrow()),
        home::Pane::Mix => home::mix_rows(&hub.mix.borrow()),
        home::Pane::Taste if hub.mix_control.taste_loading.get() || hub.mix_control.taste_error.get() => Vec::new(),
        home::Pane::Taste => home::taste_rows(&hub.taste.borrow(), &hub.taste_picked.borrow()),
    };
    for row in &mut rows {
        if (hub.mix_control.mode_busy.get() && row.kind == "mode")
            || (hub.mix_control.taste_saving.get() && (row.kind == "taste" || row.key == "save")) {
            row.kind = "disabled";
        }
    }
    ui.set_home_notice(if pane == home::Pane::Home && !hub.home_error.borrow().is_empty() {
        hub.home_error.borrow().as_str().into()
    } else {
        hub.hub_note.borrow().as_str().into()
    });
    if *hub.rows.borrow() == rows {
        return;
    }
    let previous = ui.get_home_rows();
    let previous_rows = hub.rows.borrow();
    let model: Vec<HomeRow> = rows.iter().enumerate().map(|(source, row)| {
        // Polling the mix may change captions, not artwork. Keep live images even
        // after the bounded cache evicts them; missing art is not retried per poll.
        let prior = previous_rows.iter().position(|old| {
            old.kind == row.kind && old.key == row.key && old.cover_id == row.cover_id
        }).and_then(|index| previous.row_data(index));
        let (cover, has_cover) = if let Some(prior) = prior {
            (prior.cover, prior.has_cover)
        } else {
            let cover = request_cover(hub, &row.cover_id);
            let has_cover = cover.is_some();
            (cover.unwrap_or_default(), has_cover)
        };
        HomeRow {
            source: source as i32,
            kind: row.kind.into(),
            title: row.title.as_str().into(),
            detail: row.detail.as_str().into(),
            accent: row.accent,
            has_cover,
            cover,
        }
    }).collect();
    drop(previous_rows);
    let sections: Vec<HomeSection> = if pane == home::Pane::Home {
        home::sections(&rows).into_iter().map(|(title, range)| HomeSection {
            title: title.into(),
            items: Rc::new(slint::VecModel::from(model[range].to_vec())).into(),
        }).collect()
    } else {
        Vec::new()
    };
    let features: Vec<HomeRow> = if pane == home::Pane::Home {
        model.iter().filter(|row| row.kind == "wave" || row.kind == "mix").cloned().collect()
    } else {
        Vec::new()
    };
    *hub.rows.borrow_mut() = rows;
    ui.set_home_features(Rc::new(slint::VecModel::from(features)).into());
    ui.set_home_sections(Rc::new(slint::VecModel::from(sections)).into());
    ui.set_home_rows(Rc::new(slint::VecModel::from(model)).into());
}

fn pick_home_row(
    ui: &AppWindow,
    hub: &HubState,
    player: &RefCell<Player>,
    queue: &RefCell<queue::Queue>,
    config: &navidrome::ServerConfig,
    status: &str,
    waves: &std::sync::Arc<wave::WaveHub>,
    row: &home::Row,
) {
    let playing = player.borrow().snapshot().playing;
    match row.kind {
        "section" | "note" | "disabled" => {}
        "wave" => {
            let personal = {
                let wave = hub.wave.borrow();
                wave.personal && wave.on()
            };
            if personal {
                if player.borrow().is_idle() {
                    let _ = play_current(ui, player, queue, config, waves, hub);
                } else {
                    player.borrow_mut().toggle();
                }
                let player = player.borrow();
                push_playback(ui, &player.snapshot());
                show_status(ui, &player, hub, status);
            } else if !hub.wave_busy.get() {
                begin_wave(ui, hub, String::new(), "home_card");
            }
        }
        "mix" => {
            let onboarding = hub.mix.borrow().phase == hub::MixPhase::Onboarding;
            if onboarding {
                mix_actions::open_taste(ui, hub);
            } else {
                hub.pane.set(home::Pane::Mix);
                fetch_mix(hub);
            }
        }
        "album" => open_album(ui, hub, config, &row.key, &row.title, "", &row.cover_id),
        "radio" => {
            if !hub.wave_busy.get() {
                begin_wave(ui, hub, row.key.clone(), "radio");
            }
        }
        "playlist" => open_saved_playlist(ui, hub, config, &row.key),
        "track" => {
            let index = row.key.parse::<usize>().unwrap_or(usize::MAX);
            let (name, songs) = if hub.pane.get() == home::Pane::Mix {
                let mix = hub.mix.borrow();
                (mix.title.clone(), mix.tracks.clone())
            } else {
                ("Понравившиеся".into(), hub.liked_songs.borrow().clone())
            };
            play_songs(ui, hub, player, queue, config, status, waves, &name, &songs, index);
        }
        "mode" => set_mix_mode(hub, &row.key),
        "taste" => toggle_taste(hub, &row.key),
        "action" => match row.key.as_str() {
            "play" => {
                let (name, songs) = if hub.pane.get() == home::Pane::Mix {
                    let mix = hub.mix.borrow();
                    (mix.title.clone(), mix.tracks.clone())
                } else {
                    ("Понравившиеся".into(), hub.liked_songs.borrow().clone())
                };
                let index = songs.iter().position(hub::Playable::can_play).unwrap_or(0);
                play_songs(ui, hub, player, queue, config, status, waves, &name, &songs, index);
            }
            "rebuild" => rebuild_mix(hub),
            "save" => save_taste(hub),
            "taste" => mix_actions::open_taste(ui, hub),
            "open-mix-playlist" => {
                let id = hub.mix.borrow().playlist_id.clone();
                if !id.is_empty() {
                    open_saved_playlist(ui, hub, config, &id);
                }
            }
            _ => {}
        },
        _ => {}
    }
    refresh_home(ui, hub, player.borrow().snapshot().playing || playing);
}

fn begin_wave(ui: &AppWindow, hub: &HubState, artist_id: String, origin: &'static str) {
    cancel_queue_load(ui, hub);
    let ticket = hub.queue_generation.get();
    hub.wave_busy.set(true);
    let client = Arc::clone(&hub.hub);
    let events = Arc::clone(&hub.events);
    let personal = origin == "home_card";
    std::thread::spawn(move || {
        for attempt in 0..3 {
            match client.wave_start(&artist_id, origin) {
                Ok(batch) if !batch.tracks.is_empty() || !batch.pending || attempt == 2 => {
                    push_event(
                        &events,
                        HubEvent::WaveStarted {
                            ticket,
                            batch,
                            personal,
                            origin: origin.to_string(),
                        },
                    );
                    return;
                }
                Ok(_) => std::thread::sleep(Duration::from_secs(2)),
                Err(message) => {
                    push_event(
                        &events,
                        HubEvent::WaveStartFailed { ticket, message },
                    );
                    return;
                }
            }
        }
    });
}






fn open_saved_playlist(
    ui: &AppWindow,
    hub: &HubState,
    config: &navidrome::ServerConfig,
    playlist_id: &str,
) {
    cancel_queue_load(ui, hub);
    ui.set_queue_busy(true);
    let ticket = hub.queue_generation.get();
    let config = config.clone();
    let playlist_id = playlist_id.to_string();
    let events = Arc::clone(&hub.events);
    std::thread::spawn(move || {
        let result = navidrome::playlist_by_id(&config, &playlist_id);
        push_event(&events, HubEvent::QueueLoaded { ticket, result });
    });
}

fn play_songs(
    ui: &AppWindow,
    hub: &HubState,
    player: &RefCell<Player>,
    queue: &RefCell<queue::Queue>,
    config: &navidrome::ServerConfig,
    status: &str,
    waves: &std::sync::Arc<wave::WaveHub>,
    name: &str,
    songs: &[hub::Playable],
    index: usize,
) {
    if !songs.get(index).is_some_and(hub::Playable::can_play) {
        remember_note(ui, &player.borrow(), hub, status, "Трек пока недоступен");
        return;
    }
    let queue_next = queue_from_playables(name, songs);
    if queue_next.is_empty() {
        remember_note(ui, &player.borrow(), hub, status, "Нет треков");
        return;
    }
    cancel_queue_load(ui, hub);
    let index = songs[..index].iter().filter(|song| song.can_play()).count();
    *hub.album_id.borrow_mut() = String::new();
    clear_wave(hub);
    *queue.borrow_mut() = queue_next;
    let moved = queue.borrow_mut().select(index);
    jump(ui, player, queue, config, status, waves, hub, moved);
}

fn queue_from_playables(name: &str, songs: &[hub::Playable]) -> queue::Queue {
    let items = songs
        .iter()
        .filter(|song| song.can_play())
        .map(|song| queue::Item {
            id: song.nd_song_id.clone(),
            path: String::new(),
            listen_ref: song.reference.clone(),
            title: song.title.clone(),
            artist: song.artist.clone(),
            album: song.album.clone(),
            meta: String::new(),
            duration_s: song.duration_s,
        })
        .collect();
    queue::Queue::from_tracks(name, items)
}

fn append_playables(queue: &RefCell<queue::Queue>, songs: &[hub::Playable]) {
    let mut queue = queue.borrow_mut();
    let have: HashSet<String> = queue
        .items()
        .iter()
        .map(|item| {
            if item.listen_ref.is_empty() {
                item.id.clone()
            } else {
                item.listen_ref.clone()
            }
        })
        .collect();
    let items = songs.iter().filter(|song| song.can_play()).filter(|song| {
        let key = if song.reference.is_empty() {
            song.nd_song_id.as_str()
        } else {
            song.reference.as_str()
        };
        !have.contains(key)
    });
    queue.extend(items.map(|song| queue::Item {
        id: song.nd_song_id.clone(),
        path: String::new(),
        listen_ref: song.reference.clone(),
        title: song.title.clone(),
        artist: song.artist.clone(),
        album: song.album.clone(),
        meta: String::new(),
        duration_s: song.duration_s,
    }));
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

fn show_browser(ui: &AppWindow, browser: &mut Browser, hub: &HubState) {
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
        .map(|album| {
            let cover = if hub.pane.get() == home::Pane::Library {
                request_cover(hub, &album.cover_id)
            } else {
                hub.covers.borrow().images.get(&album.cover_id).cloned().flatten()
            };
            AlbumItem {
                id: album.id.as_str().into(),
                title: album.name.as_str().into(),
                artist: album_line(album).into(),
                starred: hub.marks.borrow().albums.contains(&album.id),
                has_cover: cover.is_some(),
                cover: cover.unwrap_or_default(),
            }
        })
        .collect::<Vec<_>>();
    ui.set_artists(slint::ModelRc::from(Rc::new(slint::VecModel::from(
        artists,
    ))));
    ui.set_albums(slint::ModelRc::from(Rc::new(slint::VecModel::from(albums))));
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
            listen_ref: String::new(),
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
    play_current_at(ui, player, queue, config, waves, hub, 0.0, false)
}

fn play_current_at(
    ui: &AppWindow,
    player: &RefCell<Player>,
    queue: &RefCell<queue::Queue>,
    config: &navidrome::ServerConfig,
    waves: &std::sync::Arc<wave::WaveHub>,
    hub: &HubState,
    position_s: f32,
    paused: bool,
) -> Result<(), String> {
    let (target, cookie, cover_id) = {
        let queue = queue.borrow();
        let cover_id = queue
            .current()
            .map(|item| item.id.clone())
            .unwrap_or_default();
        let play = current_target(&queue, config, hub);
        (
            play.as_ref().map(|(url, _)| url.clone()),
            play.map(|(_, cookie)| cookie).unwrap_or_default(),
            cover_id,
        )
    };
    {
        let queue = queue.borrow();
        show_queue(ui, &queue, hub);
    }
    ui.set_wave(empty_wave());
    // A Hub stream needs the session cookie. The waveform reader has no cookie, so it stays flat.
    waves.load(if cookie.is_empty() {
        target.clone()
    } else {
        None
    });
    let loaded = match target {
        Some(target) => player.borrow_mut().load_cookie_at(&target, &cookie, position_s, paused),
        None => Err("Нет трека".into()),
    };
    maybe_extend_wave(hub, &queue.borrow());
    // The previous cover must not stay on the next track when this one has none.
    show_cover(ui, hub, &cover_id);
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

/// URL plus a Hub session cookie. The cookie is empty for a library song or a local file.
fn current_target(
    queue: &queue::Queue,
    config: &navidrome::ServerConfig,
    hub: &HubState,
) -> Option<(String, String)> {
    let item = queue.current()?;
    if !item.id.is_empty() {
        Some((navidrome::stream_url(config, &item.id), String::new()))
    } else if !item.listen_ref.is_empty() {
        let url = hub.hub.listen_url(&item.listen_ref);
        let cookie = hub.hub.session_token();
        if url.is_empty() || cookie.is_empty() {
            return None;
        }
        Some((url, cookie))
    } else if !item.path.is_empty() {
        Some((item.path.clone(), String::new()))
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
    if !queue.is_empty() {
        hub.playback_ready.set(true);
    }
    let query = ui.get_queue_search().to_string();
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
            meta: item.artist.clone().into(),
            duration: row_duration(item).into(),
            current: current == Some(index),
        })
        .collect();
    ui.set_queue(slint::ModelRc::from(Rc::new(slint::VecModel::from(rows))));
    let title = if queue.is_empty() || queue.name().is_empty() {
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
    ui.set_track_duration(item.duration_s as f32);
}

fn show_cover(ui: &AppWindow, hub: &HubState, song_id: &str) {
    let cover = request_cover(hub, song_id);
    ui.set_has_cover(cover.is_some());
    ui.set_cover(cover.unwrap_or_default());
}

fn apply_home_cover(
    model: &slint::ModelRc<HomeRow>,
    rows: &[home::Row],
    id: &str,
    image: &slint::Image,
) {
    for index in 0..model.row_count() {
        let Some(mut displayed) = model.row_data(index) else { continue };
        let Some(row) = usize::try_from(displayed.source).ok().and_then(|source| rows.get(source)) else {
            continue;
        };
        if row.cover_id == id && displayed.kind == row.kind && displayed.title == row.title.as_str() {
            displayed.cover = image.clone();
            displayed.has_cover = true;
            model.set_row_data(index, displayed);
        }
    }
}

fn apply_cover(
    ui: &AppWindow,
    hub: &HubState,
    browser: &Browser,
    queue: &queue::Queue,
    id: &str,
    image: &slint::Image,
) {
    let rows = hub.rows.borrow();
    apply_home_cover(&ui.get_home_rows(), &rows, id, image);
    for section in ui.get_home_sections().iter() {
        apply_home_cover(&section.items, &rows, id, image);
    }
    let albums = ui.get_albums();
    for (index, album) in browser.visible_albums.iter().enumerate() {
        if album.cover_id != id {
            continue;
        }
        if let Some(mut displayed) = albums.row_data(index) {
            if displayed.id == album.id.as_str() {
                displayed.cover = image.clone();
                displayed.has_cover = true;
                albums.set_row_data(index, displayed);
            }
        }
    }
    if hub.album_preview.borrow().as_ref().is_some_and(|album| album.cover_id == id) {
        ui.set_album_cover(image.clone());
        ui.set_album_has_cover(true);
    }
    if queue.current().is_some_and(|item| item.id == id) {
        ui.set_cover(image.clone());
        ui.set_has_cover(true);
    }
}

fn clear_track(ui: &AppWindow) {
    ui.set_track_title("Нет трека".into());
    ui.set_track_artist(String::new().into());
    ui.set_track_album(String::new().into());
    ui.set_track_meta(String::new().into());
    ui.set_track_duration(0.0);
    ui.set_has_cover(false);
    ui.set_queue(slint::ModelRc::from(Rc::new(slint::VecModel::from(Vec::<
        QueueItem,
    >::new(
    )))));
    ui.set_queue_title("Очередь".into());
    ui.set_queue_total(String::new().into());
}

fn row_duration(item: &queue::Item) -> String {
    if item.duration_s == 0 {
        "—".into()
    } else {
        clock(item.duration_s as f32)
    }
}

fn cancel_queue_load(ui: &AppWindow, hub: &HubState) {
    hub.queue_generation.set(hub.queue_generation.get().wrapping_add(1));
    hub.wave_busy.set(false);
    ui.set_queue_busy(false);
    ui.set_queue_error(String::new().into());
}

fn record_playback(
    ui: &AppWindow,
    hub: &HubState,
    queue: &queue::Queue,
    state: &player::PlaybackState,
    force: bool,
) {
    if !hub.playback_ready.get() {
        return;
    }
    let wave = hub.wave.borrow();
    let result = hub.playback_store.borrow_mut().record(
        queue, state, &hub.album_id.borrow(), wave.on().then_some(&*wave), force,
    );
    if let Err(message) = result {
        *hub.hub_note.borrow_mut() = message.clone();
        ui.set_status(message.into());
    }
}

fn decode_cover(bytes: &[u8]) -> Option<CoverPixels> {
    let image = image::load_from_memory(bytes).ok()?;
    let image = if image.width() > 256 || image.height() > 256 {
        image.thumbnail(256, 256)
    } else {
        image
    }.into_rgba8();
    let (width, height) = image.dimensions();
    if width == 0 || height == 0 {
        return None;
    }
    Some(CoverPixels { bytes: image.into_raw(), width, height })
}

fn image_from_pixels(pixels: CoverPixels) -> slint::Image {
    let buffer = slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(
        &pixels.bytes,
        pixels.width,
        pixels.height,
    );
    slint::Image::from_rgba8(buffer)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn late_cover_updates_only_current_identity_and_every_duplicate() {
        let rows = vec![
            home::Row {
                kind: "album", key: "new".into(), title: "Same title".into(),
                detail: String::new(), cover_id: "new-art".into(), accent: false,
            },
            home::Row {
                kind: "album", key: "old".into(), title: "Same title".into(),
                detail: String::new(), cover_id: "old-art".into(), accent: false,
            },
        ];
        let displayed = |source| HomeRow {
            source, kind: "album".into(), title: "Same title".into(),
            ..HomeRow::default()
        };
        let model: slint::ModelRc<HomeRow> = Rc::new(slint::VecModel::from(vec![
            displayed(0), displayed(1), displayed(1), displayed(99),
        ])).into();
        apply_home_cover(&model, &rows, "old-art", &slint::Image::default());
        assert!(!model.row_data(0).unwrap().has_cover);
        assert!(model.row_data(1).unwrap().has_cover);
        assert!(model.row_data(2).unwrap().has_cover);
        assert!(!model.row_data(3).unwrap().has_cover);
    }

    #[test]
    fn cover_cache_deduplicates_pending_and_missing_art_and_bounds_images() {
        let mut cache = CoverCache::default();
        assert!(cache.reserve("missing"));
        assert!(!cache.reserve("missing"));
        cache.finish("missing".into(), None);
        assert!(!cache.reserve("missing"));
        for index in 0..COVER_CACHE_LIMIT {
            let id = format!("album-{index}");
            assert!(cache.reserve(&id));
            cache.finish(id, Some(slint::Image::default()));
        }
        assert_eq!(cache.images.len(), COVER_CACHE_LIMIT);
        assert_eq!(cache.order.len(), COVER_CACHE_LIMIT);
        assert!(cache.pending.is_empty());
        assert!(!cache.images.contains_key("missing"));
    }

    #[test]
    fn decodes_a_png_cover() {
        use image::ImageEncoder;
        let mut bytes = Vec::new();
        image::codecs::png::PngEncoder::new(&mut bytes)
            .write_image(&[10, 20, 30, 255], 1, 1, image::ExtendedColorType::Rgba8)
            .unwrap();
        let image = image_from_pixels(decode_cover(&bytes).expect("png"));
        let pixels = image.to_rgba8().expect("pixels");
        assert_eq!((pixels.width(), pixels.height()), (1, 1));
        assert_eq!(pixels.as_bytes(), &[10, 20, 30, 255]);
    }

    #[test]
    fn a_json_error_is_not_a_cover() {
        assert!(decode_cover(br#"{"status":"failed"}"#).is_none());
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
                    listen_ref: String::new(),
                    title: "Visions".into(),
                    artist: "Damned Anthem".into(),
                    album: "Visions".into(),
                    meta: "MP3".into(),
                    duration_s: 200,
                },
                queue::Item {
                    id: "b".into(),
                    path: String::new(),
                    listen_ref: String::new(),
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
