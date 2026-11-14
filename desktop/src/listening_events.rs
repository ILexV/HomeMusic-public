//! One playback attempt across native errors, manual departures, and gapless promotion.
use std::collections::VecDeque;
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant};
use serde_json::{json, Value};
use crate::{hub, player::{PlaybackPhase, PlaybackState}, queue::{Item, Queue}, HubState};

struct Attempt {
    token: Option<u64>, id: String, item: Item, context: String, origin: String,
    listened_ms: u64, previous: Option<(Instant, PlaybackState)>, started: bool, terminal: bool,
    checkpoint: Instant,
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
    if let Some(rec) = queue.recommendation(index) { return format!("smart:{}", rec.session); }
    if hub.wave.borrow().on() { return format!("wave:{}", hub.wave.borrow().id); }
    queue.context().to_owned()
}

impl State {
    pub fn failed(&mut self, hub: &HubState) {
        self.emit("error", None, hub);
    }

    pub fn interrupted(&mut self, snapshot: &PlaybackState, hub: &HubState) {
        self.observe(snapshot);
        self.emit("interrupted", Some(snapshot), hub);
    }

    pub fn begin(&mut self, queue: &Queue, hub: &HubState) {
        if self.attempt.as_ref().is_some_and(|a| !a.terminal) { self.emit("interrupted", None, hub); }
        self.attempt = queue.current().map(|item| Attempt {
            token: queue.current_token(), id: hub::event_id(), item: item.clone(),
            context: context(queue, hub, queue.index().unwrap_or(0)),
            origin: if hub.wave.borrow().on() { hub.wave.borrow().origin.clone() }
                else if queue.context().starts_with("mix:") { "mix_page".into() }
                else if queue.context() == "liked" { "liked_page".into() }
                else { "playlist_page".into() },
            listened_ms: 0, previous: None, started: false, terminal: false, checkpoint: Instant::now(),
        });
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
        let snapshot = snapshot.or_else(|| a.previous.as_ref().map(|(_, s)| s));
        let position = snapshot.map_or(0.0, |s| s.position_s);
        let duration = snapshot.map_or(a.item.duration_s as f32, |s| s.duration_s);
        if let Some(mut event) = hub::wave_event(kind, &a.item.id, &a.item.listen_ref, position, duration, "", &a.origin) {
            event["context"] = if a.context.is_empty() { Value::Null } else { json!(a.context) };
            event["launch_id"] = if a.context.is_empty() { Value::Null } else { json!(a.context) };
            event["playback_id"] = json!(a.id);
            event["device_id"] = json!(self.device);
            event["listened_ms"] = json!(a.listened_ms);
            event["position_s"] = json!(position.max(0.0).min(86400.0));
            event["duration_s"] = json!(duration.max(0.0).min(86400.0));
            if a.context.starts_with("wave:") && hub.wave.borrow().on() {
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
        if !self.attempt.as_ref().is_some_and(|a| a.item == *item && !a.terminal) { return; }
        self.observe(snapshot);
        self.emit(departure_kind(snapshot, natural), Some(snapshot), hub);
    }

    pub fn tick(&mut self, snapshot: &PlaybackState, queue: &Queue, hub: &HubState) {
        if queue.current_token().is_some() && self.attempt.as_ref().is_none_or(|a| a.token != queue.current_token()) {
            self.begin(queue, hub);
        }
        self.observe(snapshot);
        if self.attempt.as_ref().is_some_and(|a| !a.terminal) {
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

fn departure_kind(snapshot: &PlaybackState, natural: bool) -> &'static str {
    if snapshot.phase == PlaybackPhase::Error { "error" }
    else if natural || snapshot.duration_s > 0.5 && snapshot.position_s >= snapshot.duration_s * 0.9 { "finish" }
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
    fn snapshot(phase: PlaybackPhase, position_s: f32) -> PlaybackState {
        PlaybackState { phase, position_s, duration_s: 100.0, seekable: true, playing: phase == PlaybackPhase::Playing, volume: 1.0 }
    }
    #[test]
    fn errors_never_become_skips_even_near_end() {
        assert_eq!(departure_kind(&snapshot(PlaybackPhase::Error, 99.0), false), "error");
        assert_eq!(departure_kind(&snapshot(PlaybackPhase::Playing, 10.0), false), "skip");
        assert_eq!(departure_kind(&snapshot(PlaybackPhase::Playing, 99.0), false), "finish");
    }
    #[test]
    fn seeking_and_buffering_do_not_inflate_listening() {
        let before = snapshot(PlaybackPhase::Playing, 5.0);
        assert_eq!(listened_delta(&before, &snapshot(PlaybackPhase::Playing, 90.0), Duration::from_secs(1)), 0);
        assert_eq!(listened_delta(&snapshot(PlaybackPhase::Buffering, 5.0), &snapshot(PlaybackPhase::Playing, 6.0), Duration::from_secs(1)), 0);
        assert_eq!(listened_delta(&before, &snapshot(PlaybackPhase::Playing, 6.0), Duration::from_secs(1)), 1000);
    }
}
