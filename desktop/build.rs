fn main() {
    let manifest = std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    println!(
        "cargo:rerun-if-changed={}",
        manifest.join("assets/icon.png").display()
    );
    slint_build::compile_with_config(
        "ui/app.slint",
        slint_build::CompilerConfiguration::new().with_style("fluent".into()),
    ).unwrap();
    let target = std::env::var("TARGET").unwrap_or_default();
    if target.contains("windows") {
        compile_windows_icon(&manifest);
        link_windows_mpv();
    } else {
        link_host_mpv();
    }
}

fn compile_windows_icon(manifest: &std::path::Path) {
    let ico = manifest.join("assets").join("icon.ico");
    println!("cargo:rerun-if-changed={}", ico.display());
    let out_dir = std::path::PathBuf::from(std::env::var("OUT_DIR").unwrap());
    let rc_path = out_dir.join("icon.rc");
    let obj_path = out_dir.join("icon.o");
    let ico_str = ico.display().to_string().replace('\\', "\\\\");
    std::fs::write(&rc_path, format!("1 ICON \"{ico_str}\"\n")).expect("write icon.rc");
    let windres = windres_program();
    let status = std::process::Command::new(&windres)
        .args(["--output-format", "coff", "--input"])
        .arg(&rc_path)
        .arg("--output")
        .arg(&obj_path)
        .status()
        .unwrap_or_else(|err| panic!("{windres} failed to start: {err}"));
    if !status.success() {
        panic!("{windres} failed to embed {}", ico.display());
    }
    println!("cargo:rustc-link-arg={}", obj_path.display());
}

fn windres_program() -> String {
    if let Ok(path) = std::env::var("WINDRES") {
        if !path.is_empty() {
            return path;
        }
    }
    if let Ok(linker) = std::env::var("CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER") {
        if let Some(dir) = std::path::Path::new(&linker).parent() {
            if dir.as_os_str().is_empty() {
                return linker_to_windres(&linker);
            }
            let file = std::path::Path::new(&linker)
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("");
            return dir.join(linker_to_windres(file)).display().to_string();
        }
        return linker_to_windres(&linker);
    }
    "x86_64-w64-mingw32-windres".into()
}

fn linker_to_windres(linker: &str) -> String {
    if let Some(prefix) = linker.strip_suffix("-gcc") {
        return format!("{prefix}-windres");
    }
    if let Some(prefix) = linker.strip_suffix("gcc") {
        return format!("{prefix}windres");
    }
    "x86_64-w64-mingw32-windres".into()
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
