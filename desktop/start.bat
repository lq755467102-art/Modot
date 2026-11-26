@echo off
setlocal
cd /d "%~dp0"

if not exist "node_modules\electron\dist\electron.exe" (
  echo.
  echo   Electron binary is missing.
  echo   In this folder, run once:
  echo.
  echo     npm install
  echo.
  pause
  exit /b 1
)

rem The host environment may leave ELECTRON_RUN_AS_NODE=1 set, which turns
rem electron.exe into plain node. Symptom: "Cannot read properties of
rem undefined (reading 'whenReady')" -- looks like a code bug, is not.
set "ELECTRON_RUN_AS_NODE="

start "" "node_modules\electron\dist\electron.exe" "%~dp0."
