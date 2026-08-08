@echo off
setlocal
set "REPO=%~dp0"
set "PYTHON=%SONDER_PYTHON%"
if not defined PYTHON set "PYTHON=python"
if not defined SONDER_PYTHON if exist "%REPO%venv\Scripts\python.exe" (
  "%REPO%venv\Scripts\python.exe" --version >nul 2>&1
  if not errorlevel 1 set "PYTHON=%REPO%venv\Scripts\python.exe"
)
"%PYTHON%" "%REPO%sonder_client.py" %*
endlocal
