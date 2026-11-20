//! An album preview never touches playback until an explicit play or append action.
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

use slint::{ComponentHandle, Model};

use crate::{
    cancel_queue_load, clear_wave, jump, library_to_queue, navidrome, note_wave_leave,
    player::Player, push_event, queue, request_cover, row_duration, show_queue, wave,
    AlbumPreview, AppWindow, HubEvent, HubState, QueueItem,
};

pub(super) enum Tracks {
    Catalog(serde_json::Value),
    Library(navidrome::LibraryQueue),
}

fn load_tracks(client: &crate::hub::Hub, config: &navidrome::ServerConfig, id: String) -> Result<(String, Tracks), String> {
    let tracks = match client.library_album(&id) {
        Ok(value) => Tracks::Catalog(value),
        Err(_) => Tracks::Library(navidrome::album_tracks(config, &id)?),
    };
    Ok((id, tracks))
}

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
    let client = Arc::clone(&hub.hub);
    std::thread::spawn(move || {
        let result = load_tracks(&client, &config, id);
        push_event(&events, HubEvent::AlbumLoaded { ticket, result });
    });
}

pub(super) fn show_explore_album(ctx: &crate::feature_context::Context, id: &str, catalog: bool, title: &str, artist: &str) {
    let Some(ui) = ctx.ui.upgrade() else { return };
    if id.is_empty() { return; }
    if ui.get_mode() == crate::Mode::Mini { ui.invoke_set_mode(1); }
    if !catalog {
        open_album(&ui, &ctx.hub, &ctx.config, id, title, artist, id);
        return;
    }
    let ticket = begin_preview(&ui, &ctx.hub, title, artist, "");
    if let Some(preview) = ctx.hub.album_preview.borrow_mut().as_mut() {
        id.clone_into(&mut preview.catalog_ref);
    }
    ui.set_album_can_download(true);
    let client = Arc::clone(&ctx.hub.hub);
    let events = Arc::clone(&ctx.hub.events);
    let reference = id.to_owned();
    std::thread::spawn(move || {
        let result = client.catalog_album(&reference).map(|v| (String::new(), Tracks::Catalog(v)));
        push_event(&events, HubEvent::AlbumLoaded { ticket, result });
    });
}

pub(super) fn open_song_album(ui: &AppWindow, hub: &HubState, config: &navidrome::ServerConfig, song_id: &str) {
    if song_id.is_empty() { return; }
    let ticket = begin_preview(ui, hub, "Альбом", "", song_id);
    let config = config.clone();
    let id = song_id.to_owned();
    let events = Arc::clone(&hub.events);
    let client = Arc::clone(&hub.hub);
    std::thread::spawn(move || {
        let result = navidrome::song_album_id(&config, &id).and_then(|id| load_tracks(&client, &config, id));
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
    result: Result<(String, Tracks), String>,
) {
    if ticket != hub.album_generation.get() || !ui.get_album_open() {
        return;
    }
    ui.set_album_busy(false);
    match result {
        Ok((id, Tracks::Catalog(value))) => {
            if let Some(preview) = hub.album_preview.borrow_mut().as_mut() { preview.id = id; }
            show_catalog(ui, hub, &value);
        }
        Ok((id, Tracks::Library(library))) => {
            if let Some(preview) = hub.album_preview.borrow_mut().as_mut() { preview.id = id; }
            let album = library_to_queue(library);
            ui.set_album_title(album.name().into());
            ui.set_album_total(album.total_label().into());
            if let Some(first) = album.items().first() {
                if ui.get_album_artist().is_empty() {
                    ui.set_album_artist(first.artist.clone().into());
                }
            }
            show_rows(ui, hub, album, std::iter::repeat(true));
        }
        Err(message) => ui.set_album_error(message.into()),
    }
}

pub(super) fn show_catalog(ui: &AppWindow, hub: &HubState, value: &serde_json::Value) {
    let title = value["title"].as_str().unwrap_or_default();
    ui.set_album_title(title.into());
    ui.set_album_artist(value["artist"].as_str().unwrap_or_default().into());
    let (items, owned): (Vec<_>, Vec<_>) = value["tracks"].as_array().into_iter().flatten().filter_map(|row| {
        let t = crate::hub::playable(row)?;
        Some((queue::Item { id: t.nd_song_id, listen_ref: t.reference, title: t.title,
            artist: t.artist, album: t.album, duration_s: t.duration_s, path: String::new(), meta: String::new() },
            row["owned"].as_bool().unwrap_or(false)))
    }).unzip();
    let album = queue::Queue::from_tracks(title, items);
    ui.set_album_total(album.total_label().into());
    show_rows(ui, hub, album, owned.into_iter());
}

fn show_rows(ui: &AppWindow, hub: &HubState, album: queue::Queue, owned: impl Iterator<Item = bool>) {
    let marks = hub.marks.borrow();
    let rows = album.items().iter().zip(owned).enumerate().map(|(index, (item, owned))| QueueItem {
        source: index as i32,
        number: (index + 1).to_string().into(),
        title: item.title.as_str().into(),
        meta: if owned { item.artist.as_str().into() } else { format!("Не скачан · {}", item.artist).into() },
        liked: crate::track_liked(&marks, item),
        song_id: if owned { item.id.as_str() } else { "" }.into(),
        identified: !item.id.is_empty() || !item.listen_ref.is_empty(),
        duration: row_duration(item).into(),
        ..QueueItem::default()
    }).collect::<Vec<_>>();
    ui.set_album_tracks(slint::ModelRc::new(slint::VecModel::from(rows)));
    if let Some(preview) = hub.album_preview.borrow_mut().as_mut() { preview.tracks = album.into_items(); }
}

pub(super) fn refresh_likes(ui: &AppWindow, hub: &HubState) {
    let preview = hub.album_preview.borrow();
    let Some(preview) = preview.as_ref() else { return };
    let marks = hub.marks.borrow();
    let rows = ui.get_album_tracks();
    for (i, item) in preview.tracks.iter().enumerate() {
        if let Some(mut row) = rows.row_data(i) {
            let liked = crate::track_liked(&marks, item);
            if row.liked != liked {
                row.liked = liked;
                rows.set_row_data(i, row);
            }
        }
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
    let hub_play = hub.clone();
    let queue_play = queue.clone();
    let player_play = player.clone();
    let config_play = config.clone();
    let status_play = status.clone();
    let waves_play = waves.clone();
    let play_preview = move |selection: Option<usize>| {
        let Some(ui) = weak.upgrade() else { return };
        if ui.get_album_busy() || !ui.get_album_error().is_empty() {
            return;
        }
        let preview = hub_play.album_preview.borrow();
        let Some(preview) = preview.as_ref() else { return };
        if selection.unwrap_or(0) >= preview.tracks.len() { return; }
        cancel_queue_load(&ui, &hub_play);
        let album = queue::Queue::from_tracks(ui.get_album_title().to_string(), preview.tracks.clone());
        play_album(&ui, &hub_play, &player_play, &queue_play, &config_play, &status_play,
            &waves_play, &preview.id, album, selection);
        ui.set_album_note("Очередь заменена альбомом".into());
    };
    let play_all = play_preview.clone();
    ui.on_album_play(move || play_all(None));
    ui.on_album_play_row(move |row| {
        if let Ok(index) = usize::try_from(row) { play_preview(Some(index)); }
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
    id: &str, mut album: queue::Queue, selection: Option<usize>,
) {
    let Some(start) = select_album_start(&mut album, selection) else { return; };
    if let Some(item) = queue.borrow().current() {
        note_wave_leave(&player.borrow(), item, hub, false);
    }
    clear_wave(hub);
    id.clone_into(&mut hub.album_id.borrow_mut());
    album.inherit_preferences(&queue.borrow());
    *queue.borrow_mut() = album;
    jump(ui, player, queue, config, status, waves, hub, true, start);
}

fn select_album_start(album: &mut queue::Queue, selection: Option<usize>) -> Option<queue::Start> {
    if !album.select(selection.unwrap_or(0)) { return None; }
    album.set_context("album");
    Some(if selection.is_some() { queue::Start::Manual } else { queue::Start::Auto })
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
        album.set_context("album");
        album.inherit_preferences(queue);
        *queue = album;
        id.clone_into(&mut hub.album_id.borrow_mut());
        if let Some(item) = queue.current() { crate::show_cover(ui, hub, &item.id); }
    } else {
        let first = queue.items().len();
        queue.extend(tracks);
        queue.set_source(first..queue.items().len(), "album");
        if hub.album_id.borrow().as_str() != id { hub.album_id.borrow_mut().clear(); }
    }
    show_queue(ui, queue, hub);
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn first_album_row_is_manual_but_play_album_is_auto() {
        let mut album = queue::Queue::from_path("one", "One", "");
        assert_eq!(select_album_start(&mut album, None), Some(queue::Start::Auto));
        assert_eq!(album.context(), "album");
        assert_eq!(select_album_start(&mut album, Some(0)), Some(queue::Start::Manual));
        assert_eq!(select_album_start(&mut album, Some(1)), None);
    }
}
