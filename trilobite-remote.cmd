@echo off
setlocal
set "REPO=%~dp0"
set "PYTHON=python"
if exist "%REPO%venv\Scripts\python.exe" (
  "%REPO%venv\Scripts\python.exe" --version >nul 2>&1
  if not errorlevel 1 set "PYTHON=%REPO%venv\Scripts\python.exe"
)
"%PYTHON%" "%REPO%trilobite_client.py" %*
endlocal
