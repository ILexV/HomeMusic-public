//! Daily mix requests share the website's settings, rebuild and onboarding lifecycle.
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::time::{Duration, Instant};

use crate::{home, hub, AppWindow, HubEvent, HubState};

#[derive(Default)]
pub struct State {
    generation: Cell<u64>,
    rebuilding_from: Cell<Option<i64>>,
    pub mode_busy: Cell<bool>,
    pub taste_generation: Cell<u64>,
    pub taste_loading: Cell<bool>,
    pub taste_error: Cell<bool>,
    pub taste_saving: Cell<bool>,
    taste_seeded: Cell<bool>,
    taste_names: RefCell<HashMap<String, hub::TasteArtist>>,
}

impl State {
    fn merge_snapshot(&self, mix: &mut hub::MixSnapshot) -> bool {
        let Some(previous) = self.rebuilding_from.get() else { return false };
        if mix.id != previous && mix.phase == hub::MixPhase::Ready {
            self.rebuilding_from.set(None);
            return true;
        }
        // The POST can finish before the worker reports building=true.
        mix.building = true;
        mix.poll = true;
        if mix.phase != hub::MixPhase::Ready { mix.phase = hub::MixPhase::Building; }
        false
    }
}

pub enum Event {
    Fetched { ticket: u64, result: Result<hub::MixSnapshot, String> },
    ModeSaved(Result<hub::MixSettings, String>),
    Rebuilt(Result<bool, String>),
    Taste { ticket: u64, result: Result<hub::TastePage, String> },
    TasteSaved(Result<hub::MixSettings, String>),
}

fn spawn(hub: &HubState, job: impl FnOnce(&hub::Hub) -> Event + Send + 'static) {
    crate::spawn_fetch(hub, move |client| HubEvent::Mix(job(client)));
}

pub fn fetch_mix(hub: &HubState) {
    let ticket = hub.mix_control.generation.get().wrapping_add(1);
    hub.mix_control.generation.set(ticket);
    hub.mix_fetching.set(true);
    hub.mix_poll_at.set(Some(Instant::now()));
    spawn(hub, move |client| Event::Fetched { ticket, result: client.mix_today() });
}

pub fn poll_mix(hub: &HubState) {
    if hub.mix_fetching.get() || hub.mix_control.mode_busy.get() || hub.mix_control.taste_saving.get() {
        return;
    }
    let mix = hub.mix.borrow();
    if !mix.poll && hub.mix_control.rebuilding_from.get().is_none() {
        return;
    }
    let interval = if mix.building { 4 } else { 8 };
    drop(mix);
    if hub.mix_poll_at.get().is_none_or(|at| at.elapsed() >= Duration::from_secs(interval)) {
        fetch_mix(hub);
    }
}

pub fn set_mix_mode(hub: &HubState, mode: &str) {
    if hub.mix_control.mode_busy.get() || hub.mix.borrow().mode == mode {
        return;
    }
    hub.mix_control.mode_busy.set(true);
    // Invalidate a GET that began before the settings mutation.
    hub.mix_control.generation.set(hub.mix_control.generation.get().wrapping_add(1));
    hub.mix_fetching.set(false);
    *hub.hub_note.borrow_mut() = "Сохраняем режим…".into();
    let mode = mode.to_string();
    spawn(hub, move |client| Event::ModeSaved(client.set_mix_mode(&mode)));
}

pub fn rebuild_mix(hub: &HubState) {
    let mut mix = hub.mix.borrow_mut();
    if !mix.can_rebuild || mix.building {
        return;
    }
    hub.mix_control.rebuilding_from.set(Some(mix.id));
    mix.building = true;
    mix.poll = true;
    drop(mix);
    *hub.hub_note.borrow_mut() = "Запускаем сборку нового микса…".into();
    spawn(hub, |client| Event::Rebuilt(client.rebuild_mix()));
}

pub fn open_taste(ui: &AppWindow, hub: &HubState) {
    crate::close_album(ui, hub);
    hub.catalog_open.set(false);
    ui.set_catalog_open(false);
    ui.set_search(String::new().into());
    hub.pane.set(home::Pane::Taste);
    hub.mix_control.taste_seeded.set(false);
    hub.taste_picked.borrow_mut().clear();
    hub.mix_control.taste_names.borrow_mut().clear();
    let ticket = prepare_taste_search(hub);
    fetch_taste(hub, ticket, String::new());
}

pub fn prepare_taste_search(hub: &HubState) -> u64 {
    let ticket = hub.mix_control.taste_generation.get().wrapping_add(1);
    hub.mix_control.taste_generation.set(ticket);
    hub.mix_control.taste_loading.set(true);
    hub.mix_control.taste_error.set(false);
    *hub.hub_note.borrow_mut() = "Поиск исполнителей…".into();
    ticket
}

pub fn fetch_taste(hub: &HubState, ticket: u64, query: String) {
    spawn(hub, move |client| Event::Taste { ticket, result: client.taste(&query) });
}

pub fn toggle_taste(hub: &HubState, reference: &str) {
    if hub.mix_control.taste_saving.get() { return; }
    let mut picked = hub.taste_picked.borrow_mut();
    if picked.remove(reference) { return; }
    if picked.len() >= 5 {
        *hub.hub_note.borrow_mut() = "Не больше 5 исполнителей".into();
    } else {
        picked.insert(reference.to_string());
        hub.hub_note.borrow_mut().clear();
    }
}

pub fn save_taste(hub: &HubState) {
    if hub.mix_control.taste_saving.get() { return; }
    let refs: Vec<_> = hub.taste_picked.borrow().iter().cloned().collect();
    if !(3..=5).contains(&refs.len()) {
        *hub.hub_note.borrow_mut() = "Выберите 3–5 исполнителей".into();
        return;
    }
    hub.mix_control.taste_saving.set(true);
    hub.mix_control.generation.set(hub.mix_control.generation.get().wrapping_add(1));
    hub.mix_fetching.set(false);
    *hub.hub_note.borrow_mut() = "Сохраняем исполнителей…".into();
    spawn(hub, move |client| Event::TasteSaved(client.pick_taste(&refs)));
}

pub fn apply_event(ui: &AppWindow, hub: &HubState, event: Event) {
    match event {
        Event::Fetched { ticket, result } => {
            if ticket != hub.mix_control.generation.get() { return; }
            hub.mix_fetching.set(false);
            match result {
                Ok(mut mix) => {
                    if hub.mix_control.merge_snapshot(&mut mix) {
                        *hub.hub_note.borrow_mut() = "Новый микс готов".into();
                    }
                    *hub.mix.borrow_mut() = mix;
                }
                Err(message) => *hub.hub_note.borrow_mut() = message,
            }
        }
        Event::ModeSaved(result) => {
            hub.mix_control.mode_busy.set(false);
            match result {
                Ok(settings) => {
                    let mut mix = hub.mix.borrow_mut();
                    mix.mode = settings.mode;
                    mix.hour = settings.hour;
                    drop(mix);
                    *hub.hub_note.borrow_mut() = "Режим сменится в следующем миксе".into();
                    fetch_mix(hub);
                }
                Err(message) => *hub.hub_note.borrow_mut() = message,
            }
        }
        Event::Rebuilt(result) => match result {
            Ok(queued) => {
                *hub.hub_note.borrow_mut() = if queued { "Собираем новый микс — пара минут" } else { "Микс уже собирается" }.into();
                fetch_mix(hub);
            }
            Err(message) => {
                hub.mix_control.rebuilding_from.set(None);
                hub.mix.borrow_mut().building = false;
                *hub.hub_note.borrow_mut() = message;
                fetch_mix(hub);
            }
        },
        Event::Taste { ticket, result } => {
            if ticket != hub.mix_control.taste_generation.get() || hub.pane.get() != home::Pane::Taste { return; }
            hub.mix_control.taste_loading.set(false);
            match result {
                Ok(page) => {
                    if !hub.mix_control.taste_seeded.replace(true) {
                        *hub.taste_picked.borrow_mut() = page.selected.iter().map(|artist| artist.reference.clone()).collect();
                    }
                    let mut names = hub.mix_control.taste_names.borrow_mut();
                    for artist in page.selected.iter().chain(page.suggestions.iter()) {
                        names.insert(artist.reference.clone(), artist.clone());
                    }
                    let picked = hub.taste_picked.borrow();
                    let mut artists: Vec<_> = picked.iter().filter_map(|id| names.get(id).cloned()).collect();
                    artists.sort_by(|a, b| a.name.cmp(&b.name));
                    artists.extend(page.suggestions.into_iter().filter(|artist| !picked.contains(&artist.reference)));
                    *hub.taste.borrow_mut() = artists;
                    hub.hub_note.borrow_mut().clear();
                }
                Err(message) => {
                    hub.mix_control.taste_error.set(true);
                    *hub.hub_note.borrow_mut() = message;
                }
            }
        }
        Event::TasteSaved(result) => {
            hub.mix_control.taste_saving.set(false);
            match result {
                Ok(settings) => {
                    let mut mix = hub.mix.borrow_mut();
                    mix.mode = settings.mode;
                    mix.hour = settings.hour;
                    hub.mix_control.rebuilding_from.set(Some(mix.id));
                    mix.building = true;
                    mix.poll = true;
                    drop(mix);
                    *hub.hub_note.borrow_mut() = "Собираем «Микс дня»".into();
                    if hub.pane.get() == home::Pane::Taste {
                        hub.pane.set(home::Pane::Mix);
                        ui.set_search(String::new().into());
                    }
                    fetch_mix(hub);
                }
                Err(message) => *hub.hub_note.borrow_mut() = message,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_old_ready_reply_keeps_rebuild_polling_until_the_replacement_arrives() {
        let state = State::default();
        state.rebuilding_from.set(Some(41));
        let mut previous = hub::MixSnapshot {
            id: 41, phase: hub::MixPhase::Ready, total: 50, ready: 50,
            ..hub::MixSnapshot::default()
        };
        assert!(!state.merge_snapshot(&mut previous));
        assert!(previous.poll && previous.building);
        assert_eq!(previous.phase, hub::MixPhase::Ready);
        let mut replacement = hub::MixSnapshot {
            id: 42, phase: hub::MixPhase::Ready, total: 50, ready: 50,
            ..hub::MixSnapshot::default()
        };
        assert!(state.merge_snapshot(&mut replacement));
        assert!(!replacement.building && !replacement.poll);
        assert_eq!(state.rebuilding_from.get(), None);
    }
}
