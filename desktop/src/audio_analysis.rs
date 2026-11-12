//! Bounded, cancellable full-stream EBU R128 analysis, separate from audible playback.
use std::collections::VecDeque;
use std::sync::{atomic::{AtomicU64, Ordering}, mpsc, Arc};
use std::time::{Duration, Instant};
use libmpv2::{events::Event, mpv_end_file_reason, Mpv};

#[derive(Clone, Debug)]
pub struct AudioTrack {
    /// Canonical media identity; never a signed URL or a queue occurrence token.
    pub key: String,
    pub target: String,
    pub cookie: String,
    /// Album artist + album identity. Empty means no album is known.
    pub album: String,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Normalization { #[default] Off, Track, Album }

struct Job { id: u64, key: String, tracks: Vec<AudioTrack> }
struct ResultEntry { id: u64, key: String, gain: Option<f64> }

pub(super) struct Analyzer {
    jobs: mpsc::SyncSender<Job>,
    results: mpsc::Receiver<ResultEntry>,
    generation: Arc<AtomicU64>,
    pending: Option<String>,
    cache: VecDeque<(String, Option<f64>)>,
}

impl Analyzer {
    pub fn new() -> Self {
        let (jobs, input) = mpsc::sync_channel::<Job>(1);
        let (output, results) = mpsc::sync_channel(2);
        let generation = Arc::new(AtomicU64::new(0));
        let cancel = generation.clone();
        std::thread::spawn(move || {
            while let Ok(job) = input.recv() {
                if cancel.load(Ordering::Relaxed) != job.id { continue; }
                let mut measurements = Vec::with_capacity(job.tracks.len());
                for track in &job.tracks {
                    match measure(track, &cancel, job.id) {
                        Some(value) => measurements.push(value),
                        None => break,
                    }
                }
                if cancel.load(Ordering::Relaxed) != job.id { continue; }
                let gain = if measurements.len() == job.tracks.len() {
                    common_gain(&measurements)
                } else { None };
                if output.send(ResultEntry { id: job.id, key: job.key, gain }).is_err() { break; }
            }
        });
        Self { jobs, results, generation, pending: None, cache: VecDeque::new() }
    }

    pub fn cancel(&mut self) {
        self.generation.fetch_add(1, Ordering::Relaxed);
        self.pending = None;
    }

    /// Outer None means pending; inner None means analysis failed (never success at 0 dB).
    pub fn gain(&mut self, key: &str, tracks: &[AudioTrack]) -> Option<Option<f64>> {
        while let Ok(result) = self.results.try_recv() {
            if result.id != self.generation.load(Ordering::Relaxed) { continue; }
            self.pending = None;
            if self.cache.len() == 128 { self.cache.pop_front(); }
            self.cache.push_back((result.key, result.gain));
        }
        if let Some((_, gain)) = self.cache.iter().find(|(cached, _)| cached == key) {
            return Some(*gain);
        }
        if self.pending.as_deref() != Some(key) {
            self.cancel();
            let id = self.generation.load(Ordering::Relaxed);
            if self.jobs.try_send(Job { id, key: key.to_owned(), tracks: tracks.to_vec() }).is_ok() {
                self.pending = Some(key.to_owned());
            }
        }
        None
    }
}

impl Drop for Analyzer {
    fn drop(&mut self) { self.cancel(); }
}

#[derive(Clone, Copy)]
struct Measurement { lufs: f64, peak_db: f64, seconds: f64 }

fn common_gain(values: &[Measurement]) -> Option<f64> {
    let duration: f64 = values.iter().map(|v| v.seconds).sum();
    if values.is_empty() || duration <= 0.0 { return None; }
    // One duration-weighted integrated loudness for the entire album, and one peak
    // ceiling. In particular, never normalize quiet interludes independently.
    let energy: f64 = values.iter().map(|v| 10f64.powf(v.lufs / 10.0) * v.seconds).sum();
    let lufs = 10.0 * (energy / duration).log10();
    let peak = values.iter().map(|v| v.peak_db).fold(f64::NEG_INFINITY, f64::max);
    let gain = (-18.0 - lufs).min(-1.0 - peak).clamp(-24.0, 12.0);
    gain.is_finite().then_some(gain)
}

/// Own the complete native map. mpv 0.37's nested af-metadata GET_TYPE path
/// dereferences NULL tags; reading the map itself avoids that broken API path.
/// libmpv2 exposes GetData but does not provide an owning MPV_FORMAT_NODE type.
pub(crate) struct Metadata(libmpv2_sys::mpv_node);

// SAFETY: get_from_c_void passes storage for precisely mpv_node, with the matching
// format, and only assumes initialization after mpv reports success. Drop releases
// the returned allocation through mpv's own allocator exactly once.
unsafe impl libmpv2::GetData for Metadata {
    fn get_format() -> libmpv2::Format { libmpv2::Format::Node }

    fn get_from_c_void<T, F: FnMut(*mut std::ffi::c_void) -> Result<T, libmpv2::Error>>(
        mut get: F,
    ) -> Result<Self, libmpv2::Error> {
        let mut node = std::mem::MaybeUninit::<libmpv2_sys::mpv_node>::uninit();
        get(node.as_mut_ptr().cast())?;
        Ok(Self(unsafe { node.assume_init() }))
    }
}

impl Metadata {
    pub(crate) fn number(&self, name: &str) -> Option<f64> {
        use libmpv2::mpv_format;
        use std::ffi::CStr;
        if self.0.format != mpv_format::Map { return None; }
        // SAFETY: libmpv owns an immutable, valid node map until our Drop; each
        // union member is read only under its corresponding format discriminator.
        unsafe {
            let list = self.0.u.list.as_ref()?;
            if !(0..=512).contains(&list.num) || list.keys.is_null() || list.values.is_null() {
                return None;
            }
            for index in 0..list.num as usize {
                let key = *list.keys.add(index);
                if key.is_null() || CStr::from_ptr(key).to_bytes() != name.as_bytes() { continue; }
                let value = &*list.values.add(index);
                let number = match value.format {
                    mpv_format::String if !value.u.string.is_null() =>
                        CStr::from_ptr(value.u.string).to_str().ok()?.parse::<f64>().ok()?,
                    mpv_format::Double => value.u.double_,
                    _ => return None,
                };
                return number.is_finite().then_some(number);
            }
        }
        None
    }
}

impl Drop for Metadata {
    fn drop(&mut self) {
        // SAFETY: this node was returned successfully by mpv_get_property and has
        // not been freed or modified since.
        unsafe { libmpv2_sys::mpv_free_node_contents(&mut self.0) };
    }
}

fn measure(track: &AudioTrack, cancel: &AtomicU64, id: u64) -> Option<Measurement> {
    if cancel.load(Ordering::Relaxed) != id { return None; }
    let mpv = Mpv::with_initializer(|init| {
        init.set_option("vid", "no")?;
        init.set_option("audio-display", "no")?;
        init.set_option("ytdl", "no")?;
        init.set_option("msg-level", "all=no")?;
        init.set_option("ao", "null")?;
        init.set_option("ao-null-untimed", "yes")?;
        init.set_option("keep-open", "always")?;
        init.set_option("network-timeout", "15")?;
        init.set_option("af", "@measure:lavfi=[ebur128=metadata=1,astats=metadata=1:reset=0:measure_overall=Peak_level:measure_perchannel=none]")?;
        Ok(())
    }).ok()?;
    super::apply_cookie(&mpv, &track.cookie);
    mpv.command("loadfile", &[&track.target, "replace"]).ok()?;
    let deadline = Instant::now() + Duration::from_secs(180);
    let mut loaded = false;
    while Instant::now() < deadline && cancel.load(Ordering::Relaxed) == id {
        match mpv.wait_event(0.025) {
            Some(Ok(Event::FileLoaded)) => loaded = true,
            Some(Err(_)) | Some(Ok(Event::EndFile(mpv_end_file_reason::Error))) => return None,
            _ => {}
        }
        if loaded && super::flag(&mpv, "eof-reached") == Some(true) {
            let metadata = mpv.get_property::<Metadata>("af-metadata/measure").ok()?;
            let lufs = metadata.number("lavfi.r128.I")?;
            let peak_db = metadata.number("lavfi.astats.Overall.Peak_level")?;
            let seconds = super::prop_f64(&mpv, "duration")?;
            return (seconds > 0.0).then_some(Measurement { lufs, peak_db, seconds });
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn album_gain_is_shared_and_peak_limited() {
        let quiet = Measurement { lufs: -30.0, peak_db: -20.0, seconds: 60.0 };
        let loud = Measurement { lufs: -14.0, peak_db: -2.0, seconds: 240.0 };
        let gain = common_gain(&[quiet, loud]).unwrap();
        assert!(gain < 0.0);
        assert_ne!(gain, common_gain(&[quiet]).unwrap());
        assert!(loud.peak_db + gain <= -1.0);
        assert!((common_gain(&[quiet]).unwrap() - 12.0).abs() < 0.001);
    }

    #[test]
    fn album_loudness_uses_duration_weighted_energy_not_decibel_average() {
        let quiet = Measurement { lufs: -30.0, peak_db: -20.0, seconds: 60.0 };
        let loud = Measurement { lufs: -14.0, peak_db: -2.0, seconds: 240.0 };
        let integrated = 10.0 * ((10f64.powf(-3.0) * 60.0 + 10f64.powf(-1.4) * 240.0) / 300.0).log10();
        assert!((common_gain(&[quiet, loud]).unwrap() - (-18.0 - integrated)).abs() < 1e-10);
        let reversed = common_gain(&[loud, quiet]).unwrap();
        assert!((reversed - common_gain(&[quiet, loud]).unwrap()).abs() < 1e-10);
    }
}
