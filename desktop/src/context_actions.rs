//! Context menus operate on the original model row, never a filtered offset or a title.
use std::cell::{Cell, RefCell};
use std::sync::{mpsc, Arc};

use slint::ComponentHandle;

use crate::{album_view, feature_context::Context, home, navidrome, player::Player, queue,
    AppWindow, HubEvent, HubState, ItemActions, TrackList, TrackPlacement};

pub(super) struct AlbumRequest {
    id: String,
    play: bool,
    generation: u64,
    instance: u64,
}

pub(super) fn bind(ui: &AppWindow, ctx: &Context) {
    let state = ctx.clone();
    ui.global::<ItemActions>().on_open_song_album(move |id| {
        if let Some(ui) = state.ui.upgrade() {
            album_view::open_song_album(&ui, &state.hub, &state.config, &id);
        }
    });

    let state = ctx.clone();
    ui.global::<ItemActions>().on_place_track(move |list, source, placement| {
        let Ok(index) = usize::try_from(source) else { return };
        let Some(ui) = state.ui.upgrade() else { return };
        let item = match list {
            TrackList::Queue => {
                // Use the same operations as the drag handle and inline next button.
                match placement {
                    TrackPlacement::Next => ui.invoke_queue_row_next(source),
                    TrackPlacement::End => {
                        let len = state.queue.borrow().items().len();
                        if len > 0 { ui.invoke_move_queue_row(source, (len - 1) as i32); }
                    }
                }
                return;
            }
            TrackList::Album => state.hub.album_preview.borrow().as_ref()
                .and_then(|a| a.tracks.get(index)).cloned(),
            TrackList::Home => home_track(&state.hub, index),
            TrackList::Catalog => state.hub.hits.borrow().get(index).cloned().and_then(crate::catalog_track),
        };
        let Some(item) = item else { return };
        crate::cancel_queue_load(&ui, &state.hub);
        let mut queue = state.queue.borrow_mut();
        let was_empty = place_new_track(&mut queue, item, placement);
        if was_empty {
            if let Some(item) = queue.current() { crate::show_cover(&ui, &state.hub, &item.id); }
        }
        let preview = state.hub.album_preview.borrow();
        let album_id = if list == TrackList::Album { preview.as_ref().map(|a| a.id.as_str()) } else { None };
        let mut current_album = state.hub.album_id.borrow_mut();
        if was_empty { album_id.unwrap_or_default().clone_into(&mut current_album); }
        else if Some(current_album.as_str()) != album_id { current_album.clear(); }
        drop(current_album);
        state.player.borrow_mut().cancel_preload();
        crate::show_queue(&ui, &queue, &state.hub);
        if list == TrackList::Album { ui.set_album_note("Трек добавлен в очередь".into()); }
    });

    // One FIFO worker preserves click order for multiple album appends, including
    // an append requested while the preceding album is still loading for play.
    let config = (*ctx.config).clone();
    let events = Arc::clone(&ctx.hub.events);
    let (send, jobs) = mpsc::channel::<AlbumRequest>();
    std::thread::spawn(move || {
        for request in jobs {
            let result = navidrome::album_tracks(&config, &request.id);
            crate::push_event(&events, HubEvent::CardAlbum { request, result });
        }
    });
    let state = ctx.clone();
    let pending_play = Cell::new(None);
    ui.global::<ItemActions>().on_load_album(move |id, play| {
        let Some(ui) = state.ui.upgrade() else { return };
        if id.is_empty() { return; }
        let own_load = pending_play.get() == Some(state.hub.queue_generation.get());
        if play || state.hub.wave_busy.get() || (ui.get_queue_busy() && !own_load) {
            crate::cancel_queue_load(&ui, &state.hub);
        }
        let generation = state.hub.queue_generation.get();
        if play {
            pending_play.set(Some(generation));
            ui.set_queue_busy(true);
        }
        ui.set_queue_error(Default::default());
        let request = AlbumRequest { id: id.to_string(), play, generation, instance: state.queue.borrow().instance() };
        if send.send(request).is_err() {
            ui.set_queue_busy(false);
            ui.set_queue_error("Не удалось загрузить альбом".into());
        }
    });
}

fn home_track(hub: &HubState, source: usize) -> Option<queue::Item> {
    let rows = hub.rows.borrow();
    let row = rows.get(source).filter(|row| row.kind == "track")?;
    let index = row.key.parse::<usize>().ok()?;
    match hub.pane.get() {
        home::Pane::Mix => hub.mix.borrow().tracks.get(index).filter(|s| s.can_play()).map(crate::playable_item),
        home::Pane::Liked => hub.liked_songs.borrow().get(index).filter(|s| s.can_play()).map(crate::playable_item),
        _ => None,
    }
}

// Selecting the first addition makes an empty queue ready for Play; it does
// not start the player. A nonempty queue keeps its current occurrence intact.
fn place_new_track(queue: &mut queue::Queue, item: queue::Item, placement: TrackPlacement) -> bool {
    let was_empty = queue.is_empty();
    match placement {
        TrackPlacement::Next => queue.insert_next(item),
        TrackPlacement::End => queue.extend(std::iter::once(item)),
    }
    if was_empty { queue.select(0); }
    was_empty
}

pub(super) fn apply_card_album(
    ui: &AppWindow, hub: &HubState, player: &RefCell<Player>, queue: &RefCell<queue::Queue>,
    config: &navidrome::ServerConfig, status: &str, waves: &Arc<crate::wave::WaveHub>,
    request: AlbumRequest, result: Result<navidrome::LibraryQueue, String>,
) {
    // Moving a row during an append is harmless; replacing the queue is not.
    if request.generation != hub.queue_generation.get() &&
        (request.play || request.instance != queue.borrow().instance()) { return; }
    if request.play { ui.set_queue_busy(false); }
    match result {
        Ok(library) if request.play => album_view::play_album(ui, hub, player, queue, config, status,
            waves, &request.id, crate::library_to_queue(library), 0),
        Ok(library) => {
            let count = library.tracks.len();
            album_view::append_album(ui, hub, player, &mut queue.borrow_mut(), &request.id,
                &library.name, library.tracks.into_iter().map(crate::library_item));
            crate::remember_note(ui, &player.borrow(), hub, status, &format!("Добавлено в очередь: {count}"));
        }
        Err(message) => ui.set_queue_error(message.into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn additions_keep_the_playing_occurrence_and_use_the_selected_source() {
        let track = |path| queue::Item::file(path, "Duplicate title");
        let mut queue = queue::Queue::from_tracks("", vec![track("first"), track("playing"), track("old tail")]);
        queue.select(1);
        let token = queue.current_token();
        assert!(!place_new_track(&mut queue, track("next"), TrackPlacement::Next));
        assert!(!place_new_track(&mut queue, track("tail"), TrackPlacement::End));
        assert_eq!(queue.current_token(), token);
        assert_eq!(queue.items().iter().map(|item| item.path.as_str()).collect::<Vec<_>>(),
            ["first", "playing", "next", "old tail", "tail"]);
    }

    #[test]
    fn either_placement_makes_an_empty_queue_ready_for_play() {
        for placement in [TrackPlacement::Next, TrackPlacement::End] {
            let mut queue = queue::Queue::from_tracks("", Vec::new());
            assert!(place_new_track(&mut queue, queue::Item::file("one", "One"), placement));
            assert_eq!(queue.current().unwrap().path, "one");
        }
    }
}
