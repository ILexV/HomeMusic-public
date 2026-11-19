//! An album preview never touches playback until an explicit play or append action.
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

use slint::ComponentHandle;

use crate::{
    cancel_queue_load, clear_wave, jump, library_to_queue, navidrome, note_wave_leave,
    player::Player, push_event, queue, request_cover, row_duration, show_queue, wave,
    AlbumPreview, AppWindow, HubEvent, HubState, QueueItem,
};

pub fn open_album(
    ui: &AppWindow,
    hub: &HubState,
    config: &navidrome::ServerConfig,
    id: &str,
    title: &str,
    artist: &str,
    cover_id: &str,
) {
    if id.is_empty() {
        return;
    }
    let ticket = begin_preview(ui, hub, title, artist, cover_id);
    let config = config.clone();
    let id = id.to_string();
    let events = Arc::clone(&hub.events);
    std::thread::spawn(move || {
        let result = navidrome::album_tracks(&config, &id).map(|tracks| (id, tracks));
        push_event(&events, HubEvent::AlbumLoaded { ticket, result });
    });
}

pub(super) fn open_song_album(ui: &AppWindow, hub: &HubState, config: &navidrome::ServerConfig, song_id: &str) {
    if song_id.is_empty() { return; }
    let ticket = begin_preview(ui, hub, "Альбом", "", song_id);
    let config = config.clone();
    let id = song_id.to_owned();
    let events = Arc::clone(&hub.events);
    std::thread::spawn(move || {
        let result = navidrome::song_album(&config, &id);
        push_event(&events, HubEvent::AlbumLoaded { ticket, result });
    });
}

fn begin_preview(ui: &AppWindow, hub: &HubState, title: &str, artist: &str, cover_id: &str) -> u64 {
    let ticket = hub.album_generation.get().wrapping_add(1);
    hub.album_generation.set(ticket);
    *hub.album_preview.borrow_mut() = Some(AlbumPreview {
        id: String::new(),
        cover_id: cover_id.to_string(),
        catalog_ref: String::new(),
        tracks: Vec::new(),
    });
    ui.set_album_title(title.into());
    ui.set_album_artist(artist.into());
    ui.set_album_tracks(slint::ModelRc::default());
    ui.set_album_total(String::new().into());
    ui.set_album_error(String::new().into());
    ui.set_album_note(String::new().into());
    ui.set_album_can_download(false);
    ui.set_album_busy(true);
    ui.set_album_open(true);
    let cover = request_cover(hub, cover_id);
    ui.set_album_has_cover(cover.is_some());
    ui.set_album_cover(cover.unwrap_or_default());
    ticket
}

pub fn close_album(ui: &AppWindow, hub: &HubState) {
    hub.album_generation.set(hub.album_generation.get().wrapping_add(1));
    hub.album_preview.borrow_mut().take();
    ui.set_album_open(false);
    ui.set_album_busy(false);
}

pub fn apply_album_result(
    ui: &AppWindow,
    hub: &HubState,
    ticket: u64,
    result: Result<(String, navidrome::LibraryQueue), String>,
) {
    if ticket != hub.album_generation.get() || !ui.get_album_open() {
        return;
    }
    let mut preview = hub.album_preview.borrow_mut();
    let Some(preview) = preview.as_mut() else { return };
    ui.set_album_busy(false);
    match result {
        Ok((id, library)) => {
            preview.id = id;
            let album = library_to_queue(library);
            ui.set_album_title(album.name().into());
            ui.set_album_total(album.total_label().into());
            if let Some(first) = album.items().first() {
                if ui.get_album_artist().is_empty() {
                    ui.set_album_artist(first.artist.clone().into());
                }
            }
            let rows = album.items().iter().enumerate().map(|(index, item)| QueueItem {
                source: index as i32,
                number: (index + 1).to_string().into(),
                title: item.title.clone().into(),
                meta: item.artist.clone().into(),
                duration: row_duration(item).into(),
                ..QueueItem::default()
            }).collect::<Vec<_>>();
            ui.set_album_tracks(slint::ModelRc::from(Rc::new(slint::VecModel::from(rows))));
            preview.tracks = album.into_items();
        }
        Err(message) => ui.set_album_error(message.into()),
    }
}

pub fn connect_album_actions(
    ui: &AppWindow,
    hub: &Rc<HubState>,
    player: &Rc<RefCell<Player>>,
    queue: &Rc<RefCell<queue::Queue>>,
    config: &Rc<navidrome::ServerConfig>,
    status: &Rc<String>,
    waves: &Arc<wave::WaveHub>,
) {
    let weak = ui.as_weak();
    let state = hub.clone();
    ui.on_close_album(move || {
        if let Some(ui) = weak.upgrade() {
            close_album(&ui, &state);
        }
    });

    let weak = ui.as_weak();
    ui.on_album_play(move || {
        if let Some(ui) = weak.upgrade() {
            ui.invoke_album_play_row(0);
        }
    });

    let weak = ui.as_weak();
    let hub_play = hub.clone();
    let queue_play = queue.clone();
    let player_play = player.clone();
    let config_play = config.clone();
    let status_play = status.clone();
    let waves_play = waves.clone();
    ui.on_album_play_row(move |index| {
        let Some(ui) = weak.upgrade() else { return };
        if index < 0 || ui.get_album_busy() || !ui.get_album_error().is_empty() {
            return;
        }
        let preview = hub_play.album_preview.borrow();
        let Some(preview) = preview.as_ref() else { return };
        let index = index as usize;
        if index >= preview.tracks.len() { return; }
        cancel_queue_load(&ui, &hub_play);
        let album = queue::Queue::from_tracks(ui.get_album_title().to_string(), preview.tracks.clone());
        play_album(&ui, &hub_play, &player_play, &queue_play, &config_play, &status_play,
            &waves_play, &preview.id, album, index);
        ui.set_album_note("Очередь заменена альбомом".into());
    });

    let weak = ui.as_weak();
    let hub_add = hub.clone();
    let queue_add = queue.clone();
    let player_add = player.clone();
    ui.on_album_enqueue(move || {
        let Some(ui) = weak.upgrade() else { return };
        if ui.get_album_busy() || !ui.get_album_error().is_empty() {
            return;
        }
        let preview = hub_add.album_preview.borrow();
        let Some(preview) = preview.as_ref().filter(|preview| !preview.tracks.is_empty()) else { return };
        cancel_queue_load(&ui, &hub_add);
        append_album(&ui, &hub_add, &player_add, &mut queue_add.borrow_mut(), &preview.id,
            ui.get_album_title().as_str(), preview.tracks.iter().cloned());
        ui.set_album_note(format!("Добавлено в очередь: {}", preview.tracks.len()).into());
    });
}

pub(super) fn play_album(
    ui: &AppWindow, hub: &HubState, player: &RefCell<Player>, queue: &RefCell<queue::Queue>,
    config: &navidrome::ServerConfig, status: &str, waves: &Arc<wave::WaveHub>,
    id: &str, mut album: queue::Queue, index: usize,
) {
    if !album.select(index) { return; }
    if let Some(item) = queue.borrow().current() {
        note_wave_leave(&player.borrow(), item, hub, false);
    }
    clear_wave(hub);
    id.clone_into(&mut hub.album_id.borrow_mut());
    album.inherit_preferences(&queue.borrow());
    *queue.borrow_mut() = album;
    jump(ui, player, queue, config, status, waves, hub, true);
}

pub(super) fn append_album(
    ui: &AppWindow, hub: &HubState, player: &RefCell<Player>, queue: &mut queue::Queue,
    id: &str, name: &str, tracks: impl ExactSizeIterator<Item = queue::Item>,
) {
    if tracks.len() == 0 { return; }
    player.borrow_mut().cancel_preload();
    if queue.is_empty() {
        let mut album = queue::Queue::from_tracks(name, tracks.collect());
        album.select(0);
        album.inherit_preferences(queue);
        *queue = album;
        id.clone_into(&mut hub.album_id.borrow_mut());
        if let Some(item) = queue.current() { crate::show_cover(ui, hub, &item.id); }
    } else {
        queue.extend(tracks);
        if hub.album_id.borrow().as_str() != id { hub.album_id.borrow_mut().clear(); }
    }
    show_queue(ui, queue, hub);
}
