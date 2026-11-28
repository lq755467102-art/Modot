!include "${PROJECT_DIR}\installer\dokan.nsh"

!macro customInstall
  ; The standard upgrade first removes the previous application files.
  ; Check the extracted video runtime before completing setup.
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\python\Lib\site-packages\av.libs\avcodec-63.dll" 0 modot_copy_failed
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\python\Lib\site-packages\av-19.0.2.dist-info\*.*" modot_copy_failed 0
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\python\Lib\site-packages\av.libs\libx264*.dll" modot_copy_failed 0
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\python\Lib\site-packages\av.libs\libx265*.dll" modot_copy_failed 0
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\git\cmd\git.exe" 0 modot_copy_failed
  IfFileExists "$INSTDIR\resources\app.asar.unpacked\dokan\Dokan_x64.msi" modot_copy_done modot_copy_failed
  modot_copy_failed:
    MessageBox MB_OK|MB_ICONSTOP "Could not finish installing runtime files. Re-run the installer." /SD IDOK
    SetErrorLevel 3
    Abort
  modot_copy_done:
    !insertmacro installBundledDokan
!macroend
