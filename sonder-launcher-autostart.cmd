@echo off
setlocal
set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Sonder Launcher.cmd"
if /I "%~1"=="uninstall" (
  if exist "%TARGET%" del /q "%TARGET%"
  echo Removed Sonder launcher startup entry.
  exit /b 0
)
if not defined SONDER_LAUNCHER_TOKEN (
  echo ERROR: Set a strong per-user SONDER_LAUNCHER_TOKEN first.
  echo Example token generator: py "%~dp0sonder_launcher.py" --generate-token
  exit /b 2
)
>"%TARGET%" echo @echo off
>>"%TARGET%" echo start "" /min "%~dp0sonder-launcher.cmd" --host 0.0.0.0
echo Installed: %TARGET%
echo The token remains in your user environment and was not copied into the startup file.
exit /b 0
