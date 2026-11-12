#!/usr/bin/env bash
# Package an already-built x64 Windows release; never copy configuration or secrets.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bundle="$root/dist/windows"
makensis=${MAKENSIS:-makensis}
objdump=${OBJDUMP:-objdump}
version=${APP_VERSION:-$(sed -n 's/^version = "\([0-9][0-9.]*\)"$/\1/p' "$root/Cargo.toml" | head -n 1)}
fail() { printf 'Installer: %s\n' "$*" >&2; exit 1; }
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "APP_VERSION must be three numeric components"
command -v "$makensis" >/dev/null || fail "makensis not found; install NSIS or set MAKENSIS (and NSISDIR for a locally extracted distribution)"
command -v "$objdump" >/dev/null || fail "objdump not found; install binutils or set OBJDUMP"
for file in HomeMusic.exe libmpv-2.dll vulkan-1.dll VulkanRT-License.txt; do
    [[ -s "$bundle/$file" ]] || fail "Missing required artifact: $bundle/$file"
done
[[ -s "$root/assets/icon.ico" ]] || fail "Missing application icon"
for file in THIRD-PARTY-NOTICES.txt mpv-Copyright.txt mpv-GPL-2.0.txt mpv-LGPL-2.1.txt \
    FFmpeg-LICENSE.txt FFmpeg-COPYING.GPLv2.txt FFmpeg-COPYING.GPLv3.txt \
    FFmpeg-COPYING.LGPLv2.1.txt FFmpeg-COPYING.LGPLv3.txt; do
    [[ -s "$root/installer/licenses/$file" ]] || fail "Missing runtime notice: $file"
done
command -v sha256sum >/dev/null || fail "sha256sum is required to identify the documented media runtime"
mpv_hash=$(sha256sum "$bundle/libmpv-2.dll")
[[ ${mpv_hash%% *} == 88504ee5560942a955405e99dfb5971368e4cf851321f36565bf2a667271db1b ]] \
    || fail "libmpv-2.dll changed: refresh its provenance and license notices before packaging"

# Recheck the final executable AND both DLLs. New external imports must be
# consciously added to the explicit NSIS payload, not swept in with a wildcard.
for file in HomeMusic.exe libmpv-2.dll vulkan-1.dll; do
    header=$("$objdump" -f "$bundle/$file") || fail "Cannot inspect $file"
    [[ $header == *pei-x86-64* ]] || fail "$file is not a Windows x64 PE artifact"
    imports=$("$objdump" -p "$bundle/$file") || fail "Cannot inspect imports of $file"
    found=false
    while read -r dll; do
        found=true
        case ${dll,,} in
            libmpv-2.dll|vulkan-1.dll) [[ -s "$bundle/${dll,,}" ]] || fail "Missing dependency: $dll" ;;
            api-ms-win-*.dll|ext-ms-win-*.dll) ;; # Windows 10/11 API sets
            advapi32.dll|avicap32.dll|avrt.dll|bcrypt.dll|bcryptprimitives.dll|cfgmgr32.dll|combase.dll|comctl32.dll|crypt32.dll|d2d1.dll|dwmapi.dll|dwrite.dll|gdi32.dll|imm32.dll|iphlpapi.dll|kernel32.dll|msvcrt.dll|ncrypt.dll|normaliz.dll|ntdll.dll|ole32.dll|oleaut32.dll|opengl32.dll|propsys.dll|rpcrt4.dll|secur32.dll|setupapi.dll|shcore.dll|shell32.dll|shlwapi.dll|ucrtbase.dll|uiautomationcore.dll|user32.dll|userenv.dll|uxtheme.dll|version.dll|winmm.dll|wldap32.dll|ws2_32.dll) ;;
            *) fail "Unpackaged non-system dependency of $file: $dll. Review and explicitly add it to the installer before building." ;;
        esac
    done < <(printf '%s\n' "$imports" | sed -n 's/^[[:space:]]*DLL Name: //p')
    "$found" || fail "No DLL imports found in $file; cannot verify dependency closure"
done

# NSIS File statements are an allowlist; dist/windows/config.toml and all other
# user files are intentionally ignored, never staged or embedded.
"$makensis" -V3 "-DAPP_VERSION=$version" "-DBUNDLE_DIR=$bundle" \
    "-DAPP_ICON=$root/assets/icon.ico" "$root/installer/HomeMusic.nsi"
printf 'Installer created: %s/HomeMusic-Setup.exe\n' "$bundle"
