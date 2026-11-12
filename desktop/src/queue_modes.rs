//! Queue modes and the Hub's existing smart-shuffle protocol.
use std::cell::{Cell, RefCell};
use std::collections::HashSet;
use std::rc::Rc;
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use slint::{ComponentHandle, ModelRc, Timer, TimerMode, VecModel};
use crate::feature_context::Context;
use crate::queue::{Item, Queue, Recommendation, Repeat, Shuffle};
use crate::{ModeRecommendation, ToolsWindow};

enum ReplyKind { Plan { start: bool, revision: u64, events: Vec<Value> }, Action { row: u64, hide: bool } }
struct Reply { epoch: u64, instance: u64, kind: ReplyKind, result: Result<Value, String> }

pub(crate) struct State {
    ctx: Context,
    tools: slint::Weak<ToolsWindow>,
    timer: Timer,
    tx: SyncSender<Reply>,
    rx: Receiver<Reply>,
    epoch: Cell<u64>,
    instance: Cell<u64>,
    revision: Cell<u64>,
    busy: Cell<bool>,
    session: RefCell<String>,
    seen: RefCell<HashSet<String>>,
    events: RefCell<Vec<Value>>,
    token: Cell<Option<u64>>,
    last_snapshot: Cell<Option<(u64, f32, f32)>>,
    pending: Cell<bool>,
    due: Cell<Instant>,
    actions: RefCell<HashSet<u64>>,
    displayed: RefCell<Vec<u64>>,
}

pub(crate) fn bind(ctx: &Context, tools: &ToolsWindow) -> Rc<State> {
    let (tx, rx) = mpsc::sync_channel(16);
    let state = Rc::new(State {
        ctx: ctx.clone(), tools: tools.as_weak(), timer: Timer::default(), tx, rx,
        epoch: Cell::new(0), instance: Cell::new(ctx.queue.borrow().instance()), revision: Cell::new(0),
        busy: Cell::new(false), session: RefCell::new(String::new()), seen: RefCell::new(HashSet::new()),
        events: RefCell::new(Vec::new()), token: Cell::new(None), pending: Cell::new(false),
        last_snapshot: Cell::new(None),
        due: Cell::new(Instant::now()), actions: RefCell::new(HashSet::new()), displayed: RefCell::new(Vec::new()),
    });
    let weak = Rc::downgrade(&state);
    tools.on_modes_set_repeat(move |value| {
        let Some(s) = weak.upgrade() else { return };
        s.ctx.queue.borrow_mut().set_repeat(match value { 1 => Repeat::All, 2 => Repeat::One, _ => Repeat::Off });
        s.refresh();
    });
    let weak = Rc::downgrade(&state);
    tools.on_modes_set_shuffle(move |value| {
        let Some(s) = weak.upgrade() else { return };
        let mode = match value { 1 => Shuffle::Ordinary, 2 => Shuffle::Smart, _ => Shuffle::Off };
        if mode == Shuffle::Smart && !s.can_smart() { s.message("Умное перемешивание доступно для плейлистов и понравившихся"); return; }
        s.reset();
        s.ctx.queue.borrow_mut().set_shuffle(mode);
        s.refresh();
        s.tick();
    });
    let weak = Rc::downgrade(&state);
    tools.on_modes_enable_smart(move |on| {
        let Some(s) = weak.upgrade() else { return };
        s.reset();
        s.ctx.queue.borrow_mut().set_smart_enabled(on);
        s.refresh();
    });
    let weak = Rc::downgrade(&state);
    tools.on_modes_add(move |index| { if let Some(s) = weak.upgrade() { s.action(index, false); } });
    let weak = Rc::downgrade(&state);
    tools.on_modes_hide(move |index| { if let Some(s) = weak.upgrade() { s.action(index, true); } });
    let weak = Rc::downgrade(&state);
    state.timer.start(TimerMode::Repeated, Duration::from_millis(250), move || {
        if let Some(s) = weak.upgrade() { s.tick(); }
    });
    state.refresh();
    state
}

impl State {
    fn message(&self, message: &str) {
        if let Some(tools) = self.tools.upgrade() { tools.set_modes_status(message.into()); }
    }

    fn can_smart(&self) -> bool {
        let q = self.ctx.queue.borrow();
        q.smart_enabled() && !q.context().is_empty() && !q.is_empty()
            && q.items().len() <= 5000 && q.items().iter().all(|item| {
                let key = Queue::song_key(item);
                !key.is_empty() && key.len() <= 80
            })
    }

    fn reset(&self) {
        self.epoch.set(self.epoch.get().wrapping_add(1));
        self.session.borrow_mut().clear();
        self.seen.borrow_mut().clear();
        self.events.borrow_mut().clear();
        self.actions.borrow_mut().clear();
        self.busy.set(false);
        self.pending.set(false);
        self.token.set(None);
        self.last_snapshot.set(None);
    }

    fn refresh(&self) {
        let Some(tools) = self.tools.upgrade() else { return };
        let q = self.ctx.queue.borrow();
        tools.set_modes_repeat(match q.repeat() { Repeat::Off => 0, Repeat::All => 1, Repeat::One => 2 });
        tools.set_modes_shuffle(match q.shuffle() { Shuffle::Off => 0, Shuffle::Ordinary => 1, Shuffle::Smart => 2 });
        tools.set_modes_smart_enabled(q.smart_enabled());
        tools.set_modes_can_smart(self.can_smart());
        let mut rows = Vec::new();
        let mut ids = self.displayed.borrow_mut();
        ids.clear();
        for (i, item) in q.items().iter().enumerate() {
            if let Some(rec) = q.recommendation(i) {
                let id = q.row_token(i).unwrap();
                ids.push(id);
                rows.push(ModeRecommendation { title: item.title.clone().into(), artist: item.artist.clone().into(),
                    label: rec.label.clone().into(), current: q.index() == Some(i), busy: self.actions.borrow().contains(&id) });
            }
        }
        tools.set_modes_recommendations(ModelRc::new(VecModel::from(rows)));
        self.revision.set(q.revision());
        drop(q);
        self.ctx.refresh_queue();
    }

    /// Call immediately before an actual departure, while queue/player still describe it.
    /// Automatic EOF/promotion uses true; manual skip/selection/removal uses false.
    pub(crate) fn note_leave(&self, automatic: bool) {
        if automatic {
            // mpv may already be playing the promoted stream: never sample it here.
            self.note_leave_with_snapshot(true, 0.0, 0.0);
        } else {
            let snapshot = self.ctx.player.borrow().snapshot();
            self.note_leave_with_snapshot(false, snapshot.position_s, snapshot.duration_s);
        }
    }

    /// Also usable when the caller already holds a Player borrow. The queue must
    /// still identify the departing occurrence. Automatic departures use the
    /// cached old occurrence, not the possibly promoted Player snapshot.
    pub(crate) fn note_leave_with_snapshot(&self, automatic: bool, position: f32, duration: f32) {
        let q = self.ctx.queue.borrow();
        let Some(index) = q.index() else { return };
        let Some(rec) = q.recommendation(index) else { return };
        let item = &q.items()[index];
        let (position, duration) = if automatic {
            let (_, position, duration) = self.last_snapshot.get()
                .filter(|(token, _, _)| Some(*token) == q.current_token())
                .unwrap_or((0, 0.0, item.duration_s as f32));
            let duration = if duration > 0.0 { duration } else { item.duration_s as f32 };
            (if duration > 0.0 { duration } else { position }, duration)
        } else { (position, duration) };
        let kind = if automatic || (duration > 0.0 && position >= duration * 0.9) { "finish" } else { "skip" };
        if let Some(event) = smart_event(kind, item, rec, position, duration) {
            self.events.borrow_mut().push(event);
            self.pending.set(true);
            self.due.set(Instant::now());
        }
    }

    fn tick(&self) {
        let instance = self.ctx.queue.borrow().instance();
        if self.instance.replace(instance) != instance { self.reset(); }
        if let Some(token) = self.ctx.queue.borrow().current_token() {
            let snapshot = self.ctx.player.borrow().snapshot();
            if snapshot.position_s.is_finite() && snapshot.duration_s.is_finite() {
                let previous = self.last_snapshot.get().filter(|(old, _, _)| *old == token);
                let duration = if snapshot.duration_s > 0.0 { snapshot.duration_s }
                    else { previous.map_or(0.0, |(_, _, duration)| duration) };
                self.last_snapshot.set(Some((token, snapshot.position_s.max(0.0), duration)));
            }
        } else {
            self.last_snapshot.set(None);
        }
        while let Ok(reply) = self.rx.try_recv() {
            if reply.epoch != self.epoch.get() || reply.instance != instance { continue; }
            match reply.kind {
                ReplyKind::Plan { start, revision, events } => {
                    self.busy.set(false);
                    if self.ctx.queue.borrow().shuffle() != Shuffle::Smart { continue; }
                    match reply.result {
                        Ok(value) => {
                            if start {
                                let Some(session) = value["session_id"].as_str().filter(|s| !s.is_empty()) else {
                                    self.ctx.queue.borrow_mut().set_shuffle(Shuffle::Ordinary);
                                    self.message("Хаб вернул некорректную сессию; включено обычное перемешивание");
                                    self.refresh();
                                    continue;
                                };
                                *self.session.borrow_mut() = session.to_owned();
                                // Do not overwrite queue edits made while the request was in flight.
                                if self.ctx.queue.borrow().revision() == revision {
                                    let own: Vec<_> = value["own"].as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_owned).collect();
                                    self.ctx.queue.borrow_mut().apply_smart_order(&own);
                                }
                            }
                            self.insert_recs(&value);
                            self.pending.set(self.pending.get() || value["pending"].as_bool().unwrap_or(false) || !self.events.borrow().is_empty());
                            self.due.set(Instant::now() + Duration::from_secs(4));
                            self.message("Умное перемешивание: рекомендации добавляются между песнями");
                            self.refresh();
                        }
                        Err(error) => {
                            if start {
                                self.ctx.queue.borrow_mut().set_shuffle(Shuffle::Ordinary);
                                self.message(&format!("Умное перемешивание недоступно; обычное перемешивание. {error}"));
                                self.refresh();
                            } else {
                                self.events.borrow_mut().splice(0..0, events);
                                self.pending.set(true);
                                self.due.set(Instant::now() + Duration::from_secs(15));
                                self.message(&error);
                            }
                        }
                    }
                }
                ReplyKind::Action { row, hide } => {
                    self.actions.borrow_mut().remove(&row);
                    match reply.result {
                        Ok(value) => {
                            let at = self.ctx.queue.borrow().row_index(row);
                            if let Some(at) = at {
                                if hide {
                                    let current = self.ctx.queue.borrow().index() == Some(at);
                                    let was_playing = self.ctx.player.borrow().snapshot().playing;
                                    if current { self.note_leave(false); }
                                    let advanced = current && self.ctx.queue.borrow_mut().next();
                                    self.ctx.queue.borrow_mut().remove(at);
                                    if current {
                                        if advanced && self.ctx.queue.borrow().current().is_some() {
                                            self.ctx.play_current();
                                            if !was_playing { self.ctx.player.borrow_mut().set_paused(true); }
                                        } else { self.ctx.player.borrow_mut().stop(); }
                                    }
                                } else { self.ctx.queue.borrow_mut().mark_own(row); }
                            }
                            self.message(if hide { "Трек больше не рекомендуется здесь" } else if value["status"] == "downloading" { "Трек скачивается и появится в плейлисте" } else { "Трек добавлен" });
                        }
                        Err(error) => self.message(&error),
                    }
                    self.refresh();
                }
            }
        }
        if self.revision.get() != self.ctx.queue.borrow().revision() { self.refresh(); }
        if self.ctx.queue.borrow().shuffle() != Shuffle::Smart { return; }
        if !self.can_smart() { return; }
        if self.session.borrow().is_empty() && !self.busy.get() {
            // A persisted queue carries the real session, not a new recommendation run.
            let restored = {
                let q = self.ctx.queue.borrow();
                (0..q.items().len()).find_map(|i| q.recommendation(i).map(|r| r.session.clone()))
            };
            if let Some(session) = restored {
                *self.session.borrow_mut() = session;
                let q = self.ctx.queue.borrow();
                self.seen.borrow_mut().extend(q.items().iter().enumerate().filter(|(i, _)| q.recommendation(*i).is_some()).map(|(_, item)| item.listen_ref.clone()));
                self.pending.set(true);
            } else { self.request(true); return; }
        }
        let token = self.ctx.queue.borrow().current_token();
        if self.token.replace(token) != token {
            let q = self.ctx.queue.borrow();
            if let Some(index) = q.index() {
                if let Some(rec) = q.recommendation(index) {
                    if let Some(event) = smart_event("start", &q.items()[index], rec, 0.0, 0.0) { self.events.borrow_mut().push(event); }
                }
            }
            self.pending.set(true);
            self.due.set(Instant::now());
        }
        if self.pending.get() && Instant::now() >= self.due.get() && !self.busy.get() { self.request(false); }
    }

    fn request(&self, start: bool) {
        if self.busy.replace(true) { return; }
        let q = self.ctx.queue.borrow();
        let events = if start { Vec::new() } else {
            let mut pending = self.events.borrow_mut();
            let count = pending.len().min(500);
            pending.drain(..count).collect()
        };
        let path = if start { "/api/smart-shuffle/start".into() } else { format!("/api/smart-shuffle/{}/next", encode(&self.session.borrow())) };
        let body = if start { json!({"context": q.context(), "songs": q.own_keys(), "current": q.current().map(Queue::song_key), "title": q.name(), "origin": if q.context() == "liked" { "liked_page" } else { "playlist_page" }, "client": "desktop"}) }
            else { json!({"played": events, "at": q.own_at()}) };
        let (epoch, instance, revision) = (self.epoch.get(), q.instance(), q.revision());
        let hub = self.ctx.hub.hub.clone();
        let tx = self.tx.clone();
        self.pending.set(false);
        std::thread::spawn(move || {
            let result = hub.send("POST", &path, Some(body));
            let _ = tx.send(Reply { epoch, instance, kind: ReplyKind::Plan { start, revision, events }, result });
        });
    }

    fn insert_recs(&self, value: &Value) {
        let mut q = self.ctx.queue.borrow_mut();
        let label = if q.context() == "liked" { "Рекомендация на основе любимых песен".to_owned() } else { format!("Рекомендация на основе «{}»", q.name()) };
        for rec in value["recs"].as_array().into_iter().flatten() {
            let Some(reference) = rec["ref"].as_str().filter(|r| !r.is_empty()) else { continue };
            if !self.seen.borrow_mut().insert(reference.to_owned()) { continue; }
            let Some(after) = rec["after"].as_str() else { continue };
            let text = |key: &str| rec[key].as_str().unwrap_or("").to_owned();
            let artist = rec["artists"].as_array().map(|a| a.iter().filter_map(Value::as_str).collect::<Vec<_>>().join(", ")).filter(|s| !s.is_empty()).unwrap_or_else(|| text("artist"));
            let item = Item { id: if rec["owned"].as_bool().unwrap_or(false) { text("nd_song_id") } else { String::new() }, path: String::new(), listen_ref: reference.into(), title: text("title"), artist, album: text("album_title"), meta: label.clone(), duration_s: rec["duration_s"].as_f64().unwrap_or(0.0).max(0.0) as u32 };
            q.insert_recommendation(after, item, Recommendation { session: self.session.borrow().clone(), label: label.clone() });
        }
    }

    fn action(&self, index: i32, hide: bool) {
        let Some(row) = usize::try_from(index).ok().and_then(|i| self.displayed.borrow().get(i).copied()) else { return };
        let q = self.ctx.queue.borrow();
        let Some(at) = q.row_index(row) else { return };
        let Some(rec) = q.recommendation(at) else { return };
        if self.actions.borrow().len() >= 8 || !self.actions.borrow_mut().insert(row) { return; }
        let item = q.items()[at].clone();
        let path = format!("/api/smart-shuffle/{}/{}", encode(&rec.session), if hide { "hide" } else { "add" });
        let liked = q.context() == "liked";
        let (epoch, instance) = (self.epoch.get(), q.instance());
        let hub = self.ctx.hub.hub.clone();
        let tx = self.tx.clone();
        drop(q);
        self.refresh();
        std::thread::spawn(move || {
            let result = (|| {
                let body = json!({"ref": if item.listen_ref.is_empty() { None } else { Some(&item.listen_ref) }, "nd_song_id": if item.id.is_empty() { None } else { Some(&item.id) }});
                if liked && !hide {
                    let mut target = body.clone();
                    target["target_type"] = json!("track");
                    target["target_data"] = json!({"title": item.title, "artist": item.artist, "album": item.album, "duration_s": item.duration_s});
                    hub.send("POST", "/api/star", Some(json!({"targets": [target], "unstar": false, "client": "desktop", "context": "liked"})))?;
                }
                hub.send("POST", &path, Some(body))
            })();
            let _ = tx.send(Reply { epoch, instance, kind: ReplyKind::Action { row, hide }, result });
        });
    }
}

fn smart_event(kind: &str, item: &Item, rec: &Recommendation, position: f32, duration: f32) -> Option<Value> {
    let mut event = crate::hub::wave_event(kind, &item.id, &item.listen_ref, position, duration, &rec.session, "playlist_page")?;
    event["context"] = json!(format!("smart:{}", rec.session));
    event["launch_id"] = json!(format!("smart:{}", rec.session));
    Some(event)
}

fn encode(value: &str) -> String {
    value.bytes().map(|b| if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) { (b as char).to_string() } else { format!("%{b:02X}") }).collect()
}
