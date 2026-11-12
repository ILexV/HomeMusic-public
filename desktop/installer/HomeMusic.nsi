Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"

!ifndef APP_VERSION
  !error "APP_VERSION must be supplied by build-installer.sh"
!endif
!ifndef BUNDLE_DIR
  !error "BUNDLE_DIR must point to the Windows distribution"
!endif
!ifndef APP_ICON
  !error "APP_ICON must point to assets/icon.ico"
!endif

Name "HomeMusic"
OutFile "${BUNDLE_DIR}/HomeMusic-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\HomeMusic"
InstallDirRegKey HKCU "Software\HomeMusic" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetOverwrite on
BrandingText "HomeMusic"
Icon "${APP_ICON}"
UninstallIcon "${APP_ICON}"
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=1049 "ProductName" "HomeMusic"
VIAddVersionKey /LANG=1049 "FileDescription" "Установка HomeMusic"
VIAddVersionKey /LANG=1049 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1049 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1049 "LegalCopyright" "HomeMusic contributors"

!define MUI_ABORTWARNING
!define MUI_ICON "${APP_ICON}"
!define MUI_UNICON "${APP_ICON}"
!define MUI_WELCOMEPAGE_TEXT "Установка HomeMusic для текущего пользователя без прав администратора.$\r$\n$\r$\nПеред обновлением закройте HomeMusic. Настройки подключения, пароли и личные данные не входят в установщик и сохраняются при обновлении.$\r$\n$\r$\nТребуется Windows 10/11 (64 бит)."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\HomeMusic.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Запустить HomeMusic"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!insertmacro MUI_PAGE_FINISH
!define MUI_UNCONFIRMPAGE_TEXT_TOP "Удалить программу HomeMusic?$\r$\n$\r$\nНастройки, сохранённые пароли, состояние проигрывателя и офлайн-музыка останутся на компьютере. Удаляются только файлы программы и её ярлыки."
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "Russian"

Function .onInit
  SetShellVarContext current
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "HomeMusic требует 64-разрядную Windows 10 или новее."
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_OK|MB_ICONSTOP "HomeMusic требует Windows 10 или новее."
    Abort
  ${EndIf}
  SetRegView 64
FunctionEnd

Function un.onInit
  SetShellVarContext current
  SetRegView 64
FunctionEnd

; Check before replacing/removing any runtime file. Never terminate the user's player.
!macro CHECK_RUNTIME PREFIX
Function ${PREFIX}CheckRuntime
  Push $0
  Push $1
  Push $2
  StrCpy $2 0
  next_file:
    ${If} $2 == 0
      StrCpy $0 "$INSTDIR\HomeMusic.exe"
    ${ElseIf} $2 == 1
      StrCpy $0 "$INSTDIR\libmpv-2.dll"
    ${Else}
      StrCpy $0 "$INSTDIR\vulkan-1.dll"
    ${EndIf}
  retry_file:
    IfFileExists "$0" 0 checked_file
    System::Call 'kernel32::CreateFileW(w r0, i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .r1'
    ${If} $1 == -1
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Не удалось открыть файл программы:$\r$\n$0$\r$\n$\r$\nЗакройте HomeMusic и повторите попытку. Также убедитесь, что папка доступна для записи." IDRETRY retry_file
      Abort
    ${EndIf}
    System::Call 'kernel32::CloseHandle(p r1)'
  checked_file:
    IntOp $2 $2 + 1
    IntCmp $2 3 done next_file done
  done:
  Pop $2
  Pop $1
  Pop $0
FunctionEnd
!macroend
!insertmacro CHECK_RUNTIME ""
!insertmacro CHECK_RUNTIME "un."

Section "HomeMusic (обязательно)" MainSection
  SectionIn RO
  Call CheckRuntime
  SetOutPath "$INSTDIR"
  ; Deliberate allowlist: NEVER use File /r or bundle the distribution directory.
  File "${BUNDLE_DIR}/HomeMusic.exe"
  File "${BUNDLE_DIR}/libmpv-2.dll"
  File "${BUNDLE_DIR}/vulkan-1.dll"
  File "${BUNDLE_DIR}/VulkanRT-License.txt"
  File /oname=HomeMusic.ico "${APP_ICON}"
  SetOutPath "$INSTDIR\licenses"
  File "licenses/THIRD-PARTY-NOTICES.txt"
  File "licenses/mpv-Copyright.txt"
  File "licenses/mpv-GPL-2.0.txt"
  File "licenses/mpv-LGPL-2.1.txt"
  File "licenses/FFmpeg-LICENSE.txt"
  File "licenses/FFmpeg-COPYING.GPLv2.txt"
  File "licenses/FFmpeg-COPYING.GPLv3.txt"
  File "licenses/FFmpeg-COPYING.LGPLv2.1.txt"
  File "licenses/FFmpeg-COPYING.LGPLv3.txt"
  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\HomeMusic" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\HomeMusic" "DisplayName" "HomeMusic"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\HomeMusic" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\HomeMusic" "Publisher" "HomeMusic contributors"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\HomeMusic" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\HomeMusic" "DisplayIcon" "$INSTDIR\HomeMusic.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\HomeMusic" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\HomeMusic" "QuietUninstallString" '$\"$INSTDIR\Uninstall.exe$\" /S'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\HomeMusic" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\HomeMusic" "NoRepair" 1
  CreateDirectory "$SMPROGRAMS\HomeMusic"
  CreateShortcut "$SMPROGRAMS\HomeMusic\HomeMusic.lnk" "$INSTDIR\HomeMusic.exe" "" "$INSTDIR\HomeMusic.ico"
  CreateShortcut "$SMPROGRAMS\HomeMusic\Удалить HomeMusic.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section /o "Ярлык на рабочем столе" DesktopSection
  CreateShortcut "$DESKTOP\HomeMusic.lnk" "$INSTDIR\HomeMusic.exe" "" "$INSTDIR\HomeMusic.ico"
SectionEnd

Section "Uninstall"
  Call un.CheckRuntime
  Delete "$INSTDIR\HomeMusic.exe"
  Delete "$INSTDIR\libmpv-2.dll"
  Delete "$INSTDIR\vulkan-1.dll"
  Delete "$INSTDIR\VulkanRT-License.txt"
  Delete "$INSTDIR\HomeMusic.ico"
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$INSTDIR\licenses\THIRD-PARTY-NOTICES.txt"
  Delete "$INSTDIR\licenses\mpv-Copyright.txt"
  Delete "$INSTDIR\licenses\mpv-GPL-2.0.txt"
  Delete "$INSTDIR\licenses\mpv-LGPL-2.1.txt"
  Delete "$INSTDIR\licenses\FFmpeg-LICENSE.txt"
  Delete "$INSTDIR\licenses\FFmpeg-COPYING.GPLv2.txt"
  Delete "$INSTDIR\licenses\FFmpeg-COPYING.GPLv3.txt"
  Delete "$INSTDIR\licenses\FFmpeg-COPYING.LGPLv2.1.txt"
  Delete "$INSTDIR\licenses\FFmpeg-COPYING.LGPLv3.txt"
  RMDir "$INSTDIR\licenses"
  Delete "$SMPROGRAMS\HomeMusic\HomeMusic.lnk"
  Delete "$SMPROGRAMS\HomeMusic\Удалить HomeMusic.lnk"
  RMDir "$SMPROGRAMS\HomeMusic"
  Delete "$DESKTOP\HomeMusic.lnk"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\HomeMusic"
  DeleteRegValue HKCU "Software\HomeMusic" "InstallDir"
  DeleteRegKey /ifempty HKCU "Software\HomeMusic"
  ; Nonrecursive: config/state and any unknown personal files keep this directory alive.
  RMDir "$INSTDIR"
SectionEnd
