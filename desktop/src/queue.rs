//! The playing queue. Next and previous stay inside this list.

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
}

impl Queue {
    pub fn from_tracks(name: impl Into<String>, items: Vec<Item>) -> Self {
        Self {
            name: name.into(),
            items,
            index: None,
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

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn index(&self) -> Option<usize> {
        self.index
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
        true
    }

    /// Put a track after the current one and make it current. An idle queue starts with it.
    pub fn extend(&mut self, items: impl IntoIterator<Item = Item>) {
        self.items.extend(items);
    }

    pub fn play_inserted(&mut self, item: Item) -> bool {
        let at = self.index.map(|index| index + 1).unwrap_or(0);
        self.items.insert(at, item);
        self.index = Some(at);
        true
    }

    pub fn next(&mut self) -> bool {
        match self.index {
            Some(index) if index + 1 < self.items.len() => {
                self.index = Some(index + 1);
                true
            }
            Some(_) => false,
            None => self.select(0),
        }
    }

    pub fn prev(&mut self) -> bool {
        match self.index {
            Some(index) if index > 0 => {
                self.index = Some(index - 1);
                true
            }
            _ => false,
        }
    }

    /// `47 треков · 2:55:19`. An unknown duration is an em dash.
    pub fn total_label(&self) -> String {
        if self.items.is_empty() {
            return String::new();
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
        assert_eq!(Queue::from_tracks("n", Vec::new()).total_label(), "");
        assert_eq!(
            Queue::from_path("/tmp/a.wav", "a", "WAV").total_label(),
            "1 трек · —"
        );
    }
}
