//! Bridges the queue's occurrence identity and mpv's prepared media without replaying promotions.
use crate::{feature_context::Context, player::PlaybackState};

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
    observation: Option<(Option<u64>, PlaybackState)>,
}

impl State {
    pub fn tick(&mut self, ctx: &Context) {
        // Drain native boundary events before changing the native playlist.
        let (ended, promoted) = {
            let mut player = ctx.player.borrow_mut();
            (player.take_natural_end(), player.take_promoted())
        };
        if ended || promoted.is_some() {
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
            let moved = ctx.queue.borrow_mut().advance();
            crate::maybe_extend_wave(&ctx.hub, &ctx.queue.borrow());
            self.prepared = None;
            if moved {
                if adopted {
                    self.refresh_promoted(ctx);
                } else {
                    ctx.play_current();
                }
            } else if promoted.is_some() {
                // The user removed the predicted successor before native promotion was observed.
                ctx.player.borrow_mut().stop();
            }
        }
        self.prepare(ctx);
        self.observation = Some((ctx.queue.borrow().current_token(), ctx.player.borrow().snapshot()));
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
            let full_only = ctx.hub.wave.borrow().on() || queue.row_index(next).and_then(|index| queue.recommendation(index)).is_some();
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
