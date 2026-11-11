//! Where `config.toml` and `window-place.toml` live.
//!
//! `cargo run` and `cargo test` put the binary under `target/`, so those
//! still read the source folder. A copied exe reads the folder it sits in.

use std::path::{Component, Path, PathBuf};

pub fn dir() -> PathBuf {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    match std::env::current_exe() {
        Ok(exe) => dir_for(&exe, &manifest),
        Err(_) => manifest,
    }
}

pub fn dir_for(exe: &Path, manifest: &Path) -> PathBuf {
    let under_target = exe
        .components()
        .any(|component| matches!(component, Component::Normal(name) if name == "target"));
    if under_target {
        manifest.to_path_buf()
    } else {
        exe.parent().unwrap_or(manifest).to_path_buf()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cargo_target_keeps_the_source_folder() {
        let manifest = Path::new("/src/desktop");
        let exe = Path::new("/src/desktop/target/debug/homemusic-desktop");
        assert_eq!(dir_for(exe, manifest), manifest);
        let deps = Path::new("/src/desktop/target/debug/deps/homemusic_desktop-hash");
        assert_eq!(dir_for(deps, manifest), manifest);
    }

    #[test]
    fn a_copied_exe_reads_its_own_folder() {
        let manifest = Path::new("/src/desktop");
        let exe = Path::new("/opt/HomeMusic/HomeMusic.exe");
        assert_eq!(dir_for(exe, manifest), Path::new("/opt/HomeMusic"));
    }

    #[test]
    fn target_inside_a_longer_name_does_not_count() {
        let manifest = Path::new("/src/desktop");
        let exe = Path::new("/opt/target-player/HomeMusic.exe");
        assert_eq!(dir_for(exe, manifest), Path::new("/opt/target-player"));
    }
}
