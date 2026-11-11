//! Keyboard and media-key commands. The window and the system session both call these.

use std::time::Duration;

pub const VOLUME_STEP: f32 = 0.05;
const TRAY_DOUBLE_CLICK: Duration = Duration::from_millis(400);

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Command {
    Toggle,
    Play,
    Pause,
    Stop,
    Next,
    Previous,
    VolumeUp,
    VolumeDown,
    SetVolume(f32),
}

/// What the operating system asked for. Seeking and opening a URI are ignored.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum MediaKind {
    Play,
    Pause,
    Toggle,
    Next,
    Previous,
    Stop,
    SetVolume(f64),
}

pub fn from_media(kind: MediaKind) -> Option<Command> {
    Some(match kind {
        MediaKind::Play => Command::Play,
        MediaKind::Pause => Command::Pause,
        MediaKind::Toggle => Command::Toggle,
        MediaKind::Next => Command::Next,
        MediaKind::Previous => Command::Previous,
        MediaKind::Stop => Command::Stop,
        MediaKind::SetVolume(value) => {
            if !value.is_finite() {
                return None;
            }
            Command::SetVolume(value.clamp(0.0, 1.0) as f32)
        }
    })
}

/// Focused shortcuts. `text` is the Slint key text: a space, or an arrow's private-use character.
/// Media-key names are accepted too, for a backend that delivers them as text.
pub fn shortcut_command(
    text: &str,
    control: bool,
    alt: bool,
    shift: bool,
    meta: bool,
) -> Option<Command> {
    if alt || meta {
        return None;
    }
    if text == " " && !control && !shift {
        return Some(Command::Toggle);
    }
    if control && !shift {
        return match text {
            "\u{F702}" => Some(Command::Previous),
            "\u{F703}" => Some(Command::Next),
            "\u{F700}" => Some(Command::VolumeUp),
            "\u{F701}" => Some(Command::VolumeDown),
            _ => None,
        };
    }
    if control || shift {
        return None;
    }
    match text {
        "\u{F73F}" | "MediaTrackPrevious" | "XF86AudioPrev" => Some(Command::Previous),
        "\u{F740}" | "MediaTrackNext" | "XF86AudioNext" => Some(Command::Next),
        "MediaPlayPause" | "XF86AudioPlay" => Some(Command::Toggle),
        "XF86AudioRaiseVolume" => Some(Command::VolumeUp),
        "XF86AudioLowerVolume" => Some(Command::VolumeDown),
        _ => None,
    }
}

pub fn volume_after(current: f32, command: Command) -> Option<f32> {
    match command {
        Command::VolumeUp => Some((current + VOLUME_STEP).clamp(0.0, 1.0)),
        Command::VolumeDown => Some((current - VOLUME_STEP).clamp(0.0, 1.0)),
        Command::SetVolume(value) => Some(value.clamp(0.0, 1.0)),
        _ => None,
    }
}

/// Two presses within 400 ms restore the window. The tray callback is a single click.
pub fn tray_click(last_ms: Option<u128>, now_ms: u128) -> (bool, Option<u128>) {
    let window = TRAY_DOUBLE_CLICK.as_millis();
    if let Some(last) = last_ms {
        if now_ms.saturating_sub(last) <= window {
            return (true, None);
        }
    }
    (false, Some(now_ms))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn focused_keys_map_to_transport_and_volume() {
        assert_eq!(
            shortcut_command(" ", false, false, false, false),
            Some(Command::Toggle)
        );
        assert_eq!(
            shortcut_command("\u{F702}", true, false, false, false),
            Some(Command::Previous)
        );
        assert_eq!(
            shortcut_command("\u{F703}", true, false, false, false),
            Some(Command::Next)
        );
        assert_eq!(
            shortcut_command("\u{F700}", true, false, false, false),
            Some(Command::VolumeUp)
        );
        assert_eq!(
            shortcut_command("\u{F701}", true, false, false, false),
            Some(Command::VolumeDown)
        );
        assert_eq!(shortcut_command(" ", true, false, false, false), None);
        assert_eq!(shortcut_command("a", false, false, false, false), None);
        assert_eq!(shortcut_command(" ", false, true, false, false), None);
    }

    #[test]
    fn media_keys_map_even_as_plain_names() {
        assert_eq!(
            shortcut_command("MediaPlayPause", false, false, false, false),
            Some(Command::Toggle)
        );
        assert_eq!(
            shortcut_command("XF86AudioNext", false, false, false, false),
            Some(Command::Next)
        );
        assert_eq!(
            shortcut_command("XF86AudioPrev", false, false, false, false),
            Some(Command::Previous)
        );
        assert_eq!(
            shortcut_command("XF86AudioRaiseVolume", false, false, false, false),
            Some(Command::VolumeUp)
        );
        assert_eq!(from_media(MediaKind::Play), Some(Command::Play));
        assert_eq!(from_media(MediaKind::Pause), Some(Command::Pause));
        assert_eq!(from_media(MediaKind::Next), Some(Command::Next));
        assert_eq!(from_media(MediaKind::Previous), Some(Command::Previous));
        assert_eq!(
            from_media(MediaKind::SetVolume(0.4)),
            Some(Command::SetVolume(0.4))
        );
        assert_eq!(from_media(MediaKind::SetVolume(f64::NAN)), None);
        assert_eq!(
            from_media(MediaKind::SetVolume(2.0)),
            Some(Command::SetVolume(1.0))
        );
    }

    #[test]
    fn volume_steps_stay_inside_the_range() {
        assert_eq!(volume_after(0.8, Command::VolumeUp), Some(0.85));
        assert_eq!(volume_after(0.02, Command::VolumeDown), Some(0.0));
        assert_eq!(volume_after(0.2, Command::SetVolume(1.4)), Some(1.0));
        assert_eq!(volume_after(0.2, Command::Next), None);
    }

    #[test]
    fn two_tray_clicks_inside_the_window_restore() {
        let (restore, last) = tray_click(None, 1_000);
        assert!(!restore);
        let (restore, last) = tray_click(last, 1_200);
        assert!(restore);
        assert!(last.is_none());
    }

    #[test]
    fn a_late_second_tray_click_does_not_restore() {
        let (_, last) = tray_click(None, 1_000);
        let (restore, last) = tray_click(last, 1_500);
        assert!(!restore);
        assert_eq!(last, Some(1_500));
    }
}
