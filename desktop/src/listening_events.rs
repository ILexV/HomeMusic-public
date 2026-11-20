//! One playback attempt across native errors, manual departures, and gapless promotion.
use std::collections::VecDeque;
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant};
use serde_json::{json, Value};
use crate::{hub, player::{PlaybackPhase, PlaybackState}, queue::{Item, Queue, Start}, HubState};

struct Attempt {
    token: Option<u64>, id: String, item: Item, context: String, origin: String,
    start: Start,
    listened_ms: u64, previous: Option<(Instant, PlaybackState)>, started: bool, terminal: bool,
    checkpoint: Instant,
}

impl Attempt {
    fn from_queue(queue: &Queue, wave: &hub::WaveLive, start: Start) -> Option<Self> {
        let index = queue.index()?;
        let context = context_at(queue, wave, index);
        let source = queue.row_source(index).unwrap_or(queue.context());
        let origin = if context.strip_prefix("wave:") == Some(wave.id.as_str()) { wave.origin.as_str() }
            else if source.starts_with("mix:") { "mix_page" }
            else if source.starts_with("playlist:") { "playlist_page" }
            else if source == "liked" { "liked_page" }
            else if matches!(source, "album" | "library" | "search") { source }
            else { "" };
        Some(Self {
            token: queue.current_token(), id: hub::event_id(), item: queue.current()?.clone(),
            start, context, origin: origin.into(), listened_ms: 0, previous: None,
            started: false, terminal: false, checkpoint: Instant::now(),
        })
    }

    fn event(&self, kind: &str, snapshot: Option<&PlaybackState>, device: &str) -> Option<Value> {
        let snapshot = snapshot.or_else(|| self.previous.as_ref().map(|(_, s)| s));
        let position = snapshot.map_or(0.0, |s| s.position_s);
        let duration = snapshot.map_or(self.item.duration_s as f32, |s| s.duration_s);
        let mut event = hub::wave_event(kind, &self.item.id, &self.item.listen_ref, position, duration, "", &self.origin)?;
        event["context"] = if self.context.is_empty() { Value::Null } else { json!(self.context) };
        // Bare contexts identify a surface, not a launch of a list.
        event["launch_id"] = if (self.context == "liked" || self.context.contains(':')) && self.context.len() <= 64 {
            json!(self.context)
        } else { Value::Null };
        if self.origin.is_empty() { event["launch_origin"] = Value::Null; }
        if let Some(trigger) = self.start.trigger() { event["play_trigger"] = json!(trigger); }
        event["playback_id"] = json!(self.id);
        event["device_id"] = json!(device);
        event["listened_ms"] = json!(self.listened_ms);
        event["position_s"] = json!(position.max(0.0).min(86400.0));
        event["duration_s"] = json!(duration.max(0.0).min(86400.0));
        Some(event)
    }
}

pub(crate) struct State {
    attempt: Option<Attempt>, device: String, pending: VecDeque<Value>,
    response: Option<Receiver<(Vec<Value>, Result<Value, String>)>>, retry: Instant,
}
impl Default for State {
    fn default() -> Self {
        Self { attempt: None, device: format!("desktop-{}", hub::event_id()), pending: VecDeque::new(), response: None, retry: Instant::now() }
    }
}

pub(crate) fn context(queue: &Queue, hub: &HubState, index: usize) -> String {
    context_at(queue, &hub.wave.borrow(), index)
}

fn context_at(queue: &Queue, wave: &hub::WaveLive, index: usize) -> String {
    if let Some(rec) = queue.recommendation(index) { return format!("smart:{}", rec.session); }
    if let Some(source) = queue.row_source(index) { return source.to_owned(); }
    if wave.on() { return format!("wave:{}", wave.id); }
    queue.context().to_owned()
}

impl State {
    pub fn attempt_token(&self) -> Option<u64> {
        self.attempt.as_ref().and_then(|attempt| attempt.token)
    }

    pub fn failed(&mut self, hub: &HubState) {
        self.emit("error", None, hub);
    }

    pub fn interrupted(&mut self, snapshot: &PlaybackState, hub: &HubState) {
        self.observe(snapshot);
        self.emit("interrupted", Some(snapshot), hub);
    }

    pub fn begin(&mut self, queue: &Queue, hub: &HubState, start: Start) {
        if self.attempt.as_ref().is_some_and(|a| !a.terminal) { self.emit("interrupted", None, hub); }
        self.attempt = Attempt::from_queue(queue, &hub.wave.borrow(), start);
    }

    fn observe(&mut self, snapshot: &PlaybackState) {
        if let Some(a) = &mut self.attempt {
            let now = Instant::now();
            if let Some((at, previous)) = &a.previous {
                a.listened_ms = (a.listened_ms + listened_delta(previous, snapshot, now.duration_since(*at))).min(604800000);
            }
            a.previous = Some((now, snapshot.clone()));
        }
    }

    fn emit(&mut self, kind: &str, snapshot: Option<&PlaybackState>, hub: &HubState) {
        let Some(a) = &mut self.attempt else { return };
        if a.terminal { return; }
        if let Some(event) = a.event(kind, snapshot, &self.device) {
            if a.context.strip_prefix("wave:").is_some_and(|id| !id.is_empty() && id == hub.wave.borrow().id) {
                hub.wave.borrow_mut().pending.push(event.clone());
            } else if a.context.starts_with("smart:") {
                if let Some(modes) = hub.modes.borrow().upgrade() { modes.queue_event(event.clone()); }
            }
            self.pending.push_back(event);
        }
        if matches!(kind, "skip" | "finish" | "error" | "interrupted") { a.terminal = true; }
        if kind == "start" { a.started = true; }
        a.checkpoint = Instant::now();
    }

    pub fn depart(&mut self, snapshot: &PlaybackState, item: &Item, hub: &HubState, natural: bool) {
        let Some(a) = &self.attempt else { return };
        if a.item != *item || a.terminal { return; }
        self.observe(snapshot);
        let listened_ms = self.attempt.as_ref().unwrap().listened_ms;
        self.emit(departure_kind(snapshot, natural, listened_ms), Some(snapshot), hub);
    }

    pub fn tick(&mut self, snapshot: &PlaybackState, queue: &Queue, hub: &HubState) {
        // Selecting/undoing/enqueuing a row without loading it is not a playback attempt.
        if self.attempt.as_ref().is_some_and(|a| !a.terminal && a.token == queue.current_token()) {
            self.observe(snapshot);
            if snapshot.phase == PlaybackPhase::Error { self.emit("error", Some(snapshot), hub); }
            else if snapshot.phase == PlaybackPhase::Stopped { self.emit("interrupted", Some(snapshot), hub); }
            else if snapshot.phase == PlaybackPhase::Playing {
                if self.attempt.as_ref().is_some_and(|a| !a.started) { self.emit("start", Some(snapshot), hub); }
                else if self.attempt.as_ref().is_some_and(|a| a.checkpoint.elapsed() >= Duration::from_secs(15)) { self.emit("progress", Some(snapshot), hub); }
            }
        }
        if let Some(rx) = &self.response {
            match rx.try_recv() {
                Ok((events, result)) => {
                    self.response = None;
                    match result {
                        Ok(value) if value["rejected"].as_array().is_none_or(|r| r.is_empty()) => {},
                        Ok(_) => { *hub.hub_note.borrow_mut() = "Хаб отклонил часть событий прослушивания".into(); },
                        Err(error) => {
                            for event in events.into_iter().rev() { self.pending.push_front(event); }
                            *hub.hub_note.borrow_mut() = format!("События прослушивания не отправлены: {error}");
                            self.retry = Instant::now() + Duration::from_secs(15);
                        }
                    }
                }
                Err(mpsc::TryRecvError::Empty) => {},
                Err(mpsc::TryRecvError::Disconnected) => {
                    self.response = None;
                    *hub.hub_note.borrow_mut() = "Отправка событий прослушивания прервана".into();
                }
            }
        }
        if self.response.is_none() && !self.pending.is_empty() && Instant::now() >= self.retry {
            let events: Vec<_> = self.pending.drain(..self.pending.len().min(500)).collect();
            let client = hub.hub.clone();
            let (tx, rx) = mpsc::channel();
            self.response = Some(rx);
            std::thread::spawn(move || {
                let result = client.send("POST", "/api/plays/events", Some(json!({"events": events})));
                let _ = tx.send((events, result));
            });
        }
    }
}

fn departure_kind(snapshot: &PlaybackState, natural: bool, listened_ms: u64) -> &'static str {
    if snapshot.phase == PlaybackPhase::Error { "error" }
    else if natural || snapshot.duration_s > 0.5 && listened_ms as f64 >= f64::from(snapshot.duration_s) * 900.0 { "finish" }
    else { "skip" }
}
fn listened_delta(previous: &PlaybackState, current: &PlaybackState, elapsed: Duration) -> u64 {
    if previous.phase != PlaybackPhase::Playing { return 0; }
    let advanced = current.position_s - previous.position_s;
    let wall = elapsed.as_secs_f32();
    // A seek is not listening. Native end/error may reset position; never invent the tail.
    if !advanced.is_finite() || advanced < 0.0 || advanced > wall + 1.0 { return 0; }
    (advanced.min(wall).max(0.0) * 1000.0) as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn play_trigger_follows_attempt_not_row_or_surrounding_queue() {
        let track = |id: &str| Item { id: id.into(), ..Item::file("", "song") };
        let wave = hub::WaveLive { id: "s".into(), origin: "home_card".into(), ..Default::default() };
        let mut queue = Queue::from_tracks("wave", vec![track("first"), track("next")]);
        queue.set_context("wave:s");
        queue.select(0);
        queue.play_inserted(track("searched"), "search");
        let selected = Attempt::from_queue(&queue, &wave, Start::Manual).unwrap();
        for kind in ["start", "progress", "finish", "skip", "interrupted", "error"] {
            let event = selected.event(kind, None, "device").unwrap();
            assert_eq!(event["context"], "search");
            assert_eq!(event["play_trigger"], "manual");
            assert_eq!(event["playback_id"], selected.id);
        }

        // Disk restoration creates a new attempt: row provenance survives, intent does not.
        let mut restored = Queue::from_tracks("wave", queue.items().to_vec());
        restored.select(queue.index().unwrap());
        restored.restore_modes(&queue.modes_snapshot());
        let resume = Attempt::from_queue(&restored, &wave, Start::Unknown).unwrap();
        let payload = resume.event("start", None, "device").unwrap();
        assert_eq!(payload["context"], "search");
        assert!(payload.get("play_trigger").is_none());
        assert_ne!(resume.id, selected.id);

        restored.set_repeat(crate::queue::Repeat::One);
        let start = restored.advance().unwrap();
        let replay = Attempt::from_queue(&restored, &wave, start).unwrap();
        assert_eq!(replay.event("start", None, "device").unwrap()["play_trigger"], "repeat");
        assert_eq!(replay.context, "search");
        restored.set_repeat(crate::queue::Repeat::Off);
        let start = restored.advance().unwrap();
        let next = Attempt::from_queue(&restored, &wave, start).unwrap();
        let event = next.event("error", None, "device").unwrap();
        assert_eq!(event["play_trigger"], "auto");
        assert_eq!(event["context"], "wave:s");
        assert_eq!(event["launch_origin"], "home_card");
    }
    fn snapshot(phase: PlaybackPhase, position_s: f32) -> PlaybackState {
        PlaybackState { phase, position_s, duration_s: 100.0, seekable: true, playing: phase == PlaybackPhase::Playing, volume: 1.0 }
    }
    #[test]
    fn departures_use_heard_time_not_seek_position() {
        assert_eq!(departure_kind(&snapshot(PlaybackPhase::Error, 99.0), false, 99_000), "error");
        assert_eq!(departure_kind(&snapshot(PlaybackPhase::Playing, 99.0), false, 10_000), "skip");
        assert_eq!(departure_kind(&snapshot(PlaybackPhase::Playing, 99.0), false, 90_000), "finish");
        assert_eq!(departure_kind(&snapshot(PlaybackPhase::Playing, 100.0), true, 28_000), "finish");
    }
    #[test]
    fn seeking_and_buffering_do_not_inflate_listening() {
        let before = snapshot(PlaybackPhase::Playing, 5.0);
        assert_eq!(listened_delta(&before, &snapshot(PlaybackPhase::Playing, 90.0), Duration::from_secs(1)), 0);
        assert_eq!(listened_delta(&snapshot(PlaybackPhase::Buffering, 5.0), &snapshot(PlaybackPhase::Playing, 6.0), Duration::from_secs(1)), 0);
        assert_eq!(listened_delta(&before, &snapshot(PlaybackPhase::Playing, 6.0), Duration::from_secs(1)), 1000);
    }
}
