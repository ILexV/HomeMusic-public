//! The playing queue. Next and previous stay inside this list.

use std::sync::atomic::{AtomicU64, Ordering};
use std::collections::{HashMap, HashSet};
use serde_json::{json, Value};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Repeat { #[default] Off, All, One }

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Shuffle { #[default] Off, Ordinary, Smart }

#[derive(Clone, Debug)]
pub struct Recommendation {
    pub session: String,
    pub label: String,
}

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
    instance: u64,
    rows: Vec<u64>,
    original: Option<Vec<u64>>,
    recommendations: HashMap<u64, Recommendation>,
    repeat: Repeat,
    shuffle: Shuffle,
    smart_enabled: bool,
    context: String,
}

impl Queue {
    pub fn from_tracks(name: impl Into<String>, items: Vec<Item>) -> Self {
        Self {
            instance: next_revision(),
            rows: (0..items.len()).map(|_| next_revision()).collect(),
            original: None,
            recommendations: HashMap::new(),
            repeat: Repeat::Off,
            shuffle: Shuffle::Off,
            smart_enabled: true,
            context: String::new(),
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
            for _ in previous_len..self.items.len() {
                let row = next_revision();
                self.rows.push(row);
                if let Some(original) = &mut self.original { original.push(row); }
            }
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
            self.rows[from..=to].rotate_left(1);
        } else {
            self.items[to..=from].rotate_right(1);
            self.rows[to..=from].rotate_right(1);
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
        // An explicit edit also updates the unshuffled order by occurrence.
        if let Some(original) = &mut self.original {
            let row = self.rows[to];
            original.retain(|id| *id != row);
            let before = self.rows[to + 1..].iter().find_map(|id| original.iter().position(|x| x == id));
            original.insert(before.unwrap_or(original.len()), row);
        }
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
        let row = self.rows.remove(index);
        self.recommendations.remove(&row);
        if let Some(original) = &mut self.original { original.retain(|id| *id != row); }
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
        let row = next_revision();
        self.rows.insert(at, row);
        if let Some(original) = &mut self.original {
            let after = self.index.and_then(|i| original.iter().position(|id| *id == self.rows[i]));
            original.insert(after.map_or(0, |i| i + 1), row);
        }
        self.revision = next_revision();
    }

    /// Put a track after the current one and make it current. An idle queue starts with it.
    pub fn play_inserted(&mut self, item: Item) -> bool {
        let at = self.index.map(|index| index + 1).unwrap_or(0);
        self.insert_next(item);
        self.select(at)
    }

    fn next_index(&self, automatic: bool) -> Option<usize> {
        if self.items.is_empty() { return None; }
        match self.index {
            Some(index) if automatic && self.repeat == Repeat::One => Some(index),
            Some(index) if index + 1 < self.items.len() => Some(index + 1),
            Some(_) if self.repeat == Repeat::All => Some(0),
            Some(_) => None,
            None => Some(0),
        }
    }

    /// Manual next ignores repeat-one.
    pub fn next(&mut self) -> bool {
        self.next_index(false).is_some_and(|i| self.select(i))
    }

    /// Natural EOF (including preload promotion) respects repeat-one.
    pub fn advance(&mut self) -> bool {
        self.next_index(true).is_some_and(|i| self.select(i))
    }

    pub fn peek_next(&self) -> Option<&Item> {
        self.next_index(true).and_then(|i| self.items.get(i))
    }

    pub fn peek_next_token(&self) -> Option<u64> {
        self.next_index(true).and_then(|index| self.row_token(index))
    }

    pub fn prev(&mut self) -> bool {
        match self.index {
            Some(index) if index > 0 => self.select(index - 1),
            Some(0) if self.repeat == Repeat::All && !self.items.is_empty() => self.select(self.items.len() - 1),
            _ => false,
        }
    }

    pub fn instance(&self) -> u64 { self.instance }
    pub fn repeat(&self) -> Repeat { self.repeat }
    pub fn shuffle(&self) -> Shuffle { self.shuffle }
    pub fn smart_enabled(&self) -> bool { self.smart_enabled }
    pub fn context(&self) -> &str { &self.context }
    pub fn row_token(&self, index: usize) -> Option<u64> { self.rows.get(index).copied() }
    pub fn row_index(&self, token: u64) -> Option<usize> { self.rows.iter().position(|id| *id == token) }
    pub fn recommendation(&self, index: usize) -> Option<&Recommendation> {
        self.rows.get(index).and_then(|id| self.recommendations.get(id))
    }

    /// Only the actual launch source may provide this, never a display title.
    pub fn set_context(&mut self, context: impl Into<String>) {
        let context = context.into();
        self.context = if context == "liked" || ["playlist:", "mix:", "wave:"].iter().any(|prefix| context.strip_prefix(*prefix).is_some_and(|id| !id.is_empty() && id.len() <= 64 && !id.chars().any(char::is_whitespace))) { context } else { String::new() };
        self.revision = next_revision();
    }

    pub fn set_repeat(&mut self, repeat: Repeat) {
        if self.repeat != repeat { self.repeat = repeat; self.revision = next_revision(); }
    }

    pub fn set_smart_enabled(&mut self, enabled: bool) {
        if self.smart_enabled == enabled { return; }
        self.smart_enabled = enabled;
        if !enabled && self.shuffle == Shuffle::Smart { self.set_shuffle(Shuffle::Ordinary); }
        self.revision = next_revision();
    }

    /// Reorder owned allocations, not cloned Items; preserve playback identity.
    fn reorder(&mut self, order: &[u64]) {
        let current = self.index.map(|i| self.rows[i]);
        let mut rows: HashMap<_, _> = std::mem::take(&mut self.rows).into_iter()
            .zip(std::mem::take(&mut self.items)).collect();
        self.rows = Vec::with_capacity(order.len());
        self.items = Vec::with_capacity(order.len());
        for id in order {
            if let Some(item) = rows.remove(id) { self.rows.push(*id); self.items.push(item); }
        }
        debug_assert!(rows.is_empty(), "reorder must include every occurrence");
        self.index = current.and_then(|id| self.row_index(id));
        self.revision = next_revision();
    }

    fn remove_pending_recommendations(&mut self) {
        let start = self.index.map_or(0, |i| i + 1);
        for i in (start..self.items.len()).rev() {
            if self.recommendation(i).is_some() { self.remove(i); }
        }
    }

    pub fn set_shuffle(&mut self, shuffle: Shuffle) {
        if self.shuffle == shuffle { return; }
        if self.shuffle == Shuffle::Smart { self.remove_pending_recommendations(); }
        if shuffle == Shuffle::Off {
            if let Some(mut order) = self.original.take() {
                order.retain(|id| self.rows.contains(id));
                // Played/current recommendations remain; only pending ones were removed.
                let retained: Vec<_> = self.rows.iter().copied().filter(|id| !order.contains(id)).collect();
                order.splice(0..0, retained);
                self.reorder(&order);
            }
        } else if self.shuffle == Shuffle::Off {
            self.original = Some(self.rows.clone());
            let mut order = self.rows.clone();
            let start = if let Some(index) = self.index { order.swap(0, index); 1 } else { 0 };
            let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos() as u64;
            let mut random = nanos ^ next_revision();
            for i in (start + 1..order.len()).rev() {
                random ^= random << 13; random ^= random >> 7; random ^= random << 17;
                let j = start + (random as usize % (i - start + 1));
                order.swap(i, j);
            }
            self.reorder(&order);
        }
        self.shuffle = shuffle;
        self.revision = next_revision();
    }

    pub fn inherit_preferences(&mut self, previous: &Queue) {
        self.set_repeat(previous.repeat);
        self.set_smart_enabled(previous.smart_enabled);
        self.set_shuffle(previous.shuffle);
    }

    pub fn song_key(item: &Item) -> &str {
        if !item.id.is_empty() { &item.id } else { &item.listen_ref }
    }

    pub fn own_keys(&self) -> Vec<String> {
        let order = self.original.as_ref().unwrap_or(&self.rows);
        let positions: HashMap<_, _> = self.rows.iter().enumerate().map(|(i, id)| (*id, i)).collect();
        order.iter().filter_map(|id| positions.get(id).copied())
            .filter(|i| self.recommendation(*i).is_none())
            .map(|i| Self::song_key(&self.items[i]).to_owned()).collect()
    }

    pub fn own_at(&self) -> Option<&str> {
        (0..=self.index?).rev().find(|i| self.recommendation(*i).is_none())
            .map(|i| Self::song_key(&self.items[i]))
    }

    /// Apply a Hub order only ahead of playback. Buckets retain duplicate occurrences.
    pub fn apply_smart_order(&mut self, own: &[String]) {
        let start = self.index.map_or(0, |i| i + 1);
        let mut order = self.rows[..start].to_vec();
        let mut used: HashSet<u64> = order.iter().copied().collect();
        for key in own {
            for i in start..self.items.len() {
                if Self::song_key(&self.items[i]) == key && used.insert(self.rows[i]) {
                    order.push(self.rows[i]);
                }
            }
        }
        for id in &self.rows[start..] { if used.insert(*id) { order.push(*id); } }
        self.reorder(&order);
    }

    pub fn insert_recommendation(&mut self, after: &str, item: Item, rec: Recommendation) -> bool {
        if self.items.iter().any(|existing| !item.listen_ref.is_empty() && existing.listen_ref == item.listen_ref) { return false; }
        let start = self.index.unwrap_or(0);
        let Some(at) = (start..self.items.len()).find(|i| self.recommendation(*i).is_none() && Self::song_key(&self.items[*i]) == after) else { return false };
        let row = next_revision();
        self.items.insert(at + 1, item);
        self.rows.insert(at + 1, row);
        self.recommendations.insert(row, rec);
        self.revision = next_revision();
        true
    }

    pub fn mark_own(&mut self, row: u64) {
        if let Some(rec) = self.recommendations.remove(&row) {
            if let Some(index) = self.row_index(row) {
                if self.items[index].meta == rec.label { self.items[index].meta.clear(); }
            }
            if let Some(original) = &mut self.original {
                if !original.contains(&row) { original.push(row); }
            }
            self.revision = next_revision();
        }
    }

    /// Positions, not process-local tokens, preserve duplicate identity across restart.
    pub fn modes_snapshot(&self) -> Value {
        json!({
            "repeat": match self.repeat { Repeat::Off => 0, Repeat::All => 1, Repeat::One => 2 },
            "shuffle": match self.shuffle { Shuffle::Off => 0, Shuffle::Ordinary => 1, Shuffle::Smart => 2 },
            "smart_enabled": self.smart_enabled, "context": self.context,
            "original": self.original.as_ref().map(|ids| ids.iter().filter_map(|id| self.row_index(*id)).collect::<Vec<_>>()),
            "recommendations": self.rows.iter().enumerate().filter_map(|(i, id)| self.recommendations.get(id).map(|r| json!({"index": i, "session": r.session, "label": r.label}))).collect::<Vec<_>>()
        })
    }

    pub fn restore_modes(&mut self, value: &Value) {
        self.repeat = match value["repeat"].as_u64() { Some(1) => Repeat::All, Some(2) => Repeat::One, _ => Repeat::Off };
        self.shuffle = match value["shuffle"].as_u64() { Some(1) => Shuffle::Ordinary, Some(2) => Shuffle::Smart, _ => Shuffle::Off };
        self.smart_enabled = value["smart_enabled"].as_bool().unwrap_or(true);
        self.set_context(value["context"].as_str().unwrap_or(""));
        if let Some(indices) = value["original"].as_array() {
            let mut seen = HashSet::new();
            self.original = Some(indices.iter().filter_map(|v| usize::try_from(v.as_u64()?).ok())
                .filter_map(|i| self.rows.get(i).copied()).filter(|id| seen.insert(*id)).collect());
        }
        if let Some(recs) = value["recommendations"].as_array() {
            for rec in recs {
                let Some(row) = rec["index"].as_u64().and_then(|i| usize::try_from(i).ok()).and_then(|i| self.rows.get(i)).copied() else { continue };
                let Some(session) = rec["session"].as_str().filter(|s| !s.is_empty()) else { continue };
                self.recommendations.insert(row, Recommendation { session: session.into(), label: rec["label"].as_str().unwrap_or("Рекомендация").into() });
            }
        }
        if self.shuffle != Shuffle::Off && self.original.is_none() {
            self.original = Some(self.rows.iter().copied().filter(|id| !self.recommendations.contains_key(id)).collect());
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
    fn repeat_boundaries_and_peek_distinguish_manual_next() {
        let mut q = sample();
        q.select(2);
        let token = q.current_token();
        assert!(q.peek_next().is_none());
        assert!(!q.advance());
        assert_eq!(q.current_token(), token);
        q.set_repeat(Repeat::One);
        assert_eq!(q.peek_next(), q.current());
        assert!(q.advance());
        assert_ne!(q.current_token(), token);
        assert!(!q.next());
        q.set_repeat(Repeat::All);
        assert_eq!(q.peek_next().unwrap().id, "a");
        assert!(q.advance());
        assert_eq!(q.index(), Some(0));
        assert!(q.prev());
        assert_eq!(q.index(), Some(2));
        let mut empty = Queue::from_tracks("", vec![]);
        empty.set_repeat(Repeat::All);
        assert!(empty.peek_next().is_none());
        assert!(!empty.advance());
        assert!(!empty.prev());
    }

    #[test]
    fn shuffled_duplicates_traverse_once_and_restore_exact_occurrence() {
        let mut q = Queue::from_tracks("", vec![item("same", "First", 1), item("same", "Second", 2), item("same", "Third", 3)]);
        q.select(1);
        let token = q.current_token();
        q.set_shuffle(Shuffle::Ordinary);
        assert_eq!(q.index(), Some(0));
        assert_eq!(q.current_token(), token);
        assert_eq!(q.current().unwrap().title, "Second");
        let mut visited = HashSet::new();
        visited.insert(q.row_token(0).unwrap());
        while q.next() { assert!(visited.insert(q.row_token(q.index().unwrap()).unwrap())); }
        assert_eq!(visited.len(), 3);
        let last = q.current().unwrap().title.clone();
        q.prev();
        q.next();
        assert_eq!(q.current().unwrap().title, last);
        let current = q.row_token(q.index().unwrap()).unwrap();
        q.set_shuffle(Shuffle::Off);
        assert_eq!(q.items().iter().map(|i| i.title.as_str()).collect::<Vec<_>>(), ["First", "Second", "Third"]);
        assert_eq!(q.row_token(q.index().unwrap()), Some(current));
    }

    #[test]
    fn smart_order_preserves_duplicates_and_pending_only_cleanup() {
        let mut q = Queue::from_tracks("", vec![item("a", "First", 1), item("a", "Duplicate", 2), item("b", "Last", 3)]);
        q.select(0);
        q.set_shuffle(Shuffle::Smart);
        q.apply_smart_order(&["a".into(), "b".into()]);
        assert_eq!(q.items().iter().filter(|i| i.id == "a").count(), 2);
        let rec = Recommendation { session: "session".into(), label: "Recommendation".into() };
        let mut first = item("", "Playing recommendation", 1);
        first.listen_ref = "catalog:first".into();
        assert!(q.insert_recommendation("a", first, rec.clone()));
        q.next();
        let token = q.current_token();
        let mut pending = item("", "Pending recommendation", 1);
        pending.listen_ref = "catalog:pending".into();
        assert!(q.insert_recommendation("b", pending, rec));
        q.extend([item("extra", "Added", 1)]);
        q.set_shuffle(Shuffle::Off);
        assert_eq!(q.current_token(), token);
        assert_eq!(q.current().unwrap().title, "Playing recommendation");
        assert!(!q.items().iter().any(|i| i.title == "Pending recommendation"));
        assert!(q.items().iter().any(|i| i.id == "extra"));
        assert_eq!(q.items().iter().filter(|i| i.id == "a").count(), 2);
    }

    #[test]
    fn modes_roundtrip_preserves_order_and_original_duplicate_positions() {
        let mut q = sample();
        q.select(1);
        q.set_context("playlist:real-id");
        q.set_repeat(Repeat::One);
        q.set_shuffle(Shuffle::Smart);
        q.insert_next(item("b", "Duplicate", 1));
        let modes = q.modes_snapshot();
        let mut restored = Queue::from_tracks(q.name(), q.items().to_vec());
        restored.select(q.index().unwrap());
        restored.restore_modes(&modes);
        assert_eq!(restored.items(), q.items());
        assert_eq!(restored.repeat(), Repeat::One);
        assert_eq!(restored.context(), "playlist:real-id");
        restored.set_shuffle(Shuffle::Off);
        q.set_shuffle(Shuffle::Off);
        assert_eq!(restored.items(), q.items());
        assert_eq!(restored.index(), q.index());
    }


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
