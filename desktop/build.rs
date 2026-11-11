fn main() {
    slint_build::compile("ui/app.slint").unwrap();
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
