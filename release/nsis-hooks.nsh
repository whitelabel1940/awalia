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
