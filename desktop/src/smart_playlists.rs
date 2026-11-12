//! Saved rule playlists are live library queries, not recommendation sessions.
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::time::Duration;

use serde_json::{json, Value};
use slint::{ComponentHandle, Model, ModelRc, Timer, TimerMode, VecModel};

use crate::feature_context::Context;
use crate::{hub::Playable, SmartPlaylistRow, SmartRuleRow, ToolsWindow};

const ROOT: &str = "/api/smart-playlists";
const SORTS: [&str; 6] = ["title", "artist", "year", "added", "last_listened", "random"];

pub(crate) struct State {
    ctx: Context,
    tools: slint::Weak<ToolsWindow>,
    playlists: RefCell<Vec<Value>>,
    rules: Rc<VecModel<SmartRuleRow>>,
    busy: Cell<bool>,
    tx: SyncSender<Result<Event, String>>,
    rx: Receiver<Result<Event, String>>,
    timer: Timer,
}

enum Event {
    Listed(Vec<Value>),
    Saved { row: Value, original: Value, selected: String },
    Deleted(String),
    Evaluated { result: Value, definition: Value, play: bool, name: String },
}

fn text(value: &Value, key: &str) -> String {
    value.get(key).and_then(Value::as_str).unwrap_or_default().to_string()
}

fn integer(value: &str, label: &str, minimum: u32, maximum: u32) -> Result<u32, String> {
    value.trim().parse::<u32>().ok().filter(|n| (minimum..=maximum).contains(n))
        .ok_or_else(|| format!("{label}: введите число от {minimum} до {maximum}"))
}

fn rule_json(row: &SmartRuleRow) -> Result<Value, String> {
    Ok(match row.kind {
        0 => json!({"kind": "liked", "value": !row.flag}),
        1 => json!({"kind": "not_listened", "days": integer(&row.value, "Число дней", 1, 36500)?,
                    "include_unheard": row.flag}),
        2 => json!({"kind": "unheard"}),
        3 => {
            let value = row.value.trim();
            if value.is_empty() || value.chars().count() > 128 {
                return Err("Укажите жанр (до 128 символов)".into());
            }
            json!({"kind": "genre", "value": value})
        }
        4 => {
            let minimum = integer(&row.value, "Год от", 1, 9999)?;
            let maximum = integer(&row.maximum, "Год до", 1, 9999)?;
            if minimum > maximum { return Err("Начальный год больше конечного".into()); }
            json!({"kind": "year", "minimum": minimum, "maximum": maximum})
        }
        5 => json!({"kind": "recently_added", "days": integer(&row.value, "Число дней", 1, 36500)?}),
        _ => return Err("Неизвестное правило".into()),
    })
}

fn default_rule(kind: i32) -> SmartRuleRow {
    SmartRuleRow { kind, value: match kind { 1 | 5 => "30", 4 => "1990", _ => "" }.into(),
                   maximum: "2026".into(), flag: false }
}

fn decode_rule(rule: &Value) -> SmartRuleRow {
    let kind = match text(rule, "kind").as_str() {
        "not_listened" => 1, "unheard" => 2, "genre" => 3, "year" => 4, "recently_added" => 5, _ => 0,
    };
    let mut row = default_rule(kind);
    if kind == 0 { row.flag = !rule["value"].as_bool().unwrap_or(true); }
    if kind == 1 { row.flag = rule["include_unheard"].as_bool().unwrap_or(false); }
    row.value = match kind {
        1 | 5 => rule["days"].as_u64().unwrap_or(30).to_string().into(),
        3 => text(rule, "value").into(),
        4 => rule["minimum"].as_u64().unwrap_or(1990).to_string().into(),
        _ => "".into(),
    };
    if kind == 4 { row.maximum = rule["maximum"].as_u64().unwrap_or(2026).to_string().into(); }
    row
}

impl State {
    fn definition(&self, ui: &ToolsWindow) -> Result<Value, String> {
        let rules = self.rules.iter().map(|row| rule_json(&row)).collect::<Result<Vec<_>, _>>()?;
        if rules.is_empty() { return Err("Добавьте хотя бы одно правило".into()); }
        let sort = SORTS.get(ui.get_smart_sort() as usize).ok_or("Выберите порядок")?;
        Ok(json!({"match": if ui.get_smart_match_any() { "any" } else { "all" }, "rules": rules,
                  "sort": sort, "descending": ui.get_smart_descending(),
                  "limit": integer(&ui.get_smart_limit(), "Лимит", 1, 2000)?}))
    }

    fn body(&self, ui: &ToolsWindow) -> Result<Value, String> {
        let name = ui.get_smart_name();
        let name = name.trim();
        if name.is_empty() || name.chars().count() > 160 {
            return Err("Укажите название (до 160 символов)".into());
        }
        Ok(json!({"name": name, "definition": self.definition(ui)?}))
    }

    fn request(&self, job: impl FnOnce(&crate::hub::Hub) -> Result<Event, String> + Send + 'static) {
        if self.busy.replace(true) { return; }
        if let Some(ui) = self.tools.upgrade() { ui.set_smart_busy(true); }
        let hub = self.ctx.hub.hub.clone();
        let tx = self.tx.clone();
        std::thread::spawn(move || { let _ = tx.send(job(&hub)); });
    }

    fn refresh(&self) {
        self.request(|hub| {
            let value = hub.send("GET", ROOT, None)?;
            value.as_array().cloned().map(Event::Listed).ok_or("Некорректный список плейлистов".into())
        });
    }

    fn show_rows(&self, ui: &ToolsWindow) {
        let mut playlists = self.playlists.borrow_mut();
        playlists.sort_by_key(|row| text(row, "name").to_lowercase());
        let rows: Vec<_> = playlists.iter().map(|row| {
            let def = &row["definition"];
            SmartPlaylistRow { id: text(row, "id").into(), name: text(row, "name").into(),
                detail: format!("Правил: {} · до {} треков", def["rules"].as_array().map_or(0, Vec::len),
                                def["limit"].as_u64().unwrap_or(100)).into() }
        }).collect();
        ui.set_smart_rows(ModelRc::new(VecModel::from(rows)));
    }

    fn load(&self, ui: &ToolsWindow, row: Option<&Value>) {
        let empty = json!({"name": "Новый плейлист", "definition": {
            "rules": [{"kind": "liked", "value": true}], "limit": 100, "sort": "title", "match": "all"}});
        let row = row.unwrap_or(&empty);
        let def = &row["definition"];
        ui.set_smart_selected(text(row, "id").into());
        ui.set_smart_name(text(row, "name").into());
        ui.set_smart_match_any(text(def, "match") == "any");
        ui.set_smart_sort(SORTS.iter().position(|sort| *sort == text(def, "sort")).unwrap_or(0) as i32);
        ui.set_smart_descending(def["descending"].as_bool().unwrap_or(false));
        ui.set_smart_limit(def["limit"].as_u64().unwrap_or(100).to_string().into());
        self.rules.set_vec(def["rules"].as_array().map(|rules| rules.iter().map(decode_rule).collect::<Vec<_>>()).unwrap_or_default());
        ui.set_smart_status("«Проверить» покажет число совпадений; «Играть» заново применит правила.".into());
    }

    fn save(&self) {
        let Some(ui) = self.tools.upgrade() else { return; };
        let body = match self.body(&ui) { Ok(body) => body, Err(error) => { ui.set_smart_status(error.into()); return; } };
        let selected = ui.get_smart_selected().to_string();
        let path = if selected.is_empty() { ROOT.to_string() } else { format!("{ROOT}/{selected}") };
        self.request(move |hub| {
            let row = hub.send(if selected.is_empty() { "POST" } else { "PUT" }, &path, Some(body.clone()))?;
            Ok(Event::Saved { row, original: body, selected })
        });
    }

    fn evaluate(&self, play: bool) {
        let Some(ui) = self.tools.upgrade() else { return; };
        let definition = match self.definition(&ui) { Ok(def) => def, Err(error) => { ui.set_smart_status(error.into()); return; } };
        let name = ui.get_smart_name().to_string();
        self.request(move |hub| {
            let result = hub.send("POST", &format!("{ROOT}/preview"), Some(definition.clone()))?;
            Ok(Event::Evaluated { result, definition, play, name })
        });
    }

    fn edit_rule(&self, index: i32, change: impl FnOnce(&mut SmartRuleRow)) {
        if index < 0 { return; }
        if let Some(mut row) = self.rules.row_data(index as usize) {
            change(&mut row);
            self.rules.set_row_data(index as usize, row);
        }
    }

    fn apply(&self, event: Result<Event, String>) {
        self.busy.set(false);
        let Some(ui) = self.tools.upgrade() else { return; };
        ui.set_smart_busy(false);
        match event {
            Err(error) => ui.set_smart_status(error.into()),
            Ok(Event::Listed(rows)) => {
                let selected = ui.get_smart_selected();
                let selected_row = rows.iter().find(|row| text(row, "id") == selected.as_str()).cloned();
                *self.playlists.borrow_mut() = rows;
                self.show_rows(&ui);
                if !selected.is_empty() { self.load(&ui, selected_row.as_ref()); }
                else { ui.set_smart_status("Список загружен".into()); }
            }
            Ok(Event::Saved { row, original, selected }) => {
                let id = text(&row, "id");
                self.playlists.borrow_mut().retain(|old| text(old, "id") != id);
                self.playlists.borrow_mut().push(row.clone());
                self.show_rows(&ui);
                if ui.get_smart_selected().as_str() == selected {
                    if self.body(&ui).ok().as_ref() == Some(&original) {
                        self.load(&ui, Some(&row));
                    } else {
                        // Edits made while saving remain a draft of the newly saved playlist.
                        ui.set_smart_selected(id.into());
                    }
                }
                ui.set_smart_status("Плейлист сохранён на сервере".into());
            }
            Ok(Event::Deleted(id)) => {
                self.playlists.borrow_mut().retain(|row| text(row, "id") != id);
                self.show_rows(&ui);
                if ui.get_smart_selected().as_str() == id { self.load(&ui, None); }
                ui.set_smart_status("Плейлист удалён".into());
            }
            Ok(Event::Evaluated { result, definition, play, name }) => {
                if self.definition(&ui).ok().as_ref() != Some(&definition) {
                    ui.set_smart_status("Правила изменились во время запроса — проверьте снова".into());
                    return;
                }
                let tracks: Vec<Playable> = result["tracks"].as_array().into_iter().flatten().filter_map(|row| {
                    let id = text(row, "nd_song_id");
                    (!id.is_empty()).then(|| Playable { nd_song_id: id, reference: String::new(),
                        title: text(row, "title"), artist: text(row, "artist"), album: text(row, "album"),
                        duration_s: row["duration_s"].as_f64().unwrap_or(0.0).max(0.0) as u32 })
                }).collect();
                ui.set_smart_status(format!("Совпадений: {} · в очереди: {}{}", result["count"].as_u64().unwrap_or(0),
                    tracks.len(), if tracks.is_empty() { ". Измените правила." } else { "" }).into());
                if play && !tracks.is_empty() {
                    let mut queue = crate::queue_from_playables(&name, &tracks);
                    queue.select(0);
                    self.ctx.launch_queue(queue);
                }
            }
        }
    }
}

pub(crate) fn bind(ctx: &Context, tools: &ToolsWindow) -> Rc<State> {
    let (tx, rx) = mpsc::sync_channel(4);
    let state = Rc::new(State { ctx: ctx.clone(), tools: tools.as_weak(), playlists: RefCell::new(Vec::new()),
        rules: Rc::new(VecModel::default()), busy: Cell::new(false), tx, rx, timer: Timer::default() });
    tools.set_smart_rules(state.rules.clone().into());
    state.load(tools, None);
    macro_rules! callback {
        ($name:ident, |$state:ident $(, $arg:ident)*| $body:block) => {{
            let weak = Rc::downgrade(&state);
            tools.$name(move |$($arg),*| { if let Some($state) = weak.upgrade() { $body } });
        }};
    }
    callback!(on_smart_refresh, |state| { state.refresh(); });
    callback!(on_smart_new, |state| { if !state.busy.get() { if let Some(ui) = state.tools.upgrade() { state.load(&ui, None); } } });
    callback!(on_smart_select, |state, id| {
        if !state.busy.get() {
            if let Some(ui) = state.tools.upgrade() {
                let row = state.playlists.borrow().iter().find(|row| text(row, "id") == id.as_str()).cloned();
                if let Some(row) = row { state.load(&ui, Some(&row)); }
            }
        }
    });
    callback!(on_smart_save, |state| { state.save(); });
    callback!(on_smart_delete, |state| {
        if let Some(ui) = state.tools.upgrade() {
            let id = ui.get_smart_selected().to_string();
            if !id.is_empty() { state.request(move |hub| { hub.send("DELETE", &format!("{ROOT}/{id}"), None)?; Ok(Event::Deleted(id)) }); }
        }
    });
    callback!(on_smart_preview, |state| { state.evaluate(false); });
    callback!(on_smart_play, |state| { state.evaluate(true); });
    callback!(on_smart_add_rule, |state| { if state.rules.row_count() < 32 { state.rules.push(default_rule(0)); } });
    callback!(on_smart_remove_rule, |state, index| {
        if index >= 0 && (index as usize) < state.rules.row_count() { state.rules.remove(index as usize); }
    });
    callback!(on_smart_rule_kind, |state, index, kind| { if (0..=5).contains(&kind) { state.edit_rule(index, |row| *row = default_rule(kind)); } });
    callback!(on_smart_rule_value, |state, index, value| { state.edit_rule(index, |row| row.value = value); });
    callback!(on_smart_rule_maximum, |state, index, value| { state.edit_rule(index, |row| row.maximum = value); });
    callback!(on_smart_rule_flag, |state, index| { state.edit_rule(index, |row| row.flag = !row.flag); });
    let weak = Rc::downgrade(&state);
    state.timer.start(TimerMode::Repeated, Duration::from_millis(100), move || {
        if let Some(state) = weak.upgrade() { while let Ok(event) = state.rx.try_recv() { state.apply(event); } }
    });
    state.refresh();
    state
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rule_editor_round_trips_every_rule_without_losing_unheard_semantics() {
        for rule in [json!({"kind":"liked","value":false}),
                     json!({"kind":"not_listened","days":91,"include_unheard":false}),
                     json!({"kind":"not_listened","days":30,"include_unheard":true}),
                     json!({"kind":"unheard"}), json!({"kind":"genre","value":"Trip-Hop"}),
                     json!({"kind":"year","minimum":1980,"maximum":1999}),
                     json!({"kind":"recently_added","days":7})] {
            assert_eq!(rule_json(&decode_rule(&rule)).unwrap(), rule);
        }
    }

    #[test]
    fn editor_rejects_invalid_numbers_and_reversed_years() {
        let mut row = default_rule(1);
        row.value = "0".into();
        assert!(rule_json(&row).is_err());
        row.value = "1; DROP TABLE users".into();
        assert!(rule_json(&row).is_err());
        row = default_rule(4);
        row.value = "2025".into();
        row.maximum = "1999".into();
        assert!(rule_json(&row).is_err());
    }
}
