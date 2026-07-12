@echo off
rem Call this file from another launcher. It selects bundled runtimes first and
rem leaves SONDER_PYTHON / SONDER_OLLAMA_EXE in the caller environment.
set "SONDER_RUNTIME_ROOT=%~dp0"
if not defined SONDER_HOME (
  if defined LOCALAPPDATA (
    set "SONDER_HOME=%LOCALAPPDATA%\sonder"
  ) else (
    set "SONDER_HOME=%USERPROFILE%\.sonder"
  )
)

set "SONDER_ENGINE_ID=windows-x86_64"
if /I "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "SONDER_ENGINE_ID=windows-arm64"
set "SONDER_ENGINE_ROOT="
if defined SONDER_ENGINE_BUNDLE set "SONDER_ENGINE_ROOT=%SONDER_ENGINE_BUNDLE%"
if not defined SONDER_ENGINE_ROOT if exist "%SONDER_RUNTIME_ROOT%engine\%SONDER_ENGINE_ID%\ENGINE-BUNDLE.json" set "SONDER_ENGINE_ROOT=%SONDER_RUNTIME_ROOT%engine\%SONDER_ENGINE_ID%"
if not defined SONDER_ENGINE_ROOT if exist "%SONDER_RUNTIME_ROOT%engine\ENGINE-BUNDLE.json" set "SONDER_ENGINE_ROOT=%SONDER_RUNTIME_ROOT%engine"
if defined SONDER_ENGINE_ROOT for %%I in ("%SONDER_ENGINE_ROOT%") do if /I "%%~nxI"=="ENGINE-BUNDLE.json" set "SONDER_ENGINE_ROOT=%%~dpI"

set "SONDER_PYTHON="
if defined SONDER_ENGINE_ROOT if exist "%SONDER_ENGINE_ROOT%\runtime\python\python.exe" set "SONDER_PYTHON=%SONDER_ENGINE_ROOT%\runtime\python\python.exe"
if not defined SONDER_PYTHON if exist "%SONDER_RUNTIME_ROOT%venv\Scripts\python.exe" set "SONDER_PYTHON=%SONDER_RUNTIME_ROOT%venv\Scripts\python.exe"
if not defined SONDER_PYTHON for %%P in (python.exe py.exe) do if not defined SONDER_PYTHON (
  where %%P >nul 2>&1
  if not errorlevel 1 set "SONDER_PYTHON=%%P"
)

if defined SONDER_ENGINE_ROOT if exist "%SONDER_ENGINE_ROOT%\runtime\ollama\ollama.exe" (
  set "SONDER_OLLAMA_EXE=%SONDER_ENGINE_ROOT%\runtime\ollama\ollama.exe"
  set "PATH=%SONDER_ENGINE_ROOT%\runtime\ollama;%PATH%"
  if not defined OLLAMA_MODELS set "OLLAMA_MODELS=%SONDER_HOME%\ollama-models"
  set "OLLAMA_NO_CLOUD=1"
)
if not defined SONDER_OLLAMA_EXE set "SONDER_OLLAMA_EXE=ollama"
exit /b 0
