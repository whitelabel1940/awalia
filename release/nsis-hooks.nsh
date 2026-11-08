; Tauri's currentUser default is $LOCALAPPDATA\Awalia, which is our DATA
; directory. Move the default before showing pages; cover silent installs too.
!define MUI_CUSTOMFUNCTION_GUIINIT AwaliaDefaultInstallDirectory

Function AwaliaDefaultInstallDirectory
  StrCmp $INSTDIR "$LOCALAPPDATA\Awalia" 0 done
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Awalia"
  done:
FunctionEnd

!macro NSIS_HOOK_PREINSTALL
  Call AwaliaDefaultInstallDirectory
  SetOutPath $INSTDIR
!macroend

; Before the uninstaller removes its files, confirm app/ and runtime/ are ours
; (a user may have chosen an existing folder that has its own "app" folder).
Var AwaliaOwnsApp
Var AwaliaOwnsRuntime
!macro NSIS_HOOK_PREUNINSTALL
  StrCpy $AwaliaOwnsApp 0
  StrCpy $AwaliaOwnsRuntime 0
  IfFileExists "$INSTDIR\app\cbs\web\__main__.py" 0 +2
    StrCpy $AwaliaOwnsApp 1
  IfFileExists "$INSTDIR\runtime\python314._pth" 0 +2
    StrCpy $AwaliaOwnsRuntime 1
!macroend

; tauri-bundler 2.10.3 runs this after deleting installed files and its own
; bundle-ID app data. /UPDATE normally overwrites without an old uninstall;
; skip all extra cleanup in update mode even if an uninstaller is invoked.
!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $UpdateMode <> 1
    ; Only these bundled trees may contain runtime-generated Python caches.
    ${If} $AwaliaOwnsApp = 1
      RMDir /r "$INSTDIR\app"
    ${EndIf}
    ${If} $AwaliaOwnsRuntime = 1
      RMDir /r "$INSTDIR\runtime"
    ${EndIf}
    RMDir "$INSTDIR"

    ; The confirmation page alone sets this state. It stays unset for /S
    ; (and /P), so an unattended uninstall must preserve user data.
    ${If} $DeleteAppDataCheckboxState = 1
      SetShellVarContext current
      RMDir /r "$LOCALAPPDATA\Awalia"
    ${EndIf}
  ${EndIf}
!macroend
