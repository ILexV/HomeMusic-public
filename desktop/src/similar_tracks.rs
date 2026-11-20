//! Browsing only fetches metadata. Playback, insertion and wave launch require explicit actions.
use std::rc::Rc;

use slint::{ComponentHandle, ModelRc, VecModel};

use crate::{context_actions, feature_context::Context, hub, queue, ExploreWindow, SimilarBrowser, SimilarRow};

const MISSING_ID: &str = "У трека нет идентификатора в каталоге или библиотеке";

pub(crate) struct State {
    ctx: Context,
    window: slint::Weak<ExploreWindow>,
}

pub(crate) fn bind(ctx: &Context, window: &ExploreWindow) -> Rc<State> {
    let state = Rc::new(State { ctx: ctx.clone(), window: window.as_weak() });
    let weak = Rc::downgrade(&state);
    window.global::<SimilarBrowser>().on_choose(move |row, play_now| {
        let Some(state) = weak.upgrade() else { return };
        let Some(window) = state.window.upgrade() else { return };
        let item = queue::Item { id: row.song_id.to_string(), listen_ref: row.track_ref.to_string(),
            title: row.title.to_string(), artist: row.artist.to_string(), album: row.album.to_string(),
            duration_s: row.seconds.max(0) as u32, path: String::new(), meta: String::new() };
        context_actions::place_next(&state.ctx, item, play_now);
        window.global::<SimilarBrowser>().set_action_note(if play_now { "" } else { "Трек добавлен следующим" }.into());
    });
    let weak = Rc::downgrade(&state);
    window.global::<SimilarBrowser>().on_start_wave(move || {
        let Some(state) = weak.upgrade() else { return };
        let Some(window) = state.window.upgrade() else { return };
        let reference = window.global::<SimilarBrowser>().get_seed_ref();
        if !reference.is_empty() && state.launch(reference.to_string()) { let _ = window.hide(); }
    });
    state
}

impl State {
    pub(crate) fn open(&self, item: queue::Item) {
        let Some(window) = self.window.upgrade() else { return };
        let panel = window.global::<SimilarBrowser>();
        let request = panel.get_request_id().wrapping_add(1);
        panel.set_request_id(request);
        panel.set_rows(ModelRc::default());
        panel.set_action_note(Default::default());
        let title = if item.artist.is_empty() { item.title.as_str().into() }
            else { format!("{} — {}", item.artist, item.title).into() };
        panel.set_seed_title(title);
        let Some(reference) = track_ref(item) else {
            panel.set_seed_ref(Default::default()); panel.set_status(MISSING_ID.into()); return;
        };
        panel.set_seed_ref(reference.as_str().into());
        panel.set_status("Ищем похожие песни…".into());
        let path = similar_path(&reference);
        let client = self.ctx.hub.hub.clone();
        let weak = self.window.clone();
        std::thread::spawn(move || {
            let result = client.send("GET", &path, None).and_then(|value| {
                let rows = value.as_array().ok_or_else(|| "Хаб: неверный список похожих песен".to_string())?;
                Ok(rows.iter().filter_map(hub::playable).collect::<Vec<_>>())
            });
            let _ = weak.upgrade_in_event_loop(move |window| {
                let panel = window.global::<SimilarBrowser>();
                if panel.get_request_id() != request { return; }
                match result {
                    Ok(tracks) => {
                        panel.set_status(if tracks.is_empty() { "Похожих песен не найдено" } else { "" }.into());
                        let rows: Vec<_> = tracks.into_iter().map(|song| SimilarRow {
                            title: song.title.into(), artist: song.artist.into(), album: song.album.into(),
                            song_id: song.nd_song_id.into(), track_ref: song.reference.into(),
                            seconds: i32::try_from(song.duration_s).unwrap_or(i32::MAX),
                        }).collect();
                        panel.set_rows(ModelRc::new(VecModel::from(rows)));
                    }
                    Err(message) => panel.set_status(message.into()),
                }
            });
        });
    }

    pub(crate) fn start_wave(&self, item: queue::Item) {
        if let Some(reference) = track_ref(item) { self.launch(reference); }
        else if let Some(ui) = self.ctx.ui.upgrade() { ui.set_queue_error(MISSING_ID.into()); }
    }

    fn launch(&self, reference: String) -> bool {
        let Some(ui) = self.ctx.ui.upgrade() else { return false };
        if self.ctx.hub.wave_busy.get() {
            if let Some(window) = self.window.upgrade() {
                window.global::<SimilarBrowser>().set_action_note("Подготовка волны уже идёт".into());
            }
            return false;
        }
        crate::remember_note(&ui, &self.ctx.player.borrow(), &self.ctx.hub, "", "Готовим волну по треку…");
        crate::begin_wave(&ui, &self.ctx.hub, Some(hub::WaveSeed::Track(reference)), "track_menu");
        true
    }
}

fn track_ref(item: queue::Item) -> Option<String> {
    if !item.listen_ref.is_empty() { Some(item.listen_ref) }
    else if !item.id.is_empty() { Some(format!("nd:{}", item.id)) }
    else { None }
}

fn similar_path(reference: &str) -> String {
    let query = url::form_urlencoded::Serializer::new(String::new()).append_pair("ref", reference).finish();
    format!("/api/wave/similar?{query}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn similar_requests_use_the_selected_recording_not_a_title_or_a_personal_wave() {
        let mut item = queue::Item::file("/music/Song.mp3", "Song");
        assert_eq!(track_ref(item.clone()), None);
        item.listen_ref = "file:hitmoz:a+b&c".into();
        assert_eq!(similar_path(&track_ref(item.clone()).unwrap()), "/api/wave/similar?ref=file%3Ahitmoz%3Aa%2Bb%26c");
        item.id = "library-song".into();
        assert_eq!(track_ref(item.clone()).as_deref(), Some("file:hitmoz:a+b&c"));
        item.listen_ref.clear();
        assert_eq!(track_ref(item).as_deref(), Some("nd:library-song"));
    }
}
