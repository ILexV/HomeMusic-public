//! Rows of the site's «Главная» for the large window.
//! The Hub still builds the wave and the mix. This module only turns that into lines.

use std::collections::HashSet;

use crate::hub::{self, HomeAlbum, HomeFeed, MixPhase, MixSnapshot, PlaylistLine, TasteArtist};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Pane {
    Home,
    Library,
    Playlists,
    Liked,
    Mix,
    Taste,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Row {
    pub kind: &'static str,
    pub title: String,
    pub detail: String,
    pub key: String,
    pub accent: bool,
}

pub fn greeting(hour: u32, name: &str) -> String {
    let hello = if hour < 5 {
        "Доброй ночи"
    } else if hour < 12 {
        "Доброе утро"
    } else if hour < 18 {
        "Добрый день"
    } else {
        "Добрый вечер"
    };
    if name.is_empty() {
        hello.to_string()
    } else {
        format!("{hello}, {name}")
    }
}

pub fn local_hour() -> u32 {
    let stamp = unsafe { libc::time(std::ptr::null_mut()) };
    if stamp < 0 {
        return 12;
    }
    unsafe {
        let mut tm = std::mem::zeroed();
        #[cfg(unix)]
        {
            if libc::localtime_r(&stamp, &mut tm).is_null() {
                return 12;
            }
        }
        #[cfg(windows)]
        {
            if libc::localtime_s(&mut tm, &stamp) != 0 {
                return 12;
            }
        }
        tm.tm_hour.max(0) as u32
    }
}

pub fn wave_caption(busy: bool, personal: bool, playing: bool) -> &'static str {
    if busy {
        "Подбираем первые треки…"
    } else if personal && playing {
        "Играет — подстраивается под каждый пропуск и ♥"
    } else if personal {
        "На паузе — продолжим с того же места"
    } else {
        "Бесконечный поток под ваш вкус"
    }
}

pub fn home_rows(wave_line: &str, mix: &MixSnapshot, feed: &HomeFeed) -> Vec<Row> {
    let mut rows = Vec::new();
    rows.push(Row {
        kind: "wave",
        title: "Моя волна".into(),
        detail: wave_line.into(),
        key: "wave".into(),
        accent: false,
    });
    let (title, detail) = mix_card(mix);
    rows.push(Row {
        kind: "mix",
        title,
        detail,
        key: "mix".into(),
        accent: false,
    });
    for (title, key) in [
        ("Понравившиеся", "liked"),
        ("Плейлисты", "playlists"),
        ("Исполнители", "artists"),
        ("Все альбомы", "albums"),
    ] {
        rows.push(Row {
            kind: "shortcut",
            title: title.into(),
            detail: String::new(),
            key: key.into(),
            accent: false,
        });
    }
    if !feed.radio.is_empty() {
        rows.push(section("Радиостанции"));
        for artist in &feed.radio {
            rows.push(Row {
                kind: "radio",
                title: artist.name.clone(),
                detail: String::new(),
                key: artist.id.clone(),
                accent: false,
            });
        }
    }
    push_albums(&mut rows, "Недавно слушали", &feed.recent);
    push_albums(&mut rows, "Часто слушаете", &feed.frequent);
    push_albums(&mut rows, "Понравившиеся альбомы", &feed.starred);
    push_albums(&mut rows, "Новое в библиотеке", &feed.newest);
    rows
}

pub fn playlist_rows(lists: &[PlaylistLine]) -> Vec<Row> {
    let mut rows = vec![back()];
    if lists.is_empty() {
        rows.push(note("Плейлистов нет"));
        return rows;
    }
    for list in lists {
        rows.push(Row {
            kind: "playlist",
            title: list.name.clone(),
            detail: songs_label(list.songs),
            key: list.id.clone(),
            accent: false,
        });
    }
    rows
}

pub fn liked_rows(songs: &[hub::Playable], albums: &[HomeAlbum]) -> Vec<Row> {
    let mut rows = vec![back()];
    if songs.is_empty() && albums.is_empty() {
        rows.push(note("Пока ничего"));
        return rows;
    }
    if !songs.is_empty() {
        rows.push(action("play", "Слушать"));
        for (index, song) in songs.iter().enumerate() {
            rows.push(track_row(index, song));
        }
    }
    push_albums(&mut rows, "Альбомы", albums);
    rows
}

pub fn mix_rows(mix: &MixSnapshot) -> Vec<Row> {
    let mut rows = vec![back()];
    match mix.phase {
        MixPhase::Ready => {
            rows.push(section(&mix.title));
            if !mix.description.is_empty() {
                rows.push(note(&mix.description));
            }
            for (mode, title) in [
                ("normal", "Обычный"),
                ("favorites", "Любимое"),
                ("discover", "Незнакомое"),
            ] {
                rows.push(Row {
                    kind: "mode",
                    title: title.into(),
                    detail: mode_hint(mode).into(),
                    key: mode.into(),
                    accent: mix.mode == mode,
                });
            }
            rows.push(action("play", "Слушать"));
            rows.push(action("rebuild", "Собрать заново"));
            for (index, song) in mix.tracks.iter().enumerate() {
                rows.push(track_row(index, song));
            }
        }
        MixPhase::Building => rows.push(note("Подбираем 50 песен по вашему вкусу — это займёт пару минут")),
        MixPhase::Onboarding => rows.push(note("Выберите 3–5 исполнителей")),
        MixPhase::Waiting => rows.push(note(&format!("«Микс дня» появится в {}:00", mix.hour))),
    }
    rows
}

pub fn taste_rows(artists: &[TasteArtist], picked: &HashSet<String>) -> Vec<Row> {
    let mut rows = vec![back(), note("Выберите 3–5 исполнителей")];
    if (3..=5).contains(&picked.len()) {
        rows.push(action("save", "Собрать микс"));
    }
    if artists.is_empty() {
        rows.push(note("Исполнители не пришли"));
        return rows;
    }
    for artist in artists {
        rows.push(Row {
            kind: "taste",
            title: artist.name.clone(),
            detail: String::new(),
            key: artist.reference.clone(),
            accent: picked.contains(&artist.reference),
        });
    }
    rows
}

pub fn mix_card(mix: &MixSnapshot) -> (String, String) {
    match mix.phase {
        MixPhase::Ready => {
            let mut detail = mix.description.clone();
            let songs = songs_label(mix.total);
            if detail.is_empty() {
                detail = songs;
            } else {
                detail = format!("{detail} · {songs}");
            }
            if mix.ready < mix.total {
                detail = format!("{detail} · качается {}", mix.total - mix.ready);
            }
            (mix.title.clone(), detail)
        }
        MixPhase::Building => (
            "Собираем «Микс дня»".into(),
            "Подбираем 50 песен по вашему вкусу — это займёт пару минут".into(),
        ),
        MixPhase::Onboarding => (
            "Что вы слушаете?".into(),
            "Выберите 3–5 исполнителей — соберём для вас «Микс дня»".into(),
        ),
        MixPhase::Waiting => (
            "«Микс дня»".into(),
            format!("появится в {}:00", mix.hour),
        ),
    }
}

pub fn songs_label(count: u32) -> String {
    let word = match count % 100 {
        11..=14 => "песен",
        _ => match count % 10 {
            1 => "песня",
            2..=4 => "песни",
            _ => "песен",
        },
    };
    format!("{count} {word}")
}

fn mode_hint(mode: &str) -> &'static str {
    match mode {
        "favorites" => "В основном то, что вы любите: 80 % знакомого",
        "discover" => "Почти всё новое: 90 % песен, которых вы не слышали",
        _ => "40 % знакомого и 60 % нового",
    }
}

fn album_detail(album: &HomeAlbum) -> String {
    match (album.artist.as_str(), album.year) {
        ("", None) => String::new(),
        ("", Some(year)) => year.to_string(),
        (artist, None) => artist.to_string(),
        (artist, Some(year)) => format!("{artist} · {year}"),
    }
}

fn push_albums(rows: &mut Vec<Row>, title: &str, albums: &[HomeAlbum]) {
    if albums.is_empty() {
        return;
    }
    rows.push(section(title));
    for album in albums {
        rows.push(Row {
            kind: "album",
            title: album.title.clone(),
            detail: album_detail(album),
            key: album.id.clone(),
            accent: album.starred,
        });
    }
}

fn track_row(index: usize, song: &hub::Playable) -> Row {
    let detail = if song.album.is_empty() {
        song.artist.clone()
    } else if song.artist.is_empty() {
        song.album.clone()
    } else {
        format!("{} · {}", song.artist, song.album)
    };
    Row {
        kind: "track",
        title: song.title.clone(),
        detail,
        key: index.to_string(),
        accent: false,
    }
}

fn section(title: &str) -> Row {
    Row {
        kind: "section",
        title: title.into(),
        detail: String::new(),
        key: String::new(),
        accent: false,
    }
}

fn note(text: &str) -> Row {
    Row {
        kind: "note",
        title: text.into(),
        detail: String::new(),
        key: String::new(),
        accent: false,
    }
}

fn action(key: &str, title: &str) -> Row {
    Row {
        kind: "action",
        title: title.into(),
        detail: String::new(),
        key: key.into(),
        accent: true,
    }
}

fn back() -> Row {
    action("back", "На главную")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn album(id: &str, title: &str) -> HomeAlbum {
        HomeAlbum {
            id: id.into(),
            title: title.into(),
            artist: "V".into(),
            year: Some(2020),
            starred: false,
        }
    }

    #[test]
    fn greeting_follows_the_hour() {
        assert_eq!(greeting(4, "lex"), "Доброй ночи, lex");
        assert_eq!(greeting(11, "lex"), "Доброе утро, lex");
        assert_eq!(greeting(17, ""), "Добрый день");
        assert_eq!(greeting(18, "lex"), "Добрый вечер, lex");
    }

    #[test]
    fn empty_home_sections_are_left_out() {
        let mut feed = HomeFeed::default();
        feed.recent.push(album("a", "Ночной"));
        feed.radio.push(hub::HomeArtist { id: "r".into(), name: "Радио".into() });
        let mix = MixSnapshot {
            phase: MixPhase::Waiting,
            hour: 8,
            ..MixSnapshot::default()
        };
        let rows = home_rows(wave_caption(false, false, false), &mix, &feed);
        let titles: Vec<_> = rows.iter().map(|row| row.title.as_str()).collect();
        assert!(titles.contains(&"Моя волна"));
        assert!(titles.contains(&"«Микс дня»"));
        assert!(titles.contains(&"Понравившиеся"));
        assert!(titles.contains(&"Радиостанции"));
        assert!(titles.contains(&"Недавно слушали"));
        assert!(titles.contains(&"Ночной"));
        assert!(!titles.contains(&"Часто слушаете"));
        assert!(!titles.contains(&"Новое в библиотеке"));
        assert_eq!(rows.iter().find(|row| row.title == "Ночной").unwrap().detail, "V · 2020");
        assert_eq!(wave_caption(true, false, false), "Подбираем первые треки…");
        assert_eq!(
            wave_caption(false, true, true),
            "Играет — подстраивается под каждый пропуск и ♥"
        );
        assert_eq!(
            wave_caption(false, true, false),
            "На паузе — продолжим с того же места"
        );
    }

    #[test]
    fn a_ready_mix_counts_songs_still_downloading() {
        let mix = MixSnapshot {
            phase: MixPhase::Ready,
            title: "Четверг".into(),
            description: "Тихий".into(),
            total: 50,
            ready: 40,
            mode: "discover".into(),
            ..MixSnapshot::default()
        };
        let (title, detail) = mix_card(&mix);
        assert_eq!(title, "Четверг");
        assert_eq!(detail, "Тихий · 50 песен · качается 10");
        let rows = mix_rows(&mix);
        assert!(rows.iter().any(|row| row.kind == "mode" && row.key == "discover" && row.accent));
        assert_eq!(songs_label(1), "1 песня");
        assert_eq!(songs_label(2), "2 песни");
        assert_eq!(songs_label(5), "5 песен");
        assert_eq!(songs_label(11), "11 песен");
    }
}
