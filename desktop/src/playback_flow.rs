//! Bridges the queue's occurrence identity and mpv's prepared media without replaying promotions.
use crate::{feature_context::Context, player::{PlaybackPhase, PlaybackState}};
use crate::queue::{Queue, Repeat, Start};
use slint::ComponentHandle;
use std::collections::HashSet;

/// A failed run follows row IDs (not track IDs or positions) across edits.
/// An explicit selection or a natural boundary starts a new run.
#[derive(Default)]
struct FailedRun {
    expected: Option<u64>,
    rows: HashSet<u64>,
}

impl FailedRun {
    fn clear(&mut self) {
        self.expected = None;
        self.rows.clear();
    }

    // None: stale event; Some(false): this attempt failed with no successor.
    fn advance(&mut self, queue: &mut Queue, attempt: Option<u64>, notice: &str) -> Option<bool> {
        let index = queue.index()?;
        if attempt.is_none() || attempt != queue.current_token() { return None; }
        queue.set_playback_error(index, Some(notice));
        if self.expected != attempt { self.clear(); }
        self.rows.insert(queue.row_token(index).unwrap());
        let end = if queue.repeat() == Repeat::All { index } else { 0 };
        let next = (index + 1..queue.items().len()).chain(0..end)
            .find(|&i| !self.rows.contains(&queue.row_token(i).unwrap()));
        if let Some(next) = next {
            queue.select(next);
            self.expected = queue.current_token();
            Some(true)
        } else {
            self.clear();
            Some(false)
        }
    }
}

struct Prepared {
    revision: u64,
    current: Option<u64>,
    next: u64,
    target: String,
    cookie: String,
    key: String,
    same_album: bool,
}

#[derive(Default)]
pub(crate) struct State {
    prepared: Option<Prepared>,
    failures: FailedRun,
    observation: Option<(Option<u64>, PlaybackState)>,
    loaded_occurrence: Option<u64>,
}

impl State {
    pub fn tick(&mut self, ctx: &Context) {
        // Drain native boundary events before changing the native playlist.
        let (ended, promoted) = {
            let mut player = ctx.player.borrow_mut();
            (player.take_natural_end(), player.take_promoted())
        };
        if ended || promoted.is_some() {
            self.failures.clear();
            let adopted = self.prepared.as_ref().is_some_and(|prepared| {
                let queue = ctx.queue.borrow();
                promoted.as_deref() == Some(prepared.key.as_str())
                    && queue.current_token() == prepared.current
                    && queue.peek_next_token() == Some(prepared.next)
            });
            {
                let queue = ctx.queue.borrow();
                if let Some(item) = queue.current() {
                    let snapshot = self.observation.as_ref()
                        .filter(|(token, _)| *token == queue.current_token())
                        .map(|(_, snapshot)| snapshot.clone())
                        .unwrap_or_else(|| ctx.player.borrow().snapshot());
                    crate::note_departure(&snapshot, item, &ctx.hub, true);
                }
            }
            let start = ctx.queue.borrow_mut().advance();
            crate::maybe_extend_wave(&ctx.hub, &ctx.queue.borrow());
            self.prepared = None;
            if let Some(start) = start {
                if adopted {
                    {
                        let mut queue = ctx.queue.borrow_mut();
                        if let Some(index) = queue.index() { queue.set_playback_error(index, None); }
                    }
                    ctx.hub.listening.borrow_mut().begin(&ctx.queue.borrow(), &ctx.hub, start);
                    self.refresh_promoted(ctx);
                } else {
                    ctx.play_current(start);
                }
            } else if promoted.is_some() {
                // The user removed the predicted successor before native promotion was observed.
                ctx.player.borrow_mut().stop();
            }
        }
        // Handle promotion first: the appended entry can fail in the same batch as
        // the previous entry's EOF. Reloading a stale prediction clears its failure.
        if ctx.player.borrow_mut().take_failed_media() {
            let attempt = ctx.hub.listening.borrow().attempt_token();
            let advanced = {
                let player = ctx.player.borrow();
                let notice = player.notice().unwrap_or("Не удалось воспроизвести трек");
                self.failures.advance(&mut ctx.queue.borrow_mut(), attempt, notice)
            };
            if let Some(advanced) = advanced {
                ctx.hub.listening.borrow_mut().failed(&ctx.hub);
                if advanced {
                    self.prepared = None;
                    ctx.play_current(Start::Auto);
                } else {
                    ctx.refresh_queue();
                }
            }
        }
        self.prepare(ctx);
        let queue = ctx.queue.borrow();
        let token = queue.current_token();
        let snapshot = ctx.player.borrow().snapshot();
        if self.starts_playing(token, ctx.hub.listening.borrow().attempt_token(), snapshot.phase) {
            if let Some(ui) = ctx.ui.upgrade().filter(|ui| {
                ui.get_visualizer_expanded() && ui.window().is_visible() && !ui.window().is_minimized()
            }) {
                if let Some(item) = queue.current() {
                    ui.invoke_show_track_splash(item.title.trim().into(), item.artist.trim().into());
                }
            }
        }
        self.observation = Some((token, snapshot));
    }

    // Observe loaded occurrences even while collapsed. A paused restore is a
    // baseline, not a new track the next time the listener presses Play.
    fn starts_playing(&mut self, current: Option<u64>, attempt: Option<u64>, phase: PlaybackPhase) -> bool {
        if current.is_none() || current == self.loaded_occurrence || current != attempt
            || !matches!(phase, PlaybackPhase::Playing | PlaybackPhase::Paused) {
            return false;
        }
        self.loaded_occurrence = current;
        phase == PlaybackPhase::Playing
    }

    fn refresh_promoted(&self, ctx: &Context) {
        let Some(ui) = ctx.ui.upgrade() else { return; };
        let queue = ctx.queue.borrow();
        ctx.hub.offline.protect_current(queue.current());
        crate::show_queue(&ui, &queue, &ctx.hub);
        let target = crate::current_target(&queue, &ctx.config, &ctx.hub);
        ui.set_wave(crate::empty_wave());
        match target {
            Some((target, cookie)) => ctx.waves.load(Some(target), &cookie),
            None => ctx.waves.load(None, ""),
        }
        crate::show_cover(&ui, &ctx.hub, queue.current().map(|item| item.id.as_str()).unwrap_or(""));
        ui.set_error("".into());
    }

    fn prepare(&mut self, ctx: &Context) {
        let snapshot = ctx.player.borrow().snapshot();
        if matches!(snapshot.phase, crate::player::PlaybackPhase::Idle | crate::player::PlaybackPhase::Stopped
            | crate::player::PlaybackPhase::Error | crate::player::PlaybackPhase::Ended) {
            ctx.player.borrow_mut().cancel_preload();
            self.prepared = None;
            return;
        }
        let queue = ctx.queue.borrow();
        let Some(next) = queue.peek_next_token() else {
            ctx.player.borrow_mut().cancel_preload();
            self.prepared = None;
            return;
        };
        let current = queue.current_token();
        let changed = self.prepared.as_ref().is_none_or(|prepared|
            prepared.revision != queue.revision() || prepared.current != current || prepared.next != next);
        if changed {
            let Some(item) = queue.peek_next() else { return; };
            let full_only = ctx.hub.wave.borrow().on() || queue.context().starts_with("mix:") || queue.row_index(next).and_then(|index| queue.recommendation(index)).is_some();
            let Some((target, cookie)) = crate::item_target(item, &ctx.config, &ctx.hub, full_only) else { return; };
            let same_album = queue.current().is_some_and(|current| !current.album.is_empty()
                && current.album == item.album && !current.artist.is_empty() && current.artist == item.artist);
            self.prepared = Some(Prepared { revision: queue.revision(), current, next,
                key: format!("{}:{next}", current.unwrap_or(0)), target, cookie, same_album });
        }
        if let Some(prepared) = self.prepared.as_ref() {
            // Idempotent in Player; still re-prepares after crossfade settings invalidated its deck.
            let _ = ctx.player.borrow_mut().prepare_next(&prepared.target, &prepared.cookie, &prepared.key, prepared.same_album);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::queue::Item;

    fn queue(repeat: Repeat, current: usize) -> Queue {
        let mut queue = Queue::from_tracks("test", vec![Item::file("duplicate", "duplicate"); 3]);
        queue.select(current);
        queue.set_repeat(repeat);
        queue
    }

    #[test]
    fn splash_follows_loaded_occurrences_not_labels_or_refreshes() {
        let mut flow = State::default();
        let mut q = queue(Repeat::Off, 0); // all three rows have identical metadata
        let a = q.current_token();
        assert!(!flow.starts_playing(a, a, PlaybackPhase::Paused));
        assert!(!flow.starts_playing(a, a, PlaybackPhase::Playing));
        assert_eq!(q.advance(), Some(Start::Auto));
        let b = q.current_token();
        assert!(!flow.starts_playing(b, a, PlaybackPhase::Playing));
        assert!(!flow.starts_playing(b, b, PlaybackPhase::Loading));
        assert!(flow.starts_playing(b, b, PlaybackPhase::Playing));
        assert!(!flow.starts_playing(b, b, PlaybackPhase::Playing));
        assert!(q.next());
        let c = q.current_token();
        assert!(!flow.starts_playing(c, c, PlaybackPhase::Error));
        assert!(flow.starts_playing(c, c, PlaybackPhase::Playing));
    }

    fn fail(run: &mut FailedRun, queue: &mut Queue) -> bool {
        let current = queue.current_token();
        run.advance(queue, current, "Файл недоступен").unwrap()
    }

    #[test]
    fn failed_media_skips_repeat_one_but_never_wraps_without_repeat_all() {
        for repeat in [Repeat::Off, Repeat::One] {
            let mut q = queue(repeat, 1);
            let mut failures = FailedRun::default();
            assert!(fail(&mut failures, &mut q));
            assert_eq!(q.index(), Some(2));
            assert!(!fail(&mut failures, &mut q));
            assert_eq!(q.index(), Some(2));
            assert_eq!(q.repeat(), repeat);
        }
    }

    #[test]
    fn failed_media_visits_each_row_once_across_wrap_and_reorder() {
        let mut q = queue(Repeat::All, 2);
        let mut failures = FailedRun::default();
        let stale = q.current_token();
        assert!(fail(&mut failures, &mut q));
        assert_eq!(q.index(), Some(0));
        assert_eq!(failures.advance(&mut q, stale, "Устаревшая ошибка"), None);
        assert_eq!(q.playback_error(0), None);
        assert_eq!(q.playback_error(2), Some("Файл недоступен"));
        let last_untried = q.row_token(1);
        q.move_item(2, 1);
        assert_eq!(q.playback_error(1), Some("Файл недоступен"));
        assert_eq!(q.playback_error(2), None);
        assert!(fail(&mut failures, &mut q));
        assert_eq!(q.row_token(q.index().unwrap()), last_untried);
        assert!(!fail(&mut failures, &mut q));
        // A fresh explicit attempt may retry the other rows.
        assert!(fail(&mut failures, &mut q));
    }

    #[test]
    fn failed_media_singleton_repeat_all_stops() {
        let mut q = Queue::from_path("missing", "missing", "");
        q.select(0);
        q.set_repeat(Repeat::All);
        let token = q.current_token();
        assert!(!fail(&mut FailedRun::default(), &mut q));
        assert_eq!(q.current_token(), token);
    }
}
