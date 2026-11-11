//! System media keys. Linux uses MPRIS. A missing session bus must not stop the player.
//! Windows needs a window handle; that attachment is step 8. Until then this fails softly there too.

use std::sync::mpsc::Sender;

use crate::commands::{self, Command, MediaKind};

pub fn attach(tx: Sender<Command>) -> Option<souvlaki::MediaControls> {
    let config = souvlaki::PlatformConfig {
        dbus_name: "homemusic",
        display_name: "HomeMusic",
        hwnd: None,
    };
    let mut controls = souvlaki::MediaControls::new(config).ok()?;
    if controls
        .attach(move |event| {
            if let Some(command) = map_event(event) {
                let _ = tx.send(command);
            }
        })
        .is_err()
    {
        return None;
    }
    Some(controls)
}

fn map_event(event: souvlaki::MediaControlEvent) -> Option<Command> {
    let kind = match event {
        souvlaki::MediaControlEvent::Play => MediaKind::Play,
        souvlaki::MediaControlEvent::Pause => MediaKind::Pause,
        souvlaki::MediaControlEvent::Toggle => MediaKind::Toggle,
        souvlaki::MediaControlEvent::Next => MediaKind::Next,
        souvlaki::MediaControlEvent::Previous => MediaKind::Previous,
        souvlaki::MediaControlEvent::Stop => MediaKind::Stop,
        souvlaki::MediaControlEvent::SetVolume(value) => MediaKind::SetVolume(value),
        souvlaki::MediaControlEvent::Seek(_)
        | souvlaki::MediaControlEvent::SeekBy(_, _)
        | souvlaki::MediaControlEvent::SetPosition(_)
        | souvlaki::MediaControlEvent::OpenUri(_)
        | souvlaki::MediaControlEvent::Raise
        | souvlaki::MediaControlEvent::Quit => return None,
    };
    commands::from_media(kind)
}
