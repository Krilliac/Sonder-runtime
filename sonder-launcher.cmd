@echo off
setlocal
set "REPO=%~dp0"
call "%REPO%sonder-runtime.cmd"
if not defined SONDER_PYTHON (
  echo [sonder-launcher] ERROR: no Python runtime found.
  exit /b 3
)
if not defined SONDER_LAUNCHER_HOST set "SONDER_LAUNCHER_HOST=127.0.0.1"
if not defined SONDER_LAUNCHER_PORT set "SONDER_LAUNCHER_PORT=11436"
"%SONDER_PYTHON%" "%REPO%sonder_launcher.py" %*
exit /b %ERRORLEVEL%
