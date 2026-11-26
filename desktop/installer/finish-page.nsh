!macro customHeader
  LangString ModotDesktopShortcut ${LANG_ENGLISH} "Create a desktop shortcut"
  LangString ModotDesktopShortcut ${LANG_SIMPCHINESE} "创建桌面快捷方式"
  LangString ModotDesktopShortcut ${LANG_TRADCHINESE} "建立桌面捷徑"
  LangString ModotShortcutFailed ${LANG_ENGLISH} "The desktop shortcut could not be created. You can open Modot from the Start menu."
  LangString ModotShortcutFailed ${LANG_SIMPCHINESE} "无法创建桌面快捷方式。可以从开始菜单打开 Modot。"
  LangString ModotShortcutFailed ${LANG_TRADCHINESE} "無法建立桌面捷徑。可從開始功能表開啟 Modot。"
!macroend

!macro customFinishPage
  Function ModotStartApp
    ${If} ${isUpdated}
      StrCpy $1 "--updated"
    ${Else}
      StrCpy $1 ""
    ${EndIf}
    ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" "$1"
  FunctionEnd

  Function ModotCreateDesktopShortcut
    ClearErrors
    CreateShortCut "$newDesktopLink" "$appExe" "" "$appExe" 0 "" "" "${APP_DESCRIPTION}"
    ${If} ${Errors}
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(ModotShortcutFailed)"
    ${Else}
      WinShell::SetLnkAUMI "$newDesktopLink" "${APP_ID}"
    ${EndIf}
  FunctionEnd

  !define MUI_FINISHPAGE_RUN
  !define MUI_FINISHPAGE_RUN_FUNCTION ModotStartApp
  # The second optional action on the standard finish page.
  !define MUI_FINISHPAGE_SHOWREADME
  !define MUI_FINISHPAGE_SHOWREADME_FUNCTION ModotCreateDesktopShortcut
  !define MUI_FINISHPAGE_SHOWREADME_TEXT "$(ModotDesktopShortcut)"
  !define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED
  !insertmacro MUI_PAGE_FINISH
!macroend

!macro customUnInstall
  ${IfNot} ${isKeepShortcuts}
    WinShell::UninstShortcut "$oldDesktopLink"
    Delete "$oldDesktopLink"
  ${EndIf}
!macroend
