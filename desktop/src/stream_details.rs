//! Source metadata is resolved off the UI thread; codec information comes from the playing decoder.
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::{mpsc, Arc};
use std::time::{Duration, Instant};

use serde_json::Value;
use slint::ComponentHandle;

use crate::feature_context::Context;

#[derive(Clone, Default)]
struct Details {
    source: String,
    kind: String,
    download: String,
    message: String,
}

struct Reply {
    token: Option<u64>,
    details: Details,
}

pub(crate) struct State {
    ctx: Context,
    tools: slint::Weak<crate::ToolsWindow>,
    timer: slint::Timer,
    token: Cell<Option<u64>>,
    local: Cell<bool>,
    full_only: Cell<bool>,
    pending: Cell<bool>,
    refreshed: Cell<Option<Instant>>,
    details: RefCell<Details>,
    technical: RefCell<String>,
    sender: mpsc::SyncSender<Reply>,
    receiver: mpsc::Receiver<Reply>,
}

pub(crate) fn bind(ctx: &Context, tools: &crate::ToolsWindow) -> Rc<State> {
    let (sender, receiver) = mpsc::sync_channel(1);
    let state = Rc::new(State {
        ctx: ctx.clone(), tools: tools.as_weak(), timer: slint::Timer::default(),
        token: Cell::new(None), pending: Cell::new(false), refreshed: Cell::new(None),
        local: Cell::new(false),
        full_only: Cell::new(false),
        details: RefCell::new(Details::default()), sender, receiver,
        technical: RefCell::new(String::with_capacity(96)),
    });
    let weak = Rc::downgrade(&state);
    tools.on_info_refresh(move || {
        if let Some(state) = weak.upgrade() { state.refreshed.set(None); }
    });
    let weak = Rc::downgrade(&state);
    state.timer.start(slint::TimerMode::Repeated, Duration::from_secs(1), move || {
        if let Some(state) = weak.upgrade() { state.tick(); }
    });
    state.tick();
    state
}

impl State {
    fn tick(&self) {
        let Some(tools) = self.tools.upgrade() else { return; };
        let token = self.ctx.queue.borrow().current_token();
        if self.token.replace(token) != token {
            self.refreshed.set(None);
            *self.details.borrow_mut() = Details::default();
            self.technical.borrow_mut().clear();
            let queue = self.ctx.queue.borrow();
            self.local.set(queue.current().is_some_and(|item| self.ctx.hub.offline.local_path(item).is_some()
                || !item.path.is_empty() && !item.path.starts_with("http://") && !item.path.starts_with("https://")));
            self.full_only.set(self.ctx.hub.wave.borrow().on() || queue.index().and_then(|index| queue.recommendation(index)).is_some());
            tools.set_info_title(queue.current().map(|item| format!("{} — {}", item.artist, item.title)).unwrap_or_default().into());
            tools.set_info_source("Определяется…".into());
            tools.set_info_kind("".into());
            tools.set_info_download("".into());
            tools.set_info_message("".into());
        }
        while let Ok(reply) = self.receiver.try_recv() {
            self.pending.set(false);
            if reply.token == token {
                tools.set_info_source(reply.details.source.as_str().into());
                tools.set_info_kind(reply.details.kind.as_str().into());
                tools.set_info_download(reply.details.download.as_str().into());
                tools.set_info_message(reply.details.message.as_str().into());
                *self.details.borrow_mut() = reply.details;
                self.refreshed.set(Some(Instant::now()));
            }
        }
        if token.is_none() { return; }
        let format = self.ctx.player.borrow().stream_details();
        use std::fmt::Write;
        let mut technical = self.technical.borrow_mut();
        if !format.codec.is_empty() || format.bitrate_kbps.is_some() || format.sample_rate.is_some() {
            technical.clear();
            technical.push_str(&format.codec);
            if let Some(rate) = format.bitrate_kbps { let _ = write!(technical, " · {rate} кбит/с"); }
            if let Some(rate) = format.sample_rate { let _ = write!(technical, " · {:.1} кГц", rate as f64 / 1000.0); }
            if let Some(channels) = format.channels { let _ = write!(technical, " · {channels}"); }
        }
        let technical = technical.as_str();
        let format_label = if technical.is_empty() { "Ожидание данных декодера" } else { technical };
        if tools.get_info_format().as_str() != format_label {
            tools.set_info_format(format_label.into());
        }
        let details = self.details.borrow();
        if let Some(ui) = self.ctx.ui.upgrade() {
            if !details.kind.is_empty() {
                let label = format!("{} · {}{}{}", details.kind, details.source,
                    if technical.is_empty() { "" } else { " · " }, technical);
                if ui.get_track_meta().as_str() != label { ui.set_track_meta(label.into()); }
            }
        }
        drop(details);
        let due = self.refreshed.get().is_none_or(|at| at.elapsed() >= Duration::from_secs(10)
            && tools.window().is_visible() && tools.get_active_panel() == 2);
        if !due || self.pending.get() { return; }
        let Some(item) = self.ctx.queue.borrow().current().cloned() else { return; };
        let local = self.local.get();
        let full_only = self.full_only.get();
        let previous = (!self.details.borrow().kind.is_empty()).then(|| self.details.borrow().clone());
        let hub = Arc::clone(&self.ctx.hub.hub);
        let sender = self.sender.clone();
        self.pending.set(true);
        std::thread::spawn(move || {
            let details = resolve(&hub, &item, local, full_only, previous);
            let _ = sender.send(Reply { token, details });
        });
    }
}

fn resolve(hub: &crate::hub::Hub, item: &crate::queue::Item, local: bool, full_only: bool, previous: Option<Details>) -> Details {
    if local {
        return Details { source: "Этот компьютер".into(), kind: "Локальный файл".into(),
            download: "Доступен без сети".into(), message: String::new() };
    }
    if !item.id.is_empty() {
        let source = hub.send("GET", &format!("/api/library/track-source?nd_song_id={}", encode(&item.id)), None)
            .ok().and_then(|body| body.get("source").and_then(Value::as_str).map(str::to_owned));
        return Details { source: source.map(|s| provider(&s)).unwrap_or_else(|| "Navidrome".into()),
            kind: "Библиотека".into(), download: "В серверной библиотеке; это не офлайн-копия".into(), message: String::new() };
    }
    if !item.path.is_empty() {
        return Details { source: "Внешний URL".into(), kind: "Прямой поток".into(),
            download: "Не добавлен в библиотеку".into(), message: String::new() };
    }
    let reference = encode(&item.listen_ref);
    let mut details = if let Some(previous) = previous { previous } else { match hub.send("GET", &format!("/api/listen?ref={reference}&full={full_only}"), None) {
        Ok(body) => {
            let kind = body.get("kind").and_then(Value::as_str).unwrap_or("");
            Details {
                source: provider(body.get("provider").and_then(Value::as_str)
                    .unwrap_or(if kind == "preview" { "deezer" } else if kind == "library" { "Navidrome" } else { "Источник не указан" })),
                kind: match kind { "full" => "Полная запись", "preview" => "Превью, до 30 секунд", "library" => "Библиотека", _ => "Неизвестный тип записи" }.into(),
                ..Details::default()
            }
        }
        Err(message) => Details { source: "Данные источника недоступны".into(), message, ..Details::default() },
    }};
    details.download = match hub.send("GET", &format!("/api/downloads?ref={reference}&limit=1"), None) {
        Ok(body) => body.as_array().and_then(|rows| rows.first()).map(download_label)
            .unwrap_or_else(|| "Нет задания загрузки на сервер".into()),
        Err(_) => "Не удалось получить состояние загрузки".into(),
    };
    details
}

fn provider(name: &str) -> String {
    match name { "hitmoz" => "Hitmo", "hitmos_fm" => "Hitmos.fm", "ytdlp" => "YouTube / yt-dlp",
        "deezer" => "Deezer", "upload" => "Загруженный файл", "manual" => "Импортированный файл", other => other }.into()
}

fn download_label(row: &Value) -> String {
    let status = row.get("status").and_then(Value::as_str).unwrap_or("");
    match status {
        "done" => "Загружен на сервер".into(), "queued" => "В очереди загрузки на сервер".into(),
        "failed" | "needs_review" => "Загрузка требует внимания в веб-версии".into(),
        "cancelled" => "Загрузка отменена".into(),
        "downloading" => format!("Загрузка на сервер: {:.0}%", row.get("progress").and_then(Value::as_f64).unwrap_or(0.0).clamp(0.0, 1.0) * 100.0),
        "searching" => "Подбор источника для загрузки".into(),
        "tagging" | "importing" => "Добавляется в серверную библиотеку".into(),
        other => format!("Загрузка на сервер: {other}"),
    }
}

fn encode(value: &str) -> String {
    value.bytes().fold(String::new(), |mut out, byte| {
        use std::fmt::Write;
        if byte.is_ascii_alphanumeric() || b"-_.~".contains(&byte) { out.push(byte as char); }
        else { let _ = write!(out, "%{byte:02X}"); }
        out
    })
}
