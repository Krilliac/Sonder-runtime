@echo off
setlocal
set "REPO=%~dp0"
call "%REPO%trilobite-runtime.cmd"
if not defined TRILOBITE_PYTHON (
  echo [trilobite-launcher] ERROR: no Python runtime found.
  exit /b 3
)
if not defined TRILOBITE_LAUNCHER_HOST set "TRILOBITE_LAUNCHER_HOST=127.0.0.1"
if not defined TRILOBITE_LAUNCHER_PORT set "TRILOBITE_LAUNCHER_PORT=11436"
"%TRILOBITE_PYTHON%" "%REPO%trilobite_launcher.py" %*
exit /b %ERRORLEVEL%
