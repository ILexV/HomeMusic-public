//! Account-scoped recommendation tools. All HTTP runs on workers; only replies touch Slint.
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::Duration;
use serde_json::{json, Value};
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};
use crate::{feature_context::Context, hub, RecommendationRow, ToolsWindow};

enum Job {
    View { page: i32, key: String, generation: u64 },
    Pause, Undo,
    Explain { target: String },
    Wave { session: String, instance: u64, played: Vec<Value> },
    Album { ticket: u64 },
    Download { ticket: u64 },
    Seen,
    Recording { token: Option<u64>, instance: u64, reference: String },
    UndoRecording,
}
struct Reply { job: Job, result: Result<Value, String> }

pub(crate) struct State {
    ctx: Context, tools: slint::Weak<ToolsWindow>, timer: slint::Timer,
    tx: Sender<Reply>, rx: Receiver<Reply>, requests: Cell<usize>,
    revision: Cell<u64>, selected: RefCell<Vec<u64>>, active: Cell<bool>,
    view_generation: Cell<u64>,
    policy_refresh: Cell<bool>,
    starting: Cell<bool>,
    recording: RefCell<Option<(u64, String)>>,
}

pub(crate) fn bind(ctx: &Context, tools: &ToolsWindow) -> Rc<State> {
    let (tx, rx) = mpsc::channel();
    let state = Rc::new(State { ctx: ctx.clone(), tools: tools.as_weak(), timer: slint::Timer::default(),
        tx, rx, requests: Cell::new(0), revision: Cell::new(0), selected: RefCell::new(Vec::new()), active: Cell::new(false),
        view_generation: Cell::new(0), policy_refresh: Cell::new(false), starting: Cell::new(false),
        recording: RefCell::new(None) });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_refresh(move || { if let Some(s) = weak.upgrade() { s.refresh(); } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_start_wave(move || { if let Some(s) = weak.upgrade() { s.start_wave(); } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_apply_wave(move || { if let Some(s) = weak.upgrade() { s.apply_wave(); } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_load_wave(move || { if let Some(s) = weak.upgrade() { s.load_wave(); } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_target_changed(move || { if let Some(s) = weak.upgrade() {
        if let Some(t) = s.tools.upgrade() { t.set_recommendations_explanation("".into()); }
    } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_pause_track(move |action| { if let Some(s) = weak.upgrade() { s.pause(&action); } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_undo_pause(move |id| { if let Some(s) = weak.upgrade() {
        s.request(Job::Undo, "DELETE", format!("/api/recommendations/pauses/{}", encode(&id)), None);
    } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_explain(move || { if let Some(s) = weak.upgrade() { s.explain(); } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_open_release(move |reference| { if let Some(s) = weak.upgrade() { s.open_album(&reference); } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_seen_release(move |reference, seen| { if let Some(s) = weak.upgrade() {
        s.request(Job::Seen, "PUT", "/api/releases/seen".into(), Some(json!({"ref": reference.as_str(), "seen": seen})));
    } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_wrong_recording(move || { if let Some(s) = weak.upgrade() { s.wrong_recording(); } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_undo_recording(move || { if let Some(s) = weak.upgrade() {
        if let Some((id, _)) = s.recording.borrow().as_ref() {
            s.request(Job::UndoRecording, "DELETE", format!("/api/recording-feedback/{id}"), None);
        }
    } });
    let weak = Rc::downgrade(&state);
    tools.on_recommendations_retry_recording(move || { if let Some(s) = weak.upgrade() {
        let same = s.recording.borrow().as_ref().is_some_and(|(_, reference)|
            s.ctx.queue.borrow().current().is_some_and(|item| item.listen_ref == *reference));
        if same { s.ctx.play_current(); }
        else { s.status("Текущая песня изменилась. Выберите сообщённый трек в очереди."); }
    } });
    if let Some(ui) = ctx.ui.upgrade() {
        let weak = Rc::downgrade(&state);
        ui.on_album_download(move || { if let Some(s) = weak.upgrade() { s.download_album(); } });
    }
    let weak = Rc::downgrade(&state);
    state.timer.start(slint::TimerMode::Repeated, Duration::from_millis(250), move || {
        if let Some(s) = weak.upgrade() { s.tick(); }
    });
    state
}

impl State {
    fn status(&self, text: &str) {
        if let Some(t) = self.tools.upgrade() { t.set_recommendations_status(text.into()); }
    }
    fn wrong_recording(&self) {
        if self.requests.get() != 0 { return; }
        let q = self.ctx.queue.borrow();
        let Some(item) = q.current() else { self.status("Нет текущего трека"); return };
        if item.listen_ref.is_empty() || !item.id.is_empty() {
            self.status("Сообщение доступно для полной записи из источника, не для локального файла.");
            return;
        }
        let reference = item.listen_ref.clone();
        let job = Job::Recording { token: q.current_token(), instance: q.instance(), reference: reference.clone() };
        drop(q);
        self.requests.set(self.requests.get() + 1);
        if let Some(t) = self.tools.upgrade() { t.set_recommendations_busy(true); }
        self.status("Проверяю привязку текущей записи…");
        let client = self.ctx.hub.hub.clone();
        let tx = self.tx.clone();
        std::thread::spawn(move || {
            let result = client.send("GET", &format!("/api/recording-feedback/current?ref={}", encode(&reference)), None)
                .and_then(|binding| client.send("POST", "/api/recording-feedback",
                    Some(json!({"ref": reference, "binding": binding["binding"]}))));
            let _ = tx.send(Reply { job, result });
        });
    }
    fn request(&self, job: Job, method: &'static str, path: String, body: Option<Value>) {
        self.requests.set(self.requests.get() + 1);
        if let Some(t) = self.tools.upgrade() { t.set_recommendations_busy(true); }
        self.status("Загрузка…");
        let client = self.ctx.hub.hub.clone();
        let tx = self.tx.clone();
        std::thread::spawn(move || { let result = client.send(method, &path, body); let _ = tx.send(Reply { job, result }); });
    }
    fn view_key(t: &ToolsWindow) -> String {
        match t.get_recommendations_page() {
            1 => format!("/api/recommendations/quality?days={}", [7, 30, 90][t.get_recommendations_days().clamp(0, 2) as usize]),
            2 => "/api/recommendations/pauses".into(),
            3 => format!("/api/releases?period={}&unseen={}", if t.get_recommendations_period() == 0 { "week" } else { "month" }, t.get_recommendations_unseen()),
            _ => String::new(),
        }
    }
    fn refresh(&self) {
        let Some(t) = self.tools.upgrade() else { return };
        let key = Self::view_key(&t);
        let generation = self.view_generation.get().wrapping_add(1);
        self.view_generation.set(generation);
        if !key.is_empty() {
            match t.get_recommendations_page() {
                1 => t.set_recommendations_quality("".into()),
                2 => t.set_recommendations_pauses(ModelRc::default()),
                3 => t.set_recommendations_releases(ModelRc::default()),
                _ => {},
            }
            self.request(Job::View { page: t.get_recommendations_page(), key: key.clone(), generation }, "GET", key, None);
        }
        else { self.status(""); }
    }
    fn preferences(t: &ToolsWindow) -> Value {
        json!({"energy": (["any", "calm", "energetic"][t.get_recommendations_energy().clamp(0, 2) as usize]),
            "vocals": (["any", "vocal", "instrumental"][t.get_recommendations_vocals().clamp(0, 2) as usize]),
            "discovery": (["balanced", "familiar", "new"][t.get_recommendations_discovery().clamp(0, 2) as usize])})
    }
    fn start_wave(&self) {
        let (Some(t), Some(ui)) = (self.tools.upgrade(), self.ctx.ui.upgrade()) else { return };
        if self.ctx.hub.wave_busy.get() || self.ctx.hub.wave.borrow().asking { self.status("Дождитесь ответа текущей волны"); return; }
        *self.ctx.hub.wave_start_preferences.borrow_mut() = Self::preferences(&t);
        crate::begin_wave(&ui, &self.ctx.hub, String::new(), "home_card");
        t.set_recommendations_energy(0); t.set_recommendations_vocals(0); t.set_recommendations_discovery(0);
        self.status("Подготовка полных записей новой волны…");
        self.starting.set(true);
    }
    fn load_wave(&self) {
        let Some(t) = self.tools.upgrade() else { return };
        let wave = self.ctx.hub.wave.borrow();
        let p = &wave.preferences;
        t.set_recommendations_energy(match p["energy"].as_str() { Some("calm") => 1, Some("energetic") => 2, _ => 0 });
        t.set_recommendations_vocals(match p["vocals"].as_str() { Some("vocal") => 1, Some("instrumental") => 2, _ => 0 });
        t.set_recommendations_discovery(match p["discovery"].as_str() { Some("familiar") => 1, Some("new") => 2, _ => 0 });
    }
    fn apply_wave(&self) {
        let Some(t) = self.tools.upgrade() else { return };
        self.apply_preferences(Self::preferences(&t));
    }
    fn apply_preferences(&self, preferences: Value) {
        let mut wave = self.ctx.hub.wave.borrow_mut();
        if !wave.on() { self.status("Нет активной волны"); return; }
        if wave.asking || self.ctx.hub.wave_busy.get() { self.status("Дождитесь подготовки следующей партии"); return; }
        let session = wave.id.clone();
        wave.asking = true;
        let played = std::mem::take(&mut wave.pending);
        drop(wave);
        let q = self.ctx.queue.borrow();
        let queued: Vec<_> = q.items().iter().skip(q.index().map_or(0, |i| i + 1))
            .filter(|item| !item.listen_ref.is_empty()).map(|item| item.listen_ref.clone()).collect();
        let body = json!({"played": &played, "queued": queued, "preferences": preferences});
        self.request(Job::Wave { session: session.clone(), instance: q.instance(), played }, "POST", format!("/api/wave/{}/next", encode(&session)), Some(body));
    }
    fn target(&self) -> Option<(String, crate::queue::Item, String)> {
        let t = self.tools.upgrade()?;
        let q = self.ctx.queue.borrow();
        let i = if t.get_recommendations_target() <= 0 { q.index()? }
            else { let row = *self.selected.borrow().get(t.get_recommendations_target() as usize - 1)?; q.row_index(row)? };
        let row = q.row_token(i)?;
        Some((format!("{}:{row}", q.instance()), q.items()[i].clone(), crate::listening_events::context(&q, &self.ctx.hub, i)))
    }
    fn pause(&self, action: &str) {
        let Some((_, item, _)) = self.target() else { self.status("Выберите песню из очереди"); return };
        if item.id.is_empty() && item.listen_ref.is_empty() { self.status("Локальный файл не имеет подтверждённой личности в каталоге"); return; }
        self.request(Job::Pause, "POST", "/api/recommendations/pauses".into(), Some(json!({"action": action,
            "ref": optional(&item.listen_ref), "nd_song_id": optional(&item.id)})));
    }
    fn prune_paused(&self, pause: &Value) {
        if !matches!(text(pause, "action"), "today" | "week") { return; }
        let mut q = self.ctx.queue.borrow_mut();
        let recommendation_queue = self.ctx.hub.wave.borrow().on() || q.context().starts_with("mix:");
        let start = q.index().map_or(0, |i| i + 1);
        for i in (start..q.items().len()).rev() {
            if !recommendation_queue && q.recommendation(i).is_none() { continue; }
            let item = &q.items()[i];
            if (!text(pause, "ref").is_empty() && item.listen_ref == text(pause, "ref"))
                || (!text(pause, "nd_song_id").is_empty() && item.id == text(pause, "nd_song_id")) { q.remove(i); }
        }
        drop(q);
        self.ctx.refresh_queue();
    }
    fn explain(&self) {
        let Some((target, item, context)) = self.target() else { self.status("Выберите песню из очереди"); return };
        if !["wave:", "mix:", "smart:"].iter().any(|p| context.starts_with(p)) {
            if let Some(t) = self.tools.upgrade() { t.set_recommendations_explanation("Для ручного выбора нет сохранённого решения рекомендателя.".into()); }
            return;
        }
        self.request(Job::Explain { target }, "GET", format!("/api/recommendations/explain?context={}&ref={}&nd_song_id={}", encode(&context), encode(&item.listen_ref), encode(&item.id)), None);
    }
    fn open_album(&self, reference: &str) {
        let Some(ui) = self.ctx.ui.upgrade() else { return };
        let ticket = self.ctx.hub.album_generation.get().wrapping_add(1);
        self.ctx.hub.album_generation.set(ticket);
        *self.ctx.hub.album_preview.borrow_mut() = Some(crate::AlbumPreview {
            id: String::new(), cover_id: String::new(), catalog_ref: reference.into(), tracks: Vec::new(),
        });
        ui.set_album_title("Загрузка релиза…".into()); ui.set_album_artist("".into());
        ui.set_album_tracks(ModelRc::default()); ui.set_album_total("".into());
        ui.set_album_error("".into()); ui.set_album_note("".into()); ui.set_album_busy(true);
        ui.set_album_has_cover(false); ui.set_album_can_download(true); ui.set_album_open(true);
        if ui.get_mode() == crate::Mode::Mini { ui.invoke_set_mode(1); }
        if let Some(t) = self.tools.upgrade() { let _ = t.hide(); }
        self.request(Job::Album { ticket }, "GET", format!("/api/catalog/album/{}", encode(reference)), None);
    }
    fn download_album(&self) {
        let reference = self.ctx.hub.album_preview.borrow().as_ref().map(|a| a.catalog_ref.clone()).unwrap_or_default();
        if reference.is_empty() { return; }
        if let Some(ui) = self.ctx.ui.upgrade() { ui.set_album_note("Отправка задания загрузки…".into()); }
        self.request(Job::Download { ticket: self.ctx.hub.album_generation.get() }, "POST", "/api/downloads".into(), Some(json!({"album_ref": reference})));
    }
    fn tick(&self) {
        let Some(t) = self.tools.upgrade() else { return };
        let active = t.window().is_visible() && t.get_active_panel() == 8;
        if self.active.replace(active) != active && active { self.refresh(); }
        if self.starting.get() && !self.ctx.hub.wave_busy.get() {
            self.starting.set(false);
            self.status(&self.ctx.hub.hub_note.borrow());
        }
        if self.policy_refresh.get() && !self.ctx.hub.wave.borrow().asking && !self.ctx.hub.wave_busy.get() {
            self.policy_refresh.set(false);
            if self.ctx.hub.wave.borrow().on() {
                let preferences = self.ctx.hub.wave.borrow().preferences.clone();
                self.apply_preferences(preferences);
            }
        }
        let q = self.ctx.queue.borrow();
        if self.revision.replace(q.revision()) != q.revision() {
            let mut targets = vec![SharedString::from("Текущая песня")];
            let previous = if t.get_recommendations_target() > 0 { self.selected.borrow().get(t.get_recommendations_target() as usize - 1).copied() } else { None };
            let mut rows = self.selected.borrow_mut(); rows.clear();
            for (i, item) in q.items().iter().enumerate() {
                targets.push(format!("{}. {} — {}", i + 1, item.artist, item.title).into());
                rows.push(q.row_token(i).unwrap());
            }
            t.set_recommendations_targets(ModelRc::new(VecModel::from(targets)));
            t.set_recommendations_target(previous.and_then(|row| rows.iter().position(|r| *r == row)).map_or(0, |i| i as i32 + 1));
            t.set_recommendations_explanation("".into());
        }
        drop(q);
        let wave = self.ctx.hub.wave.borrow();
        t.set_recommendations_wave_active(wave.on());
        if t.get_recommendations_wave_status().as_str() != wave.notice {
            t.set_recommendations_wave_status(wave.notice.as_str().into());
        }
        drop(wave);
        while let Ok(reply) = self.rx.try_recv() {
            self.requests.set(self.requests.get().saturating_sub(1));
            self.apply(reply, &t);
        }
        t.set_recommendations_busy(self.requests.get() != 0 || self.ctx.hub.wave_busy.get());
    }
    fn apply(&self, reply: Reply, t: &ToolsWindow) {
        match reply.job {
            Job::Recording { token, instance, reference } => match reply.result {
                Err(error) => self.status(&error),
                Ok(value) => {
                    if let Some(id) = value["id"].as_u64() {
                        *self.recording.borrow_mut() = Some((id, reference));
                        t.set_recommendations_recording_reported(true);
                    }
                    let current = self.ctx.queue.borrow();
                    if current.instance() == instance && current.current_token() == token {
                        let snapshot = self.ctx.player.borrow().snapshot();
                        self.ctx.hub.listening.borrow_mut().interrupted(&snapshot, &self.ctx.hub);
                        self.ctx.player.borrow_mut().set_paused(true);
                    }
                    self.status(text(&value, "message"));
                }
            },
            Job::UndoRecording => match reply.result {
                Err(error) => self.status(&error),
                Ok(_) => {
                    *self.recording.borrow_mut() = None;
                    t.set_recommendations_recording_reported(false);
                    self.status("Сообщение отменено. Запись снова разрешена.");
                }
            },
            Job::View { page, key, generation } => {
                if generation != self.view_generation.get() || page != t.get_recommendations_page() || key != Self::view_key(t) { return; }
                match reply.result {
                    Err(error) => self.status(&error),
                    Ok(value) => match page {
                        1 => { t.set_recommendations_quality(quality_text(&value).into()); self.status(""); },
                        2 => {
                            let rows: Vec<_> = value["items"].as_array().into_iter().flatten().map(|row| RecommendationRow {
                                key: identity(&row["id"]).into(), title: text(row, "label").into(),
                                detail: format!("{} · до {}", match text(row, "action") { "today" => "Не сегодня", "week" => "Пауза на неделю", "less_artist" => "Реже исполнителя (30 дней)", _ => "Ограничение" }, text(row, "expires_at")).into(), flag: false,
                            }).collect();
                            self.status(if rows.is_empty() { "Нет временных ограничений" } else { "" });
                            t.set_recommendations_pauses(ModelRc::new(VecModel::from(rows)));
                        }
                        3 => {
                            let rows: Vec<_> = value["items"].as_array().into_iter().flatten().map(|row| RecommendationRow {
                                key: text(row, "ref").into(), title: format!("{} — {}", text(row, "artist"), text(row, "title")).into(),
                                detail: format!("{} · {} треков · {}", text(row, "release_date"), row["track_count"].as_u64().map(|n| n.to_string()).unwrap_or_else(|| "неизвестно".into()), if row["seen"].as_bool().unwrap_or(false) { "просмотрен" } else { "новый" }).into(), flag: row["seen"].as_bool().unwrap_or(false),
                            }).collect();
                            let errors = strings(&value["errors"]).join("\n");
                            self.status(&format!("{}{}{}", if value["partial"].as_bool().unwrap_or(false) { "Неполные данные каталога. " } else { "" }, if rows.is_empty() { "Релизы не найдены. " } else { "" }, errors));
                            t.set_recommendations_releases(ModelRc::new(VecModel::from(rows)));
                        }
                        _ => {},
                    }
                }
            }
            Job::Pause => match reply.result {
                Err(error) => self.status(&error),
                Ok(value) => {
                    self.prune_paused(&value);
                    self.policy_refresh.set(true);
                    self.status("Временное ограничение сохранено. Текущая песня не остановлена.");
                }
            },
            Job::Undo => match reply.result {
                Err(error) => self.status(&error),
                Ok(_) => { self.policy_refresh.set(true); self.refresh(); }
            },
            Job::Seen => match reply.result {
                Err(error) => self.status(&error),
                Ok(_) => { self.status("Сохранено на сервере"); if t.get_recommendations_page() != 0 { self.refresh(); } }
            },
            Job::Explain { target } => {
                if self.target().is_none_or(|(current, _, _)| target != current) { return; }
                match reply.result {
                    Err(error) => { t.set_recommendations_explanation("".into()); self.status(&error); },
                    Ok(value) => {
                        let reasons = strings(&value["reasons"]);
                        t.set_recommendations_explanation(if reasons.is_empty() { "Нет сохранённых причин для этой песни.".into() } else { reasons.join("\n").into() });
                        self.status("");
                    }
                }
            }
            Job::Wave { session, instance, played } => {
                if self.ctx.hub.wave.borrow().id != session { return; }
                self.ctx.hub.wave.borrow_mut().asking = false;
                if self.ctx.queue.borrow().instance() != instance { return; }
                match reply.result {
                    Err(error) => {
                        self.ctx.hub.wave.borrow_mut().pending.extend(played);
                        self.status(&error);
                    },
                    Ok(value) => {
                        let batch = hub::parse_wave(&value);
                        if batch.session_id != session { self.status("Сервер вернул другую сессию; очередь не изменена"); return; }
                        {
                            let mut wave = self.ctx.hub.wave.borrow_mut();
                            wave.preferences = batch.preferences; wave.notice = batch.notice;
                            wave.exhausted = batch.tracks.is_empty() && !batch.pending;
                            wave.retry_at = Some(std::time::Instant::now() + Duration::from_secs(3));
                        }
                        {
                            let mut q = self.ctx.queue.borrow_mut();
                            replace_upcoming(&mut q);
                        }
                        crate::append_playables(&self.ctx.queue, &batch.tracks);
                        self.ctx.refresh_queue();
                        if let Some(ui) = self.ctx.ui.upgrade() { crate::record_playback(&ui, &self.ctx.hub, &self.ctx.queue.borrow(), &self.ctx.player.borrow().snapshot(), true); }
                        self.status(if batch.tracks.is_empty() { "Настройки применены. Полные записи ещё готовятся; текущая песня сохранена." } else { "Настройки применены к следующим песням; текущая не перезапущена." });
                    }
                }
            }
            Job::Album { ticket } => {
                let Some(ui) = self.ctx.ui.upgrade() else { return };
                if ticket != self.ctx.hub.album_generation.get() || !ui.get_album_open() { return; }
                ui.set_album_busy(false);
                match reply.result {
                    Err(error) => ui.set_album_error(error.into()),
                    Ok(value) => {
                        let batch = hub::parse_wave(&value);
                        let album = crate::queue_from_playables(text(&value, "title"), &batch.tracks);
                        ui.set_album_title(text(&value, "title").into()); ui.set_album_artist(text(&value, "artist").into());
                        ui.set_album_total(album.total_label().into());
                        let rows = album.items().iter().enumerate().map(|(i, item)| crate::QueueItem {
                            source: i as i32, number: (i + 1).to_string().into(), title: item.title.clone().into(), meta: item.artist.clone().into(), duration: crate::row_duration(item).into(), current: false,
                        }).collect::<Vec<_>>();
                        ui.set_album_tracks(ModelRc::new(VecModel::from(rows)));
                        if let Some(preview) = self.ctx.hub.album_preview.borrow_mut().as_mut() { preview.tracks = album.into_items(); }
                    }
                }
            }
            Job::Download { ticket } => {
                let Some(ui) = self.ctx.ui.upgrade() else { return };
                if ticket != self.ctx.hub.album_generation.get() { return; }
                ui.set_album_note(match reply.result { Ok(value) => hub::download_summary(&value), Err(error) => error }.into());
            }
        }
    }
}

fn replace_upcoming(q: &mut crate::queue::Queue) {
    let start = q.index().map_or(0, |i| i + 1);
    for i in (start..q.items().len()).rev() { q.remove(i); }
}
fn optional(value: &str) -> Option<&str> { (!value.is_empty()).then_some(value) }
fn text<'a>(value: &'a Value, key: &str) -> &'a str { value[key].as_str().unwrap_or("") }
fn identity(value: &Value) -> String { value.as_str().map(str::to_owned).or_else(|| value.as_i64().map(|n| n.to_string())).unwrap_or_default() }
fn strings(value: &Value) -> Vec<&str> { value.as_array().into_iter().flatten().filter_map(Value::as_str).collect() }
fn encode(value: &str) -> String {
    value.bytes().map(|b| if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) { (b as char).to_string() } else { format!("%{b:02X}") }).collect()
}
fn rate(value: &Value) -> String { value.as_f64().map(|n| format!("{:.1}%", n * 100.0)).unwrap_or_else(|| "нет данных".into()) }
fn quality_text(v: &Value) -> String {
    let count = |key: &str| v[key].as_u64().unwrap_or(0);
    let mut out = format!("Попытки: {} · старты: {} · пригодны для оценки: {}\nДослушано: {} ({})\nРанние пропуски: {} ({})\nОшибки воспроизведения: {} · прервано: {}\nНовые песни: {} · лайки новых: {} ({})\nПовторы: {} ({})\n\nОшибки и прерывания исключены из оценки вкуса; отсутствие данных не означает 0%.\nДослушивания и ранние пропуски — среди пригодных попыток; лайки новых — среди новых прослушиваний; повторы — среди пригодных попыток.\n\nИсточники (множитель и число обучающих примеров):\n",
        count("attempts"), count("starts"), count("eligible_plays"), count("completed"), rate(&v["completion_rate"]), count("early_skips"), rate(&v["early_skip_rate"]), count("playback_errors"), count("interrupted"), count("new_track_plays"), count("new_track_likes"), rate(&v["new_track_like_rate"]), count("repeats"), rate(&v["repeat_rate"]));
    for source in v["sources"].as_array().into_iter().flatten() {
        out.push_str(&format!("{}: x{:.2} · примеров {} · попыток {} · дослушано {} · ранних пропусков {} · ошибок {}\n", text(source, "title"), source["multiplier"].as_f64().unwrap_or(1.0), source["learning_samples"].as_u64().unwrap_or(0), source["attempts"].as_u64().unwrap_or(0), source["completed"].as_u64().unwrap_or(0), source["early_skips"].as_u64().unwrap_or(0), source["playback_errors"].as_u64().unwrap_or(0)));
    }
    let recordings = &v["recordings"];
    out.push_str(&format!("\nПолные записи: {} · полных {} / проверено {} · неизвестно {} / наблюдений {}\n{}\n",
        rate(&recordings["rate"]), recordings["full"], recordings["known"], recordings["unknown"],
        recordings["observations"], strings(&recordings["limitations"]).join("\n")));
    out.push_str(&format!("\n{} · {}\n", text(v, "metric_version"), text(v, "provenance")));
    for (key, label) in [("current", "Текущий период"), ("previous", "Предыдущий период")] {
        let window = &v["comparison"][key];
        out.push_str(&format!("{}: {} — {}\nN={} · попыток {} · дослушано {} ({}) · ранних пропусков {} ({})\n",
            label, text(window, "since"), text(window, "until"), window["eligible_plays"], window["attempts"],
            window["completed"], rate(&window["completion_rate"]), window["early_skips"], rate(&window["early_skip_rate"])));
        for cohort in window["policy_cohorts"].as_array().into_iter().flatten() {
            out.push_str(&format!("Политика {} · N={} · дослушивания {} · ранние пропуски {}\n",
                text(cohort, "policy_version"), cohort["eligible_plays"], rate(&cohort["completion_rate"]), rate(&cohort["early_skip_rate"])));
        }
    }
    out.push_str(&strings(&v["comparison"]["limitations"]).join("\n"));
    out.push('\n');
    out.push_str(&strings(&v["limitations"]).join("\n"));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn absent_quality_is_not_zero_percent_and_pause_ids_accept_integers() {
        assert_eq!(rate(&Value::Null), "нет данных");
        assert_eq!(rate(&json!(0.25)), "25.0%");
        assert_eq!(identity(&json!(12)), "12");
        assert_eq!(identity(&json!("12")), "12");
    }
    #[test]
    fn reranking_preserves_current_occurrence_and_history() {
        let mut q = crate::queue::Queue::from_tracks("wave", vec![crate::queue::Item::file("a", "A"), crate::queue::Item::file("b", "B"), crate::queue::Item::file("c", "C")]);
        q.select(1); let token = q.current_token(); let instance = q.instance();
        replace_upcoming(&mut q);
        assert_eq!(q.current_token(), token); assert_eq!(q.instance(), instance);
        assert_eq!(q.index(), Some(1)); assert_eq!(q.items().len(), 2); assert_eq!(q.current().unwrap().title, "B");
    }
}
