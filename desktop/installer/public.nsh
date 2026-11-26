!macro customInit
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=prepare-components.ps1 "${PROJECT_DIR}\installer\prepare-components.ps1"
  File /oname=runtime-assets.json "${PROJECT_DIR}\installer\runtime-assets.json"
  DetailPrint "Preparing checksum-verified MinGit..."
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\prepare-components.ps1" -Stage "$PLUGINSDIR\runtime"'
  Pop $0
  StrCmp $0 "0" modot_prepared
    MessageBox MB_OK|MB_ICONSTOP "Could not download or verify MinGit. Check your connection or use the offline cache described in the installation guide." /SD IDOK
    SetErrorLevel 2
    Quit
  modot_prepared:
!macroend

!macro customInstall
  ; The standard upgrade first removes the previous application files.
  ; Check the extracted video runtime before completing setup.
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\python\Lib\site-packages\av.libs\avcodec-63.dll" 0 modot_copy_failed
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\python\Lib\site-packages\av-19.0.2.dist-info\*.*" modot_copy_failed 0
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\python\Lib\site-packages\av.libs\libx264*.dll" modot_copy_failed 0
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\python\Lib\site-packages\av.libs\libx265*.dll" modot_copy_failed 0
  ClearErrors
  CreateDirectory "$INSTDIR\resources\app.asar.unpacked\git"
  CopyFiles /SILENT "$PLUGINSDIR\runtime\git\*.*" "$INSTDIR\resources\app.asar.unpacked\git"
  IfErrors modot_copy_failed
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\git\cmd\git.exe" modot_copy_done modot_copy_failed
  modot_copy_failed:
    MessageBox MB_OK|MB_ICONSTOP "Could not finish installing runtime files. Re-run the installer." /SD IDOK
    SetErrorLevel 3
    Abort
  modot_copy_done:
!macroend
