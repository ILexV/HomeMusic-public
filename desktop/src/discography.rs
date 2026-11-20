//! Browse the selected recording's artists without replacing or starting the queue.
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::mpsc::{self, Receiver};

use serde_json::Value;
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

use crate::{feature_context::Context, hub::Hub, queue, Discography, ExploreWindow};

#[derive(Debug)]
struct Artist {
    name: String,
    library: String,
    reference: String,
    homonym: bool,
}

#[derive(Debug)]
struct Album {
    id: String,
    title: String,
    artist: String,
    label: SharedString,
}

enum Page {
    Credits(Vec<Artist>, String),
    Releases(Vec<Album>),
}

pub(crate) struct State {
    ctx: Context,
    window: slint::Weak<ExploreWindow>,
    seed: RefCell<(String, String)>, // credits request and unsplit display credit
    artists: RefCell<Vec<Artist>>,
    albums: RefCell<Vec<Album>>,
    // Replacing this receiver drops the previous generation. A late worker cannot
    // paint either its result or its error over another track, artist or tab.
    pending: RefCell<Option<Receiver<Result<Page, String>>>>,
}

fn field<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or_default()
}

fn array<'a>(value: &'a Value, key: &str) -> Result<&'a Vec<Value>, String> {
    value[key].as_array().ok_or_else(|| format!("Хаб: в ответе нет списка {key}"))
}

fn parse_credits(value: &Value, fallback: &str) -> Result<Page, String> {
    let mut artists = array(value, "artists")?.iter().map(|row| {
        let name = field(row, "name");
        if name.is_empty() { return Err("Хаб: не указано имя участника"); }
        Ok(Artist { name: name.into(), library: field(row, "nd_artist_id").into(),
                    reference: field(row, "ref").into(), homonym: false })
    }).collect::<Result<Vec<_>, _>>()?;
    if artists.len() > 1 {
        let mut positions = std::collections::HashMap::with_capacity(artists.len());
        for index in 0..artists.len() {
            if let Some(other) = positions.insert(artists[index].name.to_lowercase(), index) {
                artists[other].homonym = true;
                artists[index].homonym = true;
            }
        }
    }
    // Files without structured metadata get a single credit, not guesses made
    // from punctuation in band names (AC/DC, Earth, Wind & Fire, …).
    if artists.is_empty() && !fallback.trim().is_empty() {
        artists.push(Artist { name: fallback.trim().into(), library: String::new(), reference: String::new(), homonym: false });
    }
    Ok(Page::Credits(artists, field(value, "warning").into()))
}

fn parse_releases(value: &Value, catalog: bool) -> Result<Vec<Album>, String> {
    let groups: &[(&str, &str)] = if catalog {
        &[("albums", "Альбом"), ("singles", "Сингл / EP"), ("compilations", "Сборник")]
    } else { &[("albums", "")] };
    let mut albums = Vec::new();
    for &(key, group) in groups {
        for row in array(value, key)? {
            let id = field(row, if catalog { "ref" } else { "id" });
            let title = field(row, "title");
            if id.is_empty() || title.is_empty() { return Err("Хаб: у альбома нет идентификатора или названия".into()); }
            let artist = field(row, "artist");
            let mut label: SharedString = title.into();
            for part in [group, artist] {
                if !part.is_empty() { label.push_str(" · "); label.push_str(part); }
            }
            if let Some(year) = row["year"].as_u64().filter(|n| *n > 0) {
                use std::fmt::Write;
                let _ = write!(label, " · {year}");
            } else if let Some(date) = row["release_date"].as_str() {
                label.push_str(" · "); label.push_str(date);
            }
            albums.push(Album { id: id.into(), title: title.into(), artist: artist.into(), label });
        }
    }
    Ok(albums)
}

fn credits_path(id: &str, reference: &str) -> String {
    if id.is_empty() && reference.is_empty() { return String::new(); }
    let mut query = url::form_urlencoded::Serializer::new(String::new());
    if !id.is_empty() { query.append_pair("nd_song_id", id); }
    if !reference.is_empty() { query.append_pair("ref", reference); }
    format!("/api/discography/credits?{}", query.finish())
}

fn artist_path(id: &str, catalog: bool) -> String {
    let id: String = url::form_urlencoded::byte_serialize(id.as_bytes()).map(|s| if s == "+" { "%20" } else { s }).collect();
    if catalog { format!("/api/catalog/artist/{id}") } else { format!("/api/nd/artists/{id}") }
}

fn load_releases(hub: &Hub, mut id: String, name: &str, catalog: bool) -> Result<Page, String> {
    if id.is_empty() {
        let query = url::form_urlencoded::Serializer::new(String::new())
            .append_pair("scope", if catalog { "catalog" } else { "library" }).append_pair("name", name).finish();
        let found = hub.send("GET", &format!("/api/discography/artist?{query}"), None)?;
        if found.is_null() { return Ok(Page::Releases(Vec::new())); }
        id = field(&found, if catalog { "ref" } else { "nd_artist_id" }).into();
        if id.is_empty() { return Err("Хаб: не указан идентификатор исполнителя".into()); }
    }
    let body = hub.send("GET", &artist_path(&id, catalog), None)?;
    parse_releases(&body, catalog).map(Page::Releases)
}

impl State {
    pub(crate) fn open(&self, item: queue::Item) {
        let Some(window) = self.window.upgrade() else { return };
        let ui = window.global::<Discography>();
        ui.set_track(if item.artist.is_empty() { item.title.into() } else { format!("{} — {}", item.artist, item.title).into() });
        ui.set_catalog(item.id.is_empty() && !item.listen_ref.starts_with("nd:"));
        self.seed.replace((credits_path(&item.id, &item.listen_ref), item.artist));
        self.load_artists(&window);
    }

    fn load_artists(&self, window: &ExploreWindow) {
        let ui = window.global::<Discography>();
        self.artists.borrow_mut().clear();
        ui.set_artists(ModelRc::default());
        ui.set_selected(0);
        ui.set_warning(SharedString::default());
        self.clear_releases(window);
        ui.set_status("Ищем участников трека…".into());
        let (path, fallback) = self.seed.borrow().clone();
        if path.is_empty() {
            self.pending.borrow_mut().take();
            let artists = if fallback.trim().is_empty() { vec![] } else {
                vec![Artist { name: fallback, library: String::new(), reference: String::new(), homonym: false }]
            };
            self.apply(window, Ok(Page::Credits(artists, String::new())));
        } else {
            self.request(move |hub| parse_credits(&hub.send("GET", &path, None)?, &fallback));
        }
    }

    fn clear_releases(&self, window: &ExploreWindow) {
        self.albums.borrow_mut().clear();
        let ui = window.global::<Discography>();
        ui.set_releases(ModelRc::default());
        ui.set_failed(false);
    }

    fn browse(&self, index: i32, catalog: bool) {
        let Some(window) = self.window.upgrade() else { return };
        let artists = self.artists.borrow();
        let Some(artist) = artists.get(index as usize) else { return };
        let ui = window.global::<Discography>();
        ui.set_selected(index);
        ui.set_catalog(catalog);
        self.clear_releases(&window);
        let id = if catalog { &artist.reference } else { &artist.library };
        if id.is_empty() && artist.homonym {
            self.pending.borrow_mut().take();
            ui.set_status("У одноимённых участников нет однозначной связи между каталогами".into());
            return;
        }
        let id = id.to_owned();
        let name = if id.is_empty() { artist.name.clone() } else { String::new() };
        ui.set_status("Загружаем альбомы…".into());
        self.request(move |hub| load_releases(hub, id, &name, catalog));
    }

    fn request(&self, job: impl FnOnce(&Hub) -> Result<Page, String> + Send + 'static) {
        let (tx, rx) = mpsc::channel();
        self.pending.replace(Some(rx));
        let hub = self.ctx.hub.hub.clone();
        let window = self.window.clone();
        std::thread::spawn(move || {
            if tx.send(job(&hub)).is_ok() {
                let _ = window.upgrade_in_event_loop(|w| w.global::<Discography>().invoke_received());
            }
        });
    }

    fn apply(&self, window: &ExploreWindow, page: Result<Page, String>) {
        let ui = window.global::<Discography>();
        match page {
            Err(message) => { ui.set_failed(true); ui.set_status(message.into()); }
            Ok(Page::Credits(artists, warning)) => {
                ui.set_warning(warning.into());
                if artists.is_empty() { ui.set_status("У трека не указаны исполнители".into()); return; }
                let names: Vec<SharedString> = artists.iter().map(|a| {
                    if a.homonym {
                        format!("{} ({})", a.name, if a.reference.is_empty() { &a.library } else { &a.reference }).into()
                    } else { a.name.as_str().into() }
                }).collect();
                self.artists.replace(artists);
                ui.set_artists(ModelRc::new(VecModel::from(names)));
                self.browse(0, ui.get_catalog());
            }
            Ok(Page::Releases(albums)) => {
                let labels: Vec<SharedString> = albums.iter().map(|a| a.label.clone()).collect();
                ui.set_status(if albums.is_empty() {
                    if ui.get_catalog() { "В каталоге не найдены релизы этого исполнителя" } else { "В библиотеке нет альбомов этого исполнителя" }
                } else { "" }.into());
                self.albums.replace(albums);
                ui.set_releases(ModelRc::new(VecModel::from(labels)));
            }
        }
    }
}

pub(crate) fn bind(ctx: &Context, window: &ExploreWindow) -> Rc<State> {
    let state = Rc::new(State { ctx: ctx.clone(), window: window.as_weak(), seed: RefCell::default(), artists: RefCell::default(),
                                albums: RefCell::default(), pending: RefCell::default() });
    let ui = window.global::<Discography>();
    let s = state.clone();
    ui.on_received(move || {
        let result = s.pending.borrow().as_ref().and_then(|rx| rx.try_recv().ok());
        if let Some(page) = result {
            s.pending.borrow_mut().take();
            if let Some(window) = s.window.upgrade() {
                if !window.get_similar() && window.window().is_visible() { s.apply(&window, page); }
            }
        }
    });
    let s = state.clone();
    ui.on_select_artist(move |index| {
        if let Some(w) = s.window.upgrade() { s.browse(index, w.global::<Discography>().get_catalog()); }
    });
    let s = state.clone();
    ui.on_select_catalog(move |catalog| {
        if let Some(w) = s.window.upgrade() {
            let ui = w.global::<Discography>();
            if catalog != ui.get_catalog() { s.browse(ui.get_selected(), catalog); }
        }
    });
    let s = state.clone();
    ui.on_open_album(move |index| {
        if let (Some(w), Some(a)) = (s.window.upgrade(), s.albums.borrow().get(index as usize)) {
            crate::show_explore_album(&s.ctx, &a.id, w.global::<Discography>().get_catalog(), &a.title, &a.artist);
            let _ = w.hide();
        }
    });
    let s = state.clone();
    ui.on_retry(move || {
        if let Some(w) = s.window.upgrade() {
            if s.artists.borrow().is_empty() { s.load_artists(&w); } else {
                let ui = w.global::<Discography>(); s.browse(ui.get_selected(), ui.get_catalog());
            }
        }
    });
    state
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn discography_keeps_track_and_artist_ids_separate() {
        let page = parse_credits(&json!({"artists":[{"name":"AC/DC", "nd_artist_id":"library-1", "ref":"dz:99"},
                                                   {"name":"Guest", "ref":"dz:3"}]}), "AC/DC & Guest").unwrap();
        let Page::Credits(rows, _) = page else { panic!() };
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].name, "AC/DC");
        assert_eq!(rows[0].library, "library-1");
        assert_eq!(artist_path(&rows[1].reference, true), "/api/catalog/artist/dz%3A3");
        assert_eq!(credits_path("nd&one", "dz:2"), "/api/discography/credits?nd_song_id=nd%26one&ref=dz%3A2");
        let Page::Credits(rows, _) = parse_credits(&json!({"artists":[]}), "Earth, Wind & Fire").unwrap() else { panic!() };
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].name, "Earth, Wind & Fire");
    }

    #[test]
    fn discography_opens_same_title_editions_and_singles_by_release_identity() {
        let rows = parse_releases(&json!({"albums":[{"title":"Same", "ref":"dz:4"}],
            "singles":[{"title":"Same", "ref":"dz:8"}], "compilations":[{"title":"Any", "ref":"dz:2"}]}), true).unwrap();
        assert_eq!(rows.iter().map(|a| a.id.as_str()).collect::<Vec<_>>(), ["dz:4", "dz:8", "dz:2"]);
        assert!(parse_releases(&json!({"albums":[]}), false).unwrap().is_empty());
        assert!(parse_releases(&json!({}), false).is_err());
        assert!(parse_releases(&json!({"albums":[{"title":"No ID"}]}), false).is_err());
    }
}
