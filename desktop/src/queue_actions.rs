//! Queue edits preserve playback unless the selected occurrence is removed.
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

use slint::ComponentHandle;

use crate::{
    cancel_queue_load, clear_track, clear_wave, empty_wave, navidrome, note_wave_leave,
    play_current, player::Player, push_playback, queue, show_queue, show_status, wave,
    AppWindow, HubState,
};

pub fn connect_queue_actions(
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
    let items = queue.clone();
    ui.on_move_queue_row(move |from, to| {
        let Some(ui) = weak.upgrade() else { return };
        if from < 0 || to < 0 {
            return;
        }
        let moved = items.borrow_mut().move_item(from as usize, to as usize);
        if moved {
            cancel_queue_load(&ui, &state);
            show_queue(&ui, &items.borrow(), &state);
        }
    });

    let weak = ui.as_weak();
    let state = hub.clone();
    let items = queue.clone();
    ui.on_queue_row_next(move |source| {
        let Some(ui) = weak.upgrade() else { return };
        let moved = source >= 0 && items.borrow_mut().play_next(source as usize);
        if moved {
            cancel_queue_load(&ui, &state);
            show_queue(&ui, &items.borrow(), &state);
        }
    });

    let weak = ui.as_weak();
    let state = hub.clone();
    let items = queue.clone();
    let playback = player.clone();
    let config = config.clone();
    let status = status.clone();
    let waves = waves.clone();
    ui.on_remove_queue_row(move |source| {
        let Some(ui) = weak.upgrade() else { return };
        if source < 0 {
            return;
        }
        let source = source as usize;
        let current_removed = items.borrow().index() == Some(source);
        let was_playing = playback.borrow().snapshot().playing;
        if current_removed {
            if let Some(item) = items.borrow().current() {
                note_wave_leave(&playback.borrow(), item, &state, false);
            }
        }
        if !items.borrow_mut().remove(source) {
            return;
        }
        cancel_queue_load(&ui, &state);
        if current_removed {
            playback.borrow_mut().stop();
            if items.borrow().is_empty() {
                clear_wave(&state);
                state.album_id.borrow_mut().clear();
                waves.load(None, "");
                ui.set_wave(empty_wave());
                clear_track(&ui);
                ui.set_error(String::new().into());
            } else {
                let _ = play_current(&ui, &playback, &items, &config, &waves, &state);
                if !was_playing {
                    playback.borrow_mut().set_paused(true);
                }
            }
        }
        show_queue(&ui, &items.borrow(), &state);
        push_playback(&ui, &playback.borrow().snapshot());
        show_status(&ui, &playback.borrow(), &state, &status);
    });
}
