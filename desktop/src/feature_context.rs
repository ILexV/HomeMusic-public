//! Shared handles for optional player panels; playback still goes through the main controller.
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

#[derive(Clone)]
pub(crate) struct Context {
    pub ui: slint::Weak<crate::AppWindow>,
    pub player: Rc<RefCell<crate::player::Player>>,
    pub queue: Rc<RefCell<crate::queue::Queue>>,
    pub config: Rc<crate::navidrome::ServerConfig>,
    pub hub: Rc<crate::HubState>,
    pub waves: Arc<crate::wave::WaveHub>,
}

impl Context {
    pub fn restart(&self) -> Result<(), String> {
        let ui = self.ui.upgrade().ok_or_else(|| "Окно плеера закрыто".to_string())?;
        let executable = std::env::current_exe().map_err(|_| "Не удалось найти программу".to_string())?;
        let snapshot = self.player.borrow().snapshot();
        crate::record_playback(&ui, &self.hub, &self.queue.borrow(), &snapshot, true);
        crate::save_place(&ui, &self.hub);
        self.player.borrow_mut().set_paused(true);
        let mut restart = std::process::Command::new(executable);
        restart.current_dir(crate::paths::dir())
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            // The replacement must outlive the old terminal/process group.
            restart.process_group(0);
        }
        if restart.spawn().is_err() {
            self.player.borrow_mut().set_paused(!snapshot.playing);
            return Err("Настройки сохранены, но перезапуск не удался. Закройте и откройте HomeMusic.".into());
        }
        let _ = slint::quit_event_loop();
        Ok(())
    }

    pub fn play_current(&self) {
        if let Some(ui) = self.ui.upgrade() {
            let _ = crate::play_current(&ui, &self.player, &self.queue, &self.config, &self.waves, &self.hub);
            crate::push_playback(&ui, &self.player.borrow().snapshot());
        }
    }


    pub fn play_current_at(&self, position: f32, paused: bool) -> Result<(), String> {
        let ui = self.ui.upgrade().ok_or_else(|| "Окно плеера закрыто".to_string())?;
        crate::play_current_at(&ui, &self.player, &self.queue, &self.config, &self.waves, &self.hub, position, paused)
    }

    pub fn launch_queue(&self, mut queue: crate::queue::Queue) {
        queue.inherit_preferences(&self.queue.borrow());
        self.replace_queue(queue, true);
    }
    pub fn replace_queue(&self, queue: crate::queue::Queue, start: bool) {
        if let Some(ui) = self.ui.upgrade() {
            if let Some(item) = self.queue.borrow().current() {
                crate::note_wave_leave(&self.player.borrow(), item, &self.hub, false);
            }
            crate::cancel_queue_load(&ui, &self.hub);
            crate::clear_wave(&self.hub);
            self.hub.album_id.borrow_mut().clear();
            *self.queue.borrow_mut() = queue;
            self.refresh_queue();
            if start {
                self.play_current();
            }
        }
    }

    pub fn refresh_queue(&self) {
        self.player.borrow_mut().cancel_preload();
        if let Some(ui) = self.ui.upgrade() {
            crate::show_queue(&ui, &self.queue.borrow(), &self.hub);
        }
    }

    pub fn seek(&self, seconds: f64) {
        self.player.borrow_mut().seek_seconds(seconds);
        if let Some(ui) = self.ui.upgrade() {
            crate::push_playback(&ui, &self.player.borrow().snapshot());
        }
    }

    pub fn current_target(&self) -> Option<(String, String)> {
        crate::current_target(&self.queue.borrow(), &self.config, &self.hub)
    }
}
