@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0"
if not exist "%~dp0desktop\node_modules\electron\dist\electron.exe" (
  echo Electron is missing. Run npm ci in the desktop folder first.
  popd
  if /i not "%~1"=="--smoke" pause
  exit /b 1
)
rem Use the installed Electron as Node for the build, then start its GUI normally.
set "ELECTRON_RUN_AS_NODE=1"
"%~dp0desktop\node_modules\electron\dist\electron.exe" "%~dp0desktop\preview.cjs" %*
set "MODOT_PREVIEW_EXIT=%errorlevel%"
popd
if not "%MODOT_PREVIEW_EXIT%"=="0" if /i not "%~1"=="--smoke" pause
exit /b %MODOT_PREVIEW_EXIT%
