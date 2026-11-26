@echo off
setlocal DisableDelayedExpansion

if not exist "%~dp0renderer-v2\dist\index.html" (
  echo Frontend build is missing.
  echo Build it once with "npm run build:v2" in the desktop folder, then try again.
  pause
  exit /b 1
)

set "TEST_RUNTIME=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies"
if not defined HARNESS_PYTHON if exist "%TEST_RUNTIME%\python\python.exe" set "HARNESS_PYTHON=%TEST_RUNTIME%\python\python.exe"
if not defined HARNESS_SANDBOX_PYTHON_ROOT if exist "%TEST_RUNTIME%\python\Lib\os.py" set "HARNESS_SANDBOX_PYTHON_ROOT=%TEST_RUNTIME%\python"
if not defined HARNESS_GIT if exist "%TEST_RUNTIME%\native\git\cmd\git.exe" set "HARNESS_GIT=%TEST_RUNTIME%\native\git\cmd\git.exe"

set "HARNESS_DATA=%LOCALAPPDATA%\my-agent-dev-test\data"
set "HARNESS_USER_DATA=%LOCALAPPDATA%\my-agent-dev-test\profile"
set "HARNESS_PORT=18788"

call "%~dp0start.bat"
exit /b %errorlevel%
