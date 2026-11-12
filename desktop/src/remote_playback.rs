//! Account-scoped Hub session. Observing another owner never fences local playback.
use std::cell::{Cell, RefCell};
use std::collections::VecDeque;
use std::rc::Rc;
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use slint::ComponentHandle;

use crate::feature_context::Context;
use crate::player::PlaybackPhase;
use crate::queue::{Item, Queue};

struct Request {
    path: &'static str,
    body: Value,
}
struct Pending {
    command: Value,
    token: Option<u64>,
    started: Instant,
    playing: bool,
    position: f64,
}

pub(crate) struct State {
    ctx: Context,
    tools: slint::Weak<crate::ToolsWindow>,
    device: String,
    timer: slint::Timer,
    response: RefCell<Option<Receiver<(u64, Result<Value, String>)>>>,
    requests: RefCell<VecDeque<Request>>,
    commands: RefCell<VecDeque<Value>>,
    completed: RefCell<VecDeque<(String, Value)>>,
    pending: RefCell<Option<Pending>>,
    prepared: RefCell<Option<(String, Option<u64>)>>,
    view: RefCell<Value>,
    occurrence: RefCell<(Option<u64>, String)>,
    queue_cache: RefCell<(u64, Value)>,
    last_poll: Cell<Instant>,
    last_success: Cell<Option<Instant>>,
    error: RefCell<String>,
    detached: Cell<bool>,
    attach_pending: Cell<bool>,
    generation: Cell<u64>,
    claim_attempt: Cell<Option<u64>>,
    participating: Cell<bool>,
}

pub(crate) fn bind(ctx: &Context, tools: &crate::ToolsWindow) -> Rc<State> {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos();
    let state = Rc::new(State {
        ctx: ctx.clone(), tools: tools.as_weak(),
        device: format!("desktop-{:x}-{nanos:x}", std::process::id()),
        timer: slint::Timer::default(), response: RefCell::new(None),
        requests: RefCell::new(VecDeque::new()), commands: RefCell::new(VecDeque::new()),
        completed: RefCell::new(VecDeque::new()), pending: RefCell::new(None),
        prepared: RefCell::new(None), view: RefCell::new(Value::Null),
        occurrence: RefCell::new((None, String::new())), queue_cache: RefCell::new((0, Value::Null)),
        last_poll: Cell::new(Instant::now() - Duration::from_secs(3)),
        last_success: Cell::new(None), error: RefCell::new(String::new()),
        detached: Cell::new(false), attach_pending: Cell::new(false), generation: Cell::new(0),
        claim_attempt: Cell::new(None),
        participating: Cell::new(false),
    });
    let weak = Rc::downgrade(&state);
    tools.on_devices_transfer(move |target| {
        if let Some(state) = weak.upgrade() { state.transfer(target.as_str()); }
    });
    let weak = Rc::downgrade(&state);
    tools.on_devices_take_here(move || {
        if let Some(state) = weak.upgrade() {
            if state.detached.replace(false) {
                state.attach_pending.set(true);
                state.last_poll.set(Instant::now() - Duration::from_secs(3));
                state.last_success.set(None);
                state.paint();
            } else if state.owner().is_empty() { state.claim(); }
            else if state.owner() != state.device { state.transfer(&state.device); }
            else { state.claim(); }
        }
    });
    let weak = Rc::downgrade(&state);
    tools.on_devices_local_only(move || {
        if let Some(state) = weak.upgrade() {
            state.detached.set(true);
            state.attach_pending.set(false);
            state.participating.set(false);
            state.generation.set(state.generation.get().wrapping_add(1));
            state.requests.borrow_mut().clear();
            state.commands.borrow_mut().clear();
            state.pending.borrow_mut().take();
            state.prepared.borrow_mut().take();
            state.ctx.player.borrow_mut().set_paused(true);
            state.set_error("");
            state.paint();
        }
    });
    let weak = Rc::downgrade(&state);
    tools.on_devices_command(move |kind, value| {
        if let Some(state) = weak.upgrade() {
            state.enqueue("/api/playback/command", json!({
                "device_id": state.device, "target": state.owner(), "revision": state.revision(),
                "kind": kind.as_str(), "value": value,
            }));
        }
    });
    let weak = Rc::downgrade(&state);
    state.timer.start(slint::TimerMode::Repeated, Duration::from_millis(100), move || {
        if let Some(state) = weak.upgrade() { state.tick(); }
    });
    state
}

impl State {
    fn owner(&self) -> String { text(&self.view.borrow(), "owner").to_owned() }
    fn revision(&self) -> u64 { self.view.borrow()["revision"].as_u64().unwrap_or(0) }

    fn set_error(&self, error: impl Into<String>) {
        *self.error.borrow_mut() = error.into();
        if let Some(tools) = self.tools.upgrade() {
            tools.set_devices_error(self.error.borrow().as_str().into());
        }
    }

    fn enqueue(&self, path: &'static str, body: Value) {
        if self.detached.get() {
            self.set_error("Сейчас включено отдельное локальное воспроизведение");
            return;
        }
        if self.requests.borrow().len() >= 8 {
            self.set_error("Предыдущая команда ещё выполняется");
            return;
        }
        self.requests.borrow_mut().push_back(Request { path, body });
    }

    fn transfer(&self, target: &str) {
        self.enqueue("/api/playback/handoff", json!({
            "device_id": self.device, "target": target, "revision": self.revision(),
        }));
    }

    fn claim(&self) {
        match self.snapshot() {
            Ok(Some(snapshot)) => self.enqueue("/api/playback/claim", json!({
                "device_id": self.device, "revision": self.revision(), "state": snapshot,
            })),
            Ok(None) => self.set_error("Сначала выберите трек"),
            Err(error) => self.set_error(error),
        }
    }

    fn snapshot(&self) -> Result<Option<Value>, String> {
        let queue = self.ctx.queue.borrow();
        let Some(index) = queue.index() else { return Ok(None) };
        if queue.items().len() > 1000 {
            return Err("Для передачи очередь должна содержать не более 1000 треков".into());
        }
        let mut cache = self.queue_cache.borrow_mut();
        if cache.0 != queue.revision() || cache.1.is_null() {
            let mut rows = Vec::with_capacity(queue.items().len());
            for item in queue.items() {
                if item.id.is_empty() && item.listen_ref.is_empty() {
                    return Err("В очереди есть локальные файлы без ID библиотеки: их нельзя передать".into());
                }
                let reference = if item.listen_ref.is_empty() { format!("nd:{}", item.id) } else { item.listen_ref.clone() };
                rows.push(json!({"ref": reference, "nd_song_id": if item.id.is_empty() { None } else { Some(&item.id) },
                    "title": item.title, "artist": item.artist, "album": item.album, "duration_s": item.duration_s}));
            }
            *cache = (queue.revision(), Value::Array(rows));
        }
        let mut occurrence = self.occurrence.borrow_mut();
        if occurrence.0 != queue.current_token() {
            *occurrence = (queue.current_token(), format!("{}:{}", self.device, queue.current_token().unwrap_or(0)));
        }
        let playback = self.ctx.player.borrow().snapshot();
        let modes = queue.modes_snapshot();
        let repeat = match modes["repeat"].as_u64().unwrap_or(0) { 1 => "all", 2 => "one", _ => "off" };
        Ok(Some(json!({"queue": cache.1, "index": index, "occurrence": occurrence.1,
            "position": playback.position_s.max(0.0), "playing": playback.playing,
            "volume": playback.volume, "repeat": repeat, "shuffle": modes["shuffle"].as_u64().unwrap_or(0) != 0})))
    }

    fn tick(&self) {
        let result = self.response.borrow().as_ref().and_then(|rx| rx.try_recv().ok());
        if let Some((generation, result)) = result {
            self.response.borrow_mut().take();
            if generation == self.generation.get() && !self.detached.get() {
                match result {
                    Ok(view) => self.accept_view(view),
                    Err(error) => self.set_error(error),
                }
            }
        }
        if self.detached.get() { return; }
        let (has_owner, is_owner, handoff_target, recovery_source) = {
            let view = self.view.borrow();
            let owner = text(&view, "owner");
            let handoff = &view["handoff"];
            let active = matches!(text(handoff, "phase"), "preparing" | "stopping" | "starting");
            (!owner.is_empty(), owner == self.device,
                active && text(handoff, "target") == self.device,
                active && handoff["recovery"].as_bool().unwrap_or(false)
                    && text(handoff, "source") == self.device)
        };
        let lease_lost = self.last_success.get().is_some_and(|at| at.elapsed() > Duration::from_secs(12));
        if lease_lost && (is_owner || handoff_target) {
            self.ctx.player.borrow_mut().set_paused(true);
            self.set_error("Связь с Хабом потеряна: общая сессия приостановлена");
            let pending = self.pending.borrow_mut().take();
            if let Some(pending) = pending { self.ack(&pending.command, Err("Связь потеряна во время команды".into())); }
            self.commands.borrow_mut().clear();
        }
        let relinquished = relinquished_session(&self.participating, is_owner, handoff_target);
        if recovery_source || relinquished {
            self.ctx.player.borrow_mut().set_paused(true);
            self.prepared.borrow_mut().take();
        }
        if !lease_lost { self.poll_pending(); }
        if !lease_lost && self.pending.borrow().is_none() {
            let command = self.commands.borrow_mut().pop_front();
            if let Some(command) = command { self.apply_command(command); }
        }
        if self.response.borrow().is_some() { return; }
        let request = self.requests.borrow_mut().pop_front();
        if let Some(request) = request { self.send(request); return; }
        if self.last_poll.get().elapsed() < Duration::from_secs(2) { return; }
        self.last_poll.set(Instant::now());
        if !has_owner && self.last_success.get().is_some() && self.claim_attempt.get() != Some(self.ctx.queue.borrow().revision()) {
            self.claim_attempt.set(Some(self.ctx.queue.borrow().revision()));
            if let Ok(Some(_)) = self.snapshot() {
                self.claim();
                return;
            }
        }
        let snapshot = if is_owner && !recovery_source && self.pending.borrow().is_none() {
            match self.snapshot() { Ok(value) => value, Err(error) => { self.set_error(error); None } }
        } else { None };
        self.send(Request { path: "/api/playback/heartbeat", body: json!({
            "device_id": self.device, "name": "Компьютер HomeMusic", "kind": "desktop",
            "state": snapshot, "revision": self.revision(),
        }) });
    }

    fn send(&self, request: Request) {
        let hub = self.ctx.hub.hub.clone();
        let (tx, rx) = mpsc::sync_channel(1);
        *self.response.borrow_mut() = Some(rx);
        let generation = self.generation.get();
        std::thread::spawn(move || { let _ = tx.send((generation, hub.send("POST", request.path, Some(request.body)))); });
    }

    fn accept_view(&self, view: Value) {
        let first = self.last_success.get().is_none();
        self.last_success.set(Some(Instant::now()));
        if let Some(error) = view["error"].as_str().filter(|s| !s.is_empty()) { self.set_error(error); }
        else { self.set_error(""); }
        let pending_id = self.pending.borrow().as_ref().map(|p| text(&p.command, "id").to_owned());
        if let Some(commands) = view["commands"].as_array() {
            let mut inbox = self.commands.borrow_mut();
            for command in commands.iter().take(32) {
                let id = text(command, "id");
                if pending_id.as_deref() != Some(id) && !inbox.iter().any(|c| text(c, "id") == id) {
                    inbox.push_back(command.clone());
                }
            }
        }
        *self.view.borrow_mut() = view;
        self.paint();
        if self.attach_pending.replace(false) {
            if self.owner().is_empty() || self.owner() == self.device { self.claim(); }
            else { self.transfer(&self.device); }
        } else if first && self.owner().is_empty() && self.ctx.queue.borrow().current().is_some() { self.claim(); }
    }

    fn paint(&self) {
        let Some(tools) = self.tools.upgrade() else { return };
        tools.set_devices_detached(self.detached.get());
        if self.detached.get() {
            tools.set_devices_status("Только здесь: отдельный локальный плеер, без общей сессии и удалённых команд".into());
            tools.set_devices_has_session(false);
            tools.set_devices_busy(false);
            tools.set_devices_track("Продолжайте воспроизведение обычными кнопками плеера".into());
            return;
        }
        let view = self.view.borrow();
        let owner = text(&view, "owner");
        let mut rows = Vec::new();
        if let Some(devices) = view["devices"].as_array() {
            for device in devices {
                let id = text(device, "id");
                let online = device["online"].as_bool().unwrap_or(false);
                let detail = if online {
                    if id == owner { "Владелец общей сессии" } else if id == self.device { "Этот компьютер" } else { "В сети" }.to_owned()
                } else {
                    let age = (epoch() - device["last_seen"].as_f64().unwrap_or(0.0)).max(0.0) as u64;
                    format!("Не в сети · {} мин назад", age / 60)
                };
                rows.push(crate::DeviceRow { id: id.into(), name: text(device, "name").into(), detail: detail.into(), online, owner: id == owner });
            }
        }
        tools.set_devices_rows(slint::ModelRc::new(slint::VecModel::from(rows)));
        let phase = text(&view["handoff"], "phase");
        let busy = matches!(phase, "preparing" | "stopping" | "starting");
        tools.set_devices_busy(busy);
        tools.set_devices_status((match phase {
            "preparing" if view["handoff"]["recovery"].as_bool().unwrap_or(false) => "Прежнее устройство не в сети: восстанавливаем сохранённую общую очередь…",
            "preparing" => "Подготовка целевого устройства…",
            "stopping" => "Целевое устройство готово; останавливаем прежнее…",
            "starting" => "Продолжаем на целевом устройстве…",
            "failed" => "Передача не выполнена. Повторите её или выберите отдельное локальное воспроизведение.",
            _ if owner.is_empty() => "Общая сессия ещё не создана",
            _ if owner == self.device => "Общая очередь играет на этом компьютере",
            _ => "Общий сеанс на другом устройстве. Обычное воспроизведение здесь независимо; «Здесь» перенесёт общую очередь.",
        }).into());
        let snapshot = &view["state"];
        let index = snapshot["index"].as_u64().unwrap_or(0) as usize;
        let track = &snapshot["queue"][index];
        tools.set_devices_has_session(!snapshot.is_null());
        tools.set_devices_track(format!("{} — {}", text(track, "artist"), text(track, "title")).into());
        tools.set_devices_position(snapshot["position"].as_f64().unwrap_or(0.0) as f32);
        tools.set_devices_duration(track["duration_s"].as_f64().unwrap_or(0.0) as f32);
        tools.set_devices_volume(snapshot["volume"].as_f64().unwrap_or(1.0) as f32);
        tools.set_devices_playing(snapshot["playing"].as_bool().unwrap_or(false));
    }

    fn apply_command(&self, command: Value) {
        let id = text(&command, "id").to_owned();
        let cached = self.completed.borrow().iter().find(|(key, _)| *key == id).map(|(_, ack)| ack.clone());
        if let Some(ack) = cached { self.enqueue("/api/playback/ack", ack); return; }
        if !self.command_current(&command) {
            self.ack(&command, Err("Передача уже отменена".into())); return;
        }
        if command["expires_at"].as_f64().unwrap_or(0.0) <= epoch() {
            self.ack(&command, Err("Срок команды истёк".into())); return;
        }
        let kind = text(&command, "kind");
        let result = match kind {
            "prepare" => self.prepare(&command),
            "start" => self.start_prepared(&command),
            "stop" | "pause" => { self.ctx.player.borrow_mut().set_paused(true); Ok(false) }
            "play" => {
                if self.ctx.player.borrow().is_idle() { self.ctx.play_current(); }
                else { self.ctx.player.borrow_mut().set_paused(false); }
                Ok(true)
            }
            "next" => {
                if self.ctx.queue.borrow_mut().next() { self.ctx.play_current(); Ok(true) }
                else { Err("Конец очереди".into()) }
            }
            "prev" => {
                if self.ctx.player.borrow().snapshot().position_s > 3.0 { self.ctx.seek(0.0); }
                else if self.ctx.queue.borrow_mut().prev() { self.ctx.play_current(); }
                else { self.ctx.seek(0.0); }
                Ok(true)
            }
            "seek" => {
                if !self.ctx.player.borrow().snapshot().seekable { Err("Этот поток не поддерживает перемотку".into()) }
                else { self.ctx.seek(command["value"].as_f64().unwrap_or(0.0)); Ok(true) }
            }
            "volume" => { self.ctx.player.borrow_mut().set_volume(command["value"].as_f64().unwrap_or(1.0) as f32); Ok(false) }
            _ => Err("Неизвестная команда".into()),
        };
        match result {
            Err(error) => {
                if matches!(kind, "prepare" | "start") { self.ctx.player.borrow_mut().set_paused(true); }
                self.ack(&command, Err(error));
            }
            Ok(false) => self.ack(&command, Ok(())),
            Ok(true) => {
                let playback = self.ctx.player.borrow().snapshot();
                let playing = if kind == "prepare" { false } else if kind == "start" { command["state"]["playing"].as_bool().unwrap_or(false) } else { playback.playing };
                let position = if matches!(kind, "prepare" | "start") { command["state"]["position"].as_f64().unwrap_or(0.0) }
                    else if kind == "seek" { command["value"].as_f64().unwrap_or(0.0) } else { 0.0 };
                *self.pending.borrow_mut() = Some(Pending { token: self.ctx.queue.borrow().current_token(), command, started: Instant::now(), playing, position });
            }
        }
    }

    fn prepare(&self, command: &Value) -> Result<bool, String> {
        let snapshot = &command["state"];
        let rows = snapshot["queue"].as_array().ok_or("Нет очереди")?;
        let mut items = Vec::with_capacity(rows.len());
        for row in rows {
            let reference = text(row, "ref");
            let id = text(row, "nd_song_id");
            if id.is_empty() && reference.is_empty() { return Err("У трека нет общего ID".into()); }
            items.push(Item { id: if id.is_empty() { reference.strip_prefix("nd:").unwrap_or("").to_owned() } else { id.to_owned() },
                path: String::new(), listen_ref: reference.to_owned(), title: text(row, "title").to_owned(),
                artist: text(row, "artist").to_owned(), album: text(row, "album").to_owned(), meta: "Общая очередь".into(),
                duration_s: row["duration_s"].as_f64().unwrap_or(0.0) as u32 });
        }
        let mut queue = Queue::from_tracks("Общая очередь", items);
        if !queue.select(snapshot["index"].as_u64().unwrap_or(u64::MAX) as usize) { return Err("Нет текущего трека".into()); }
        queue.restore_modes(&json!({"repeat": match text(snapshot, "repeat") { "all" => 1, "one" => 2, _ => 0 },
            "shuffle": if snapshot["shuffle"].as_bool().unwrap_or(false) { 1 } else { 0 }}));
        self.ctx.player.borrow_mut().stop();
        self.ctx.replace_queue(queue, false);
        *self.occurrence.borrow_mut() = (self.ctx.queue.borrow().current_token(), text(snapshot, "occurrence").to_owned());
        self.ctx.player.borrow_mut().set_volume(snapshot["volume"].as_f64().unwrap_or(1.0) as f32);
        self.ctx.play_current_at(snapshot["position"].as_f64().unwrap_or(0.0) as f32, true)?;
        *self.prepared.borrow_mut() = Some((text(command, "handoff_id").to_owned(), self.ctx.queue.borrow().current_token()));
        Ok(true)
    }

    fn start_prepared(&self, command: &Value) -> Result<bool, String> {
        let valid = self.prepared.borrow().as_ref().is_some_and(|(id, token)|
            id == text(command, "handoff_id") && *token == self.ctx.queue.borrow().current_token());
        if !valid { return Err("Подготовленная очередь изменилась; повторите передачу".into()); }
        let snapshot = &command["state"];
        // The source can advance during preparation. Re-prepare the final occurrence paused
        // rather than starting a stale track; poll_pending starts it only after FileLoaded.
        let current = self.snapshot()?.ok_or("Нет подготовленной очереди")?;
        if current["occurrence"] != snapshot["occurrence"] || current["queue"] != snapshot["queue"] || current["index"] != snapshot["index"] {
            self.prepare(command)?;
        }
        self.ctx.queue.borrow_mut().restore_modes(&json!({
            "repeat": match text(snapshot, "repeat") { "all" => 1, "one" => 2, _ => 0 },
            "shuffle": if snapshot["shuffle"].as_bool().unwrap_or(false) { 1 } else { 0 },
        }));
        self.ctx.player.borrow_mut().set_volume(snapshot["volume"].as_f64().unwrap_or(1.0) as f32);
        let position = snapshot["position"].as_f64().unwrap_or(0.0);
        let playback = self.ctx.player.borrow().snapshot();
        if playback.phase != PlaybackPhase::Loading {
            if position > 0.5 && !playback.seekable { return Err("Позицию этого потока нельзя передать".into()); }
            self.ctx.seek(position);
            self.ctx.player.borrow_mut().set_paused(!snapshot["playing"].as_bool().unwrap_or(false));
        }
        Ok(true)
    }

    fn command_current(&self, command: &Value) -> bool {
        let phase = match text(command, "kind") {
            "prepare" => "preparing", "stop" => "stopping", "start" => "starting",
            _ => return self.owner() == self.device,
        };
        let view = self.view.borrow();
        text(&view["handoff"], "id") == text(command, "handoff_id")
            && text(&view["handoff"], "phase") == phase
    }

    fn poll_pending(&self) {
        let pending = self.pending.borrow_mut().take();
        let Some(pending) = pending else { return };
        let command = &pending.command;
        let kind = text(command, "kind");
        if !self.command_current(command) {
            self.ctx.player.borrow_mut().set_paused(true);
            self.ack(command, Err("Передача уже отменена".into()));
            return;
        }
        let playback = self.ctx.player.borrow().snapshot();
        let expired = command["expires_at"].as_f64().unwrap_or(0.0) <= epoch() || pending.started.elapsed() > Duration::from_secs(22);
        let changed = pending.token != self.ctx.queue.borrow().current_token();
        if expired || changed || playback.phase == PlaybackPhase::Error {
            self.ctx.player.borrow_mut().set_paused(true);
            let error = if changed { "Очередь изменилась во время передачи" } else if expired { "Устройство не подготовило звук вовремя" } else { "Не удалось открыть трек" };
            self.ack(command, Err(error.into()));
            return;
        }
        let ready = matches!(playback.phase, PlaybackPhase::Playing | PlaybackPhase::Paused);
        if ready && kind == "start" && playback.playing != pending.playing {
            if pending.position > 0.5 && !playback.seekable {
                self.ctx.player.borrow_mut().set_paused(true);
                self.ack(command, Err("Позицию этого потока нельзя передать".into())); return;
            }
            self.ctx.seek(pending.position);
            self.ctx.player.borrow_mut().set_paused(!pending.playing);
            *self.pending.borrow_mut() = Some(pending); return;
        }
        let position_ready = !matches!(kind, "prepare" | "start" | "seek") || (f64::from(playback.position_s) - pending.position).abs() < 2.0;
        if ready && position_ready && playback.playing == pending.playing {
            self.ack(command, Ok(()));
        } else { *self.pending.borrow_mut() = Some(pending); }
    }

    fn ack(&self, command: &Value, result: Result<(), String>) {
        let mut error = result.err();
        let snapshot = match self.snapshot() {
            Ok(value) => value,
            Err(message) => { error.get_or_insert(message); None }
        };
        if let Some(message) = &error { self.set_error(message); }
        let body = json!({"device_id": self.device, "command_id": command["id"],
            "ok": error.is_none(), "error": error, "state": snapshot});
        let mut completed = self.completed.borrow_mut();
        if completed.len() >= 64 { completed.pop_front(); }
        completed.push_back((text(command, "id").to_owned(), body.clone()));
        drop(completed);
        self.enqueue("/api/playback/ack", body);
    }
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str { value[key].as_str().unwrap_or("") }
fn epoch() -> f64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64() }

// A new process merely observes the previous owner's session. Fence only an
// actual participant losing ownership/preparation, once; never every local Play.
fn relinquished_session(participating: &Cell<bool>, owner: bool, target: bool) -> bool {
    let active = owner || target;
    participating.replace(active) && !active
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn restarted_observer_does_not_pause_local_playback() {
        let participating = Cell::new(false);
        // Repeated heartbeats naming the old process (online or stale) cannot
        // turn the new process's Play action back into Pause.
        for _ in 0..20 {
            assert!(!relinquished_session(&participating, false, false));
        }
    }

    #[test]
    fn completed_handoff_pauses_old_owner_only_once() {
        let participating = Cell::new(false);
        assert!(!relinquished_session(&participating, true, false));
        assert!(relinquished_session(&participating, false, false));
        // A subsequent explicit local Play is independent, not fenced again.
        assert!(!relinquished_session(&participating, false, false));
    }

    #[test]
    fn prepared_target_is_stopped_on_abort_but_not_on_ownership_commit() {
        let participating = Cell::new(false);
        assert!(!relinquished_session(&participating, false, true));
        assert!(relinquished_session(&participating, false, false));
        assert!(!relinquished_session(&participating, false, true));
        assert!(!relinquished_session(&participating, true, false));
        assert!(relinquished_session(&participating, false, false));
    }
}
