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
    pub cover_id: String,
    pub accent: bool,
}

/// Sections retain offsets into the flat action model, including preceding notes.
pub fn sections(rows: &[Row]) -> Vec<(&str, std::ops::Range<usize>)> {
    let mut groups = Vec::new();
    let mut heading = None;
    for (index, row) in rows.iter().enumerate() {
        if row.kind == "section" {
            if let Some((title, start)) = heading.take() {
                if start < index {
                    groups.push((title, start..index));
                }
            }
            heading = Some((row.title.as_str(), index + 1));
        } else if row.kind != "radio" && row.kind != "album" {
            if let Some((title, start)) = heading.take() {
                if start < index {
                    groups.push((title, start..index));
                }
            }
        }
    }
    if let Some((title, start)) = heading {
        if start < rows.len() {
            groups.push((title, start..rows.len()));
        }
    }
    groups
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
        cover_id: String::new(),
        accent: false,
    });
    let (title, detail) = mix_card(mix);
    rows.push(Row {
        kind: "mix",
        title,
        detail,
        key: "mix".into(),
        cover_id: String::new(),
        accent: false,
    });
    if !feed.radio.is_empty() {
        rows.push(section("Радиостанции"));
        for artist in &feed.radio {
            rows.push(Row {
                kind: "radio",
                title: artist.name.clone(),
                detail: String::new(),
                key: artist.id.clone(),
                cover_id: artist.cover_id.clone(),
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
    let mut rows = Vec::new();
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
            cover_id: String::new(),
            accent: false,
        });
    }
    rows
}

pub fn liked_rows(songs: &[hub::Playable], albums: &[HomeAlbum]) -> Vec<Row> {
    let mut rows = Vec::new();
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
    let mut rows = Vec::new();
    match mix.phase {
        MixPhase::Ready => {
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
                    cover_id: String::new(),
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
    let mut rows = vec![note("Выберите 3–5 исполнителей")];
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
            cover_id: String::new(),
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
            cover_id: album.cover_id.clone(),
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
        cover_id: String::new(),
        accent: false,
    }
}

fn section(title: &str) -> Row {
    Row {
        kind: "section",
        title: title.into(),
        detail: String::new(),
        key: String::new(),
        cover_id: String::new(),
        accent: false,
    }
}

fn note(text: &str) -> Row {
    Row {
        kind: "note",
        title: text.into(),
        detail: String::new(),
        key: String::new(),
        cover_id: String::new(),
        accent: false,
    }
}

fn action(key: &str, title: &str) -> Row {
    Row {
        kind: "action",
        title: title.into(),
        detail: String::new(),
        key: key.into(),
        cover_id: String::new(),
        accent: true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn section_sources_survive_notes_and_repeated_albums() {
        let mut feed = HomeFeed::default();
        feed.radio.push(hub::HomeArtist {
            id: "radio".into(), name: "Радио".into(), cover_id: String::new(),
        });
        feed.recent.push(album("same", "Недавний"));
        feed.frequent.push(album("same", "Частый"));
        let mut rows = home_rows("", &MixSnapshot::default(), &feed);
        rows.insert(0, note("Ошибка обновления"));
        let groups = sections(&rows);
        assert_eq!(groups.len(), 3);
        assert_eq!(groups[0].0, "Радиостанции");
        assert_eq!(groups[1].0, "Недавно слушали");
        assert_eq!(groups[2].0, "Часто слушаете");
        let sources: Vec<_> = groups.iter().flat_map(|(_, range)| range.clone()).collect();
        assert_eq!(rows[sources[0]].key, "radio");
        assert_eq!(rows[sources[1]].cover_id, "al-same");
        assert_eq!(rows[sources[2]].key, "same");
    }

    fn album(id: &str, title: &str) -> HomeAlbum {
        HomeAlbum {
            id: id.into(),
            cover_id: format!("al-{id}"),
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
