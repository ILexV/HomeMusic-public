//! System media keys. Linux uses MPRIS and ignores the window handle.
//! A missing session bus must not stop the player.
//! Windows needs the real HWND. The backend panics if it is missing, so this
//! returns None until the handle exists and the player keeps running.

use std::sync::mpsc::Sender;

use crate::commands::{self, Command, MediaKind};

pub fn attach(tx: Sender<Command>, hwnd: Option<isize>) -> Option<souvlaki::MediaControls> {
    let config = souvlaki::PlatformConfig {
        dbus_name: "homemusic",
        display_name: "HomeMusic",
        hwnd: platform_hwnd(hwnd)?,
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

/// Linux always asks MPRIS with no handle. Windows refuses to ask until the HWND exists.
fn platform_hwnd(hwnd: Option<isize>) -> Option<Option<*mut std::ffi::c_void>> {
    #[cfg(target_os = "windows")]
    {
        let hwnd = hwnd?;
        if hwnd == 0 {
            return None;
        }
        Some(Some(hwnd as *mut std::ffi::c_void))
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = hwnd;
        Some(None)
    }
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
