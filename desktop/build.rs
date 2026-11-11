fn main() {
    slint_build::compile("ui/app.slint").unwrap();
    let target = std::env::var("TARGET").unwrap_or_default();
    if target.contains("windows") {
        link_windows_mpv();
    } else {
        link_host_mpv();
    }
}

fn link_host_mpv() {
    // libmpv2-sys only emits `-lmpv`. pkg-config supplies the search path of
    // whatever libmpv is installed here, without baking a machine path into git.
    let mpv = pkg_config::Config::new()
        .atleast_version("2.0")
        .probe("mpv")
        .expect("libmpv (pkg-config name `mpv`) is required");
    for path in &mpv.link_paths {
        let path = path.display();
        println!("cargo:rustc-link-arg=-Wl,-rpath-link,{path}");
        // Old DT_RPATH is also searched for libmpv's own dependencies. DT_RUNPATH is not,
        // so a libmpv that lives outside the system library path would fail to load.
        println!("cargo:rustc-link-arg=-Wl,--disable-new-dtags,-rpath,{path}");
    }
}

fn link_windows_mpv() {
    // The GNU rpath flags above are for the Linux ELF. A Windows exe loads
    // libmpv-2.dll from its own folder; the link only needs the import library.
    println!("cargo:rerun-if-env-changed=MPV_LIB_DIR");
    let manifest = std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let mut dirs = Vec::new();
    if let Ok(dir) = std::env::var("MPV_LIB_DIR") {
        if !dir.is_empty() {
            dirs.push(std::path::PathBuf::from(dir));
        }
    }
    dirs.push(manifest.join("windows").join("lib"));
    for dir in &dirs {
        let import = dir.join("libmpv.dll.a");
        if import.is_file() {
            println!("cargo:rerun-if-changed={}", import.display());
            println!("cargo:rustc-link-search=native={}", dir.display());
            return;
        }
    }
    panic!(
        "Windows build needs libmpv.dll.a in MPV_LIB_DIR or desktop/windows/lib. \
         libmpv-2.dll then sits next to the exe."
    );
}
