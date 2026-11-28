!ifndef MODOT_DOKAN_COMPONENT
!define MODOT_DOKAN_COMPONENT
!define MODOT_RUNTIME "$INSTDIR\resources\app.asar.unpacked"
!define MODOT_DOKAN_COMMAND '"${MODOT_RUNTIME}\python\python.exe" -I -B "${MODOT_RUNTIME}\dokan\dokan-component.py"'

!macro modotDriverText en zh tw
  StrCpy $0 "${en}"
  ${If} $LANGUAGE == 2052
    StrCpy $0 "${zh}"
  ${ElseIf} $LANGUAGE == 1028
    StrCpy $0 "${tw}"
  ${EndIf}
!macroend

!macro installBundledDokan
  IfSilent modot_driver_done
  nsExec::ExecToLog '${MODOT_DOKAN_COMMAND} status'
  Pop $0
  StrCmp $0 "0" modot_driver_done
  StrCmp $0 "1" 0 modot_driver_later
  !insertmacro modotDriverText "Project commands use the Dokan 2 driver. Install it now from this installer? Windows may ask for administrator approval. Choose No to install it later from a failed command's log." "项目命令需要 Dokan 2 驱动。现在从本安装包安装吗？Windows 可能要求管理员确认。选择“否”后也可以在会话的命令日志里补装。" "專案命令需要 Dokan 2 驅動。現在從本安裝程式安裝嗎？Windows 可能要求系統管理員確認。選擇「否」之後也可以從對話的命令記錄安裝。"
  MessageBox MB_YESNO|MB_ICONQUESTION "$0" IDNO modot_driver_done
  nsExec::ExecToLog '${MODOT_DOKAN_COMMAND} install'
  Pop $0
  StrCmp $0 "0" modot_driver_done
  StrCmp $0 "2" modot_driver_done
  StrCmp $0 "3" 0 modot_driver_later
  !insertmacro modotDriverText "Restart Windows to finish installing Dokan 2 before using project commands." "请重新启动 Windows 以完成 Dokan 2 安装，再运行项目命令。" "請重新啟動 Windows 以完成 Dokan 2 安裝，再執行專案命令。"
  MessageBox MB_OK|MB_ICONINFORMATION "$0"
  Goto modot_driver_done
  modot_driver_later:
    !insertmacro modotDriverText "Modot is installed. The driver is not ready. Retry from the project's command log after Windows Installer finishes, or repair Dokan 2 in Windows Settings." "Modot 已安装。驱动尚未就绪。可等待 Windows 安装程序结束后从会话命令日志重试，或在 Windows 设置中修复 Dokan 2。" "Modot 已安裝。驅動程式尚未就緒。可待 Windows 安裝程式結束後從命令記錄重試，或在 Windows 設定中修復 Dokan 2。"
    MessageBox MB_OK|MB_ICONINFORMATION "$0"
  modot_driver_done:
!macroend
!endif
