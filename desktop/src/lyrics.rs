//! Lyrics are fetched once per playback occurrence, never from the playhead timer.
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::time::Duration;

use serde_json::{json, Value};
use slint::{ComponentHandle, ModelRc, Timer, TimerMode, VecModel};

use crate::feature_context::Context;
use crate::{LyricLine, ToolsWindow};

#[derive(Debug)]
struct Line {
    text: String,
    time_s: Option<f64>,
}

struct Reply {
    status: String,
    lines: Vec<Line>,
}

impl Reply {
    fn parse(value: Value) -> Result<Self, String> {
        let invalid = || "Неверный ответ сервиса текстов".to_string();
        let status = value["status"].as_str().ok_or_else(invalid)?.to_string();
        let rows = value["lines"].as_array().ok_or_else(invalid)?;
        if rows.len() > 3000 { return Err(invalid()); }
        let mut lines = Vec::with_capacity(rows.len());
        let mut text_size = 0;
        for row in rows {
            let text = row["text"].as_str().ok_or_else(invalid)?;
            text_size += text.len();
            if text_size > 512_000 { return Err(invalid()); }
            let time_s = if row["time_s"].is_null() { None }
                else { Some(row["time_s"].as_f64().ok_or_else(invalid)?) };
            lines.push(Line { text: text.to_string(), time_s });
        }
        Ok(Self { status, lines })
    }
}

type ResultEvent = (u64, Result<Reply, String>);

pub(crate) struct State {
    ctx: Context,
    tools: slint::Weak<ToolsWindow>,
    timer: Timer,
    token: Cell<Option<u64>>,
    generation: Cell<u64>,
    attempted: Cell<bool>,
    inflight: Cell<bool>,
    lines: RefCell<Vec<Line>>,
    sender: SyncSender<ResultEvent>,
    receiver: Receiver<ResultEvent>,
}

pub(crate) fn bind(ctx: &Context, tools: &ToolsWindow) -> Rc<State> {
    let (sender, receiver) = mpsc::sync_channel(1);
    let state = Rc::new(State {
        ctx: ctx.clone(), tools: tools.as_weak(), timer: Timer::default(),
        token: Cell::new(None), generation: Cell::new(0), attempted: Cell::new(true),
        inflight: Cell::new(false), lines: RefCell::new(Vec::new()), sender, receiver,
    });
    let weak = Rc::downgrade(&state);
    tools.on_lyrics_seek(move |index| {
        let Some(state) = weak.upgrade() else { return };
        if state.token.get() != state.ctx.queue.borrow().current_token() || index < 0 {
            return;
        }
        let position = state.lines.borrow().get(index as usize).and_then(|line| line.time_s);
        if let Some(seconds) = position {
            state.ctx.seek(seconds);
            state.tick();
        }
    });
    let weak = Rc::downgrade(&state);
    tools.on_lyrics_search(move || {
        let Some(state) = weak.upgrade() else { return };
        let Some(tools) = state.tools.upgrade() else { return };
        if state.token.get() != state.ctx.queue.borrow().current_token() || !tools.get_lyrics_local() {
            return;
        }
        state.invalidate(&tools);
        state.tick();
    });
    let weak = Rc::downgrade(&state);
    state.timer.start(TimerMode::Repeated, Duration::from_millis(100), move || {
        if let Some(state) = weak.upgrade() { state.tick(); }
    });
    tools.set_lyrics_active(-1);
    tools.set_lyrics_note("Выберите трек".into());
    state.tick();
    state
}

impl State {
    fn invalidate(&self, tools: &ToolsWindow) {
        self.generation.set(self.generation.get().wrapping_add(1));
        self.attempted.set(false);
        self.lines.borrow_mut().clear();
        tools.set_lyrics_lines(ModelRc::new(VecModel::<LyricLine>::default()));
        tools.set_lyrics_active(-1);
        tools.set_lyrics_note("Ищем текст в LRCLIB…".into());
    }

    fn tick(&self) {
        let Some(tools) = self.tools.upgrade() else { return };
        let token = self.ctx.queue.borrow().current_token();
        if token != self.token.get() {
            self.token.set(token);
            self.invalidate(&tools);
            let queue = self.ctx.queue.borrow();
            let item = queue.current();
            tools.set_lyrics_title(item.map(|item| format!("{} — {}", item.artist, item.title)).unwrap_or_default().into());
            tools.set_lyrics_local(item.is_some_and(|item| item.id.is_empty() && item.listen_ref.is_empty()));
            tools.set_lyrics_query_title(item.map(|item| item.title.clone()).unwrap_or_default().into());
            tools.set_lyrics_query_artist(item.map(|item| item.artist.clone()).unwrap_or_default().into());
            tools.set_lyrics_query_album(item.map(|item| item.album.clone()).unwrap_or_default().into());
        }
        while let Ok((generation, reply)) = self.receiver.try_recv() {
            self.inflight.set(false);
            if generation != self.generation.get() { continue; }
            match reply {
                Ok(reply) => self.apply(&tools, reply),
                Err(_) => tools.set_lyrics_note("Текст недоступен: проверьте подключение к Hub".into()),
            }
        }
        if !tools.window().is_visible() || tools.get_active_panel() != 4 { return; }
        if !self.attempted.get() && !self.inflight.get() {
            self.fetch(&tools);
        }
        let playback = self.ctx.player.borrow().playback_position();
        tools.set_lyrics_seekable(playback.is_some_and(|(_, seekable)| seekable));
        let active = playback.and_then(|(position, _)| current_line(&self.lines.borrow(), position))
            .map(|index| index as i32).unwrap_or(-1);
        if tools.get_lyrics_active() != active { tools.set_lyrics_active(active); }
    }

    fn fetch(&self, tools: &ToolsWindow) {
        self.attempted.set(true);
        let queue = self.ctx.queue.borrow();
        let Some(item) = queue.current() else {
            tools.set_lyrics_note("Выберите трек".into());
            return;
        };
        let body = if !item.listen_ref.is_empty() {
            json!({"ref": item.listen_ref})
        } else if !item.id.is_empty() {
            json!({"nd_song_id": item.id})
        } else {
            let title = tools.get_lyrics_query_title().to_string();
            let artist = tools.get_lyrics_query_artist().to_string();
            if title.trim().is_empty() || artist.trim().is_empty() {
                tools.set_lyrics_note("Для локального файла укажите исполнителя и название".into());
                return;
            }
            let duration = if item.duration_s > 0 { f64::from(item.duration_s) }
                else { f64::from(self.ctx.player.borrow().snapshot().duration_s) };
            json!({"title": title, "artist": artist, "album": tools.get_lyrics_query_album().as_str(),
                "duration_s": if duration.is_finite() && duration > 0.0 { Some(duration) } else { None }})
        };
        // Only one worker can be alive. A newer occurrence waits for this bounded
        // request, then replaces it; old results never reach the model or seek handler.
        self.inflight.set(true);
        let hub = self.ctx.hub.hub.clone();
        let sender = self.sender.clone();
        let generation = self.generation.get();
        std::thread::spawn(move || {
            let reply = hub.send("POST", "/api/lyrics", Some(body))
                .and_then(Reply::parse);
            let _ = sender.send((generation, reply));
        });
    }

    fn apply(&self, tools: &ToolsWindow, mut reply: Reply) {
        let synced = reply.status == "synced";
        let valid = reply.lines.len() <= 3000 && (!synced || (
            reply.lines.iter().all(|line| line.time_s.is_some_and(|time| time.is_finite() && time >= 0.0))
            && reply.lines.windows(2).all(|pair| pair[0].time_s < pair[1].time_s)
        ));
        if !valid {
            tools.set_lyrics_note("Текст недоступен: неверные временные метки".into());
            return;
        }
        if !synced { for line in &mut reply.lines { line.time_s = None; } }
        let note = match reply.status.as_str() {
            "synced" => "LRCLIB · нажмите на строку, чтобы перейти к ней",
            "plain" => "LRCLIB · текст без синхронизации",
            "instrumental" => "Инструментальная композиция — без слов",
            _ => "Текст подходящей версии не найден в LRCLIB",
        };
        let rows = reply.lines.iter().map(|line| LyricLine {
            text: line.text.as_str().into(), timed: line.time_s.is_some(),
        }).collect::<Vec<_>>();
        *self.lines.borrow_mut() = reply.lines;
        tools.set_lyrics_lines(ModelRc::new(VecModel::from(rows)));
        tools.set_lyrics_note(note.into());
    }
}

fn current_line(lines: &[Line], position: f64) -> Option<usize> {
    if !position.is_finite() || lines.first()?.time_s.is_none() { return None; }
    lines.partition_point(|line| line.time_s.is_some_and(|time| time <= position)).checked_sub(1)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn timed(times: &[f64]) -> Vec<Line> {
        times.iter().map(|time| Line { text: String::new(), time_s: Some(*time) }).collect()
    }

    #[test]
    fn highlight_follows_exact_boundaries_and_backward_seek() {
        let lines = timed(&[2.25, 5.0, 12.125]);
        for (position, expected) in [(0.0, None), (2.249, None), (2.25, Some(0)),
            (5.0, Some(1)), (100.0, Some(2)), (3.0, Some(0)), (1.0, None)] {
            assert_eq!(current_line(&lines, position), expected);
        }
    }

    #[test]
    fn plain_empty_and_invalid_playheads_never_highlight() {
        assert_eq!(current_line(&[], 4.0), None);
        assert_eq!(current_line(&[Line { text: "Untimed".into(), time_s: None }], 4.0), None);
        assert_eq!(current_line(&timed(&[0.0]), f64::NAN), None);
        assert_eq!(current_line(&timed(&[0.0]), -1.0), None);
    }
}
