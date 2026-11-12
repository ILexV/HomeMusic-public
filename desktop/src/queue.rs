//! The playing queue. Next and previous stay inside this list.

use std::sync::atomic::{AtomicU64, Ordering};

fn next_revision() -> u64 {
    static REVISION: AtomicU64 = AtomicU64::new(1);
    REVISION.fetch_add(1, Ordering::Relaxed)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Item {
    pub id: String,
    pub path: String,
    /// Catalog ref for a Hub stream. Empty for a library song or a local file.
    /// The stream URL is built at play time and is not stored here.
    pub listen_ref: String,
    pub title: String,
    pub artist: String,
    pub album: String,
    pub meta: String,
    pub duration_s: u32,
}

impl Item {
    pub fn file(path: &str, title: &str) -> Self {
        Self {
            id: String::new(),
            path: path.to_string(),
            listen_ref: String::new(),
            title: title.to_string(),
            artist: String::new(),
            album: String::new(),
            meta: String::new(),
            duration_s: 0,
        }
    }
}

pub struct Queue {
    name: String,
    items: Vec<Item>,
    index: Option<usize>,
    revision: u64,
    /// Identity of the current playback occurrence, independent of row position.
    current_token: Option<u64>,
}

impl Queue {
    pub fn from_tracks(name: impl Into<String>, items: Vec<Item>) -> Self {
        Self {
            name: name.into(),
            items,
            index: None,
            revision: next_revision(),
            current_token: None,
        }
    }

    pub fn from_path(path: &str, title: &str, meta: &str) -> Self {
        let mut item = Item::file(path, title);
        item.meta = meta.to_string();
        Self::from_tracks(String::new(), vec![item])
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn items(&self) -> &[Item] {
        &self.items
    }

    pub fn into_items(self) -> Vec<Item> {
        self.items
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn index(&self) -> Option<usize> {
        self.index
    }

    pub fn revision(&self) -> u64 {
        self.revision
    }

    pub fn current_token(&self) -> Option<u64> {
        self.current_token
    }

    pub fn current(&self) -> Option<&Item> {
        self.index.and_then(|index| self.items.get(index))
    }

    /// Play this row, including the row that is already current.
    pub fn select(&mut self, index: usize) -> bool {
        if index >= self.items.len() {
            return false;
        }
        self.index = Some(index);
        self.revision = next_revision();
        self.current_token = Some(self.revision);
        true
    }

    /// Append tracks without changing the current selection.
    pub fn extend(&mut self, items: impl IntoIterator<Item = Item>) {
        let previous_len = self.items.len();
        self.items.extend(items);
        if self.items.len() != previous_len {
            self.revision = next_revision();
        }
    }

    /// Move a row to its final index, preserving the logical current item.
    pub fn move_item(&mut self, from: usize, to: usize) -> bool {
        if from >= self.items.len() || to >= self.items.len() || from == to {
            return false;
        }
        if from < to {
            self.items[from..=to].rotate_left(1);
        } else {
            self.items[to..=from].rotate_right(1);
        }
        self.index = self.index.map(|current| {
            if current == from {
                to
            } else if from < current && current <= to {
                current - 1
            } else if to <= current && current < from {
                current + 1
            } else {
                current
            }
        });
        self.revision = next_revision();
        true
    }

    /// Remove a row, selecting its successor (or predecessor) if it was current.
    pub fn remove(&mut self, index: usize) -> bool {
        if index >= self.items.len() {
            return false;
        }
        let removed_current = self.index == Some(index);
        self.items.remove(index);
        self.index = self.index.and_then(|current| {
            if current > index {
                Some(current - 1)
            } else if current == index {
                if self.items.is_empty() {
                    None
                } else {
                    Some(index.min(self.items.len() - 1))
                }
            } else {
                Some(current)
            }
        });
        self.revision = next_revision();
        if removed_current {
            self.current_token = self.index.map(|_| self.revision);
        }
        true
    }

    /// Move an existing row after the current one, or to the front when idle.
    pub fn play_next(&mut self, index: usize) -> bool {
        let to = match self.index {
            Some(current) if index == current => return false,
            Some(current) if index < current => current,
            Some(current) => current + 1,
            None => 0,
        };
        self.move_item(index, to)
    }

    /// Insert after the current row, or at the front when idle, without selecting.
    pub fn insert_next(&mut self, item: Item) {
        let at = self.index.map(|index| index + 1).unwrap_or(0);
        self.items.insert(at, item);
        self.revision = next_revision();
    }

    /// Put a track after the current one and make it current. An idle queue starts with it.
    pub fn play_inserted(&mut self, item: Item) -> bool {
        let at = self.index.map(|index| index + 1).unwrap_or(0);
        self.insert_next(item);
        self.select(at)
    }

    pub fn next(&mut self) -> bool {
        match self.index {
            Some(index) if index + 1 < self.items.len() => {
                self.select(index + 1)
            }
            Some(_) => false,
            None => self.select(0),
        }
    }

    pub fn prev(&mut self) -> bool {
        match self.index {
            Some(index) if index > 0 => {
                self.select(index - 1)
            }
            _ => false,
        }
    }

    /// `47 треков · 2:55:19`. An unknown duration is an em dash.
    pub fn total_label(&self) -> String {
        if self.items.is_empty() {
            return count_label(0);
        }
        let seconds = self
            .items
            .iter()
            .map(|item| u64::from(item.duration_s))
            .sum::<u64>();
        if seconds == 0 {
            format!("{} · —", count_label(self.items.len()))
        } else {
            format!(
                "{} · {}",
                count_label(self.items.len()),
                total_clock(seconds)
            )
        }
    }
}

fn count_label(count: usize) -> String {
    let n = count as u64;
    let word = match n % 100 {
        11..=14 => "треков",
        _ => match n % 10 {
            1 => "трек",
            2..=4 => "трека",
            _ => "треков",
        },
    };
    format!("{n} {word}")
}

fn total_clock(seconds: u64) -> String {
    if seconds >= 3600 {
        format!(
            "{}:{:02}:{:02}",
            seconds / 3600,
            (seconds / 60) % 60,
            seconds % 60
        )
    } else {
        format!("{}:{:02}", seconds / 60, seconds % 60)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn revisions_distinguish_replacement_and_occurrences_survive_neighbor_edits() {
        let mut queue = Queue::from_tracks("same", vec![item("a", "A", 60); 3]);
        let original_revision = queue.revision();
        let replacement = Queue::from_tracks("same", vec![item("a", "A", 60); 3]);
        assert_ne!(original_revision, replacement.revision());
        queue.select(1);
        let token = queue.current_token();
        assert_ne!(queue.revision(), original_revision);
        queue.remove(0);
        assert_eq!(queue.index(), Some(0));
        assert_eq!(queue.current_token(), token);
        queue.insert_next(item("b", "B", 60));
        queue.extend([item("c", "C", 60)]);
        queue.move_item(0, 2);
        assert_eq!(queue.current_token(), token);
        assert_eq!(queue.index(), Some(2));
        queue.select(2); // Explicit replay of the same row starts a new occurrence.
        assert_ne!(queue.current_token(), token);
        let replay_token = queue.current_token();
        queue.prev();
        assert_ne!(queue.current_token(), replay_token);
        let previous_token = queue.current_token();
        queue.next();
        assert_ne!(queue.current_token(), previous_token);
        let revision = queue.revision();
        queue.extend(Vec::new());
        assert!(!queue.move_item(0, 0));
        assert!(!queue.remove(99));
        assert!(!queue.select(99));
        assert_eq!(queue.revision(), revision);
        while !queue.is_empty() {
            queue.remove(0);
        }
        assert_eq!(queue.current_token(), None);
    }

    fn item(id: &str, title: &str, duration_s: u32) -> Item {
        Item {
            id: id.into(),
            path: String::new(),
            listen_ref: String::new(),
            title: title.into(),
            artist: String::new(),
            album: String::new(),
            meta: String::new(),
            duration_s,
        }
    }

    fn sample() -> Queue {
        Queue::from_tracks(
            "Любимое",
            vec![
                item("a", "One", 129),
                item("b", "Two", 10),
                item("c", "Three", 61),
            ],
        )
    }

    #[test]
    fn next_and_prev_stay_inside_the_queue() {
        let mut queue = sample();
        assert!(queue.select(0));
        assert!(!queue.prev());
        assert_eq!(queue.index(), Some(0));
        assert!(queue.select(0), "the current row plays again");
        assert_eq!(queue.current().unwrap().id, "a");
        assert!(queue.next());
        assert_eq!(queue.current().unwrap().id, "b");
        assert!(queue.next());
        assert!(!queue.next());
        assert_eq!(queue.index(), Some(2));
        assert!(queue.prev());
        assert_eq!(queue.index(), Some(1));
        assert!(!queue.select(3));
        assert_eq!(queue.index(), Some(1));
    }

    #[test]
    fn an_inserted_track_plays_after_the_current_one() {
        let mut queue = sample();
        assert!(queue.select(0));
        assert!(queue.play_inserted(item("z", "Новый", 10)));
        assert_eq!(queue.current().unwrap().id, "z");
        assert_eq!(queue.items()[0].id, "a");
        assert_eq!(queue.items()[1].id, "z");
    }

    #[test]
    fn moving_rows_up_and_down_preserves_current_identity() {
        for current in 0..3 {
            for from in 0..3 {
                for to in 0..3 {
                    let mut queue = sample();
                    queue.select(current);
                    let selected = queue.current().unwrap().clone();
                    let mut expected = queue.items().to_vec();
                    let moved = expected.remove(from);
                    expected.insert(to, moved);
                    assert_eq!(queue.move_item(from, to), from != to);
                    assert_eq!(queue.items(), expected.as_slice());
                    assert_eq!(queue.current(), Some(&selected));
                }
            }
        }
    }

    #[test]
    fn moving_rows_rejects_invalid_indexes_and_preserves_idle_selection() {
        let mut queue = sample();
        let original = queue.items().to_vec();
        assert!(!queue.move_item(3, 0));
        assert!(!queue.move_item(0, usize::MAX));
        assert!(!queue.move_item(1, 1));
        assert_eq!(queue.items(), original.as_slice());
        assert!(queue.move_item(2, 0));
        assert_eq!(queue.items()[0].id, "c");
        assert_eq!(queue.index(), None);
        let mut empty = Queue::from_tracks("", vec![]);
        assert!(!empty.move_item(0, 0));
        assert_eq!(empty.index(), None);
    }

    #[test]
    fn removing_other_rows_preserves_current_identity() {
        let mut queue = sample();
        queue.select(1);
        assert!(queue.remove(0));
        assert_eq!(queue.index(), Some(0));
        assert_eq!(queue.current().unwrap().id, "b");
        assert!(queue.remove(1));
        assert_eq!(queue.index(), Some(0));
        assert_eq!(queue.current().unwrap().id, "b");
        assert!(!queue.remove(usize::MAX));
        assert_eq!(queue.current().unwrap().id, "b");
    }

    #[test]
    fn removing_current_selects_successor_then_predecessor_then_none() {
        let mut queue = sample();
        queue.select(1);
        assert!(queue.remove(1));
        assert_eq!(queue.index(), Some(1));
        assert_eq!(queue.current().unwrap().id, "c");
        assert!(queue.remove(1));
        assert_eq!(queue.index(), Some(0));
        assert_eq!(queue.current().unwrap().id, "a");
        assert!(queue.remove(0));
        assert!(queue.is_empty());
        assert_eq!(queue.index(), None);
        assert!(!queue.remove(0));
    }

    #[test]
    fn removing_rows_from_idle_queue_never_selects() {
        let mut queue = sample();
        for index in [1, 0, 0] {
            assert!(queue.remove(index));
            assert_eq!(queue.index(), None);
        }
        assert!(queue.is_empty());
    }

    #[test]
    fn play_next_moves_from_either_side_without_changing_current() {
        let mut queue = sample();
        queue.select(1);
        assert!(queue.play_next(0));
        assert_eq!(queue.index(), Some(0));
        assert_eq!(queue.current().unwrap().id, "b");
        assert_eq!(queue.items()[1].id, "a");
        assert!(queue.play_next(2));
        assert_eq!(queue.current().unwrap().id, "b");
        assert_eq!(queue.items()[1].id, "c");
        let original = queue.items().to_vec();
        assert!(!queue.play_next(0));
        assert!(!queue.play_next(1));
        assert!(!queue.play_next(usize::MAX));
        assert_eq!(queue.items(), original.as_slice());
        queue.select(2);
        assert!(queue.play_next(0));
        assert_eq!(queue.current().unwrap().id, "a");
        assert_eq!(queue.index(), Some(1));
        assert_eq!(queue.items()[2].id, "b");
    }

    #[test]
    fn play_next_when_idle_moves_to_front_without_selecting() {
        let mut queue = sample();
        assert!(queue.play_next(2));
        assert_eq!(queue.items()[0].id, "c");
        assert_eq!(queue.index(), None);
        assert!(!queue.play_next(0));
        assert!(!queue.play_next(3));
        let mut empty = Queue::from_tracks("", vec![]);
        assert!(!empty.play_next(0));
        assert_eq!(empty.index(), None);
    }

    #[test]
    fn insert_next_preserves_current_or_idle_selection() {
        let mut queue = sample();
        queue.select(2);
        queue.insert_next(item("z", "New", 10));
        assert_eq!(queue.index(), Some(2));
        assert_eq!(queue.current().unwrap().id, "c");
        assert_eq!(queue.items()[3].id, "z");
        let mut idle = sample();
        idle.insert_next(item("z", "New", 10));
        assert_eq!(idle.items()[0].id, "z");
        assert_eq!(idle.index(), None);
        let mut empty = Queue::from_tracks("", vec![]);
        empty.insert_next(item("z", "New", 10));
        assert_eq!(empty.items()[0].id, "z");
        assert_eq!(empty.index(), None);
    }

    #[test]
    fn edits_preserve_duplicate_id_occurrence_not_first_matching_id() {
        let mut queue = Queue::from_tracks(
            "",
            vec![
                item("same", "First", 1),
                item("same", "Second", 2),
                item("same", "Third", 3),
            ],
        );
        queue.select(1);
        assert!(queue.move_item(0, 2));
        assert_eq!(queue.current().unwrap().title, "Second");
        assert!(queue.play_next(2));
        assert_eq!(queue.current().unwrap().title, "Second");
        assert!(queue.remove(1));
        assert_eq!(queue.current().unwrap().title, "Second");
        queue.insert_next(item("same", "Fourth", 4));
        assert_eq!(queue.current().unwrap().title, "Second");
        assert!(queue.remove(0));
        assert_eq!(queue.current().unwrap().title, "Fourth");
    }

    #[test]
    fn play_inserted_still_selects_front_when_idle_or_empty() {
        let mut idle = sample();
        assert!(idle.play_inserted(item("z", "New", 10)));
        assert_eq!(idle.index(), Some(0));
        assert_eq!(idle.current().unwrap().id, "z");
        assert_eq!(idle.items()[1].id, "a");
        let mut empty = Queue::from_tracks("", vec![]);
        assert!(empty.play_inserted(item("z", "New", 10)));
        assert_eq!(empty.index(), Some(0));
        assert_eq!(empty.current().unwrap().id, "z");
    }

    #[test]
    fn total_uses_the_russian_plural_and_hours() {
        assert_eq!(count_label(1), "1 трек");
        assert_eq!(count_label(2), "2 трека");
        assert_eq!(count_label(4), "4 трека");
        assert_eq!(count_label(5), "5 треков");
        assert_eq!(count_label(11), "11 треков");
        assert_eq!(count_label(14), "14 треков");
        assert_eq!(count_label(21), "21 трек");
        assert_eq!(count_label(22), "22 трека");
        assert_eq!(count_label(47), "47 треков");
        assert_eq!(total_clock(200), "3:20");
        assert_eq!(total_clock(10519), "2:55:19");
        assert_eq!(sample().total_label(), "3 трека · 3:20");
        let long = Queue::from_tracks("n", vec![item("a", "t", 10519)]);
        assert_eq!(long.total_label(), "1 трек · 2:55:19");
        assert_eq!(Queue::from_tracks("n", Vec::new()).total_label(), "0 треков");
        assert_eq!(
            Queue::from_path("/tmp/a.wav", "a", "WAV").total_label(),
            "1 трек · —"
        );
    }
}
