@echo off
rem Call this file from another launcher. It selects bundled runtimes first and
rem leaves TRILOBITE_PYTHON / TRILOBITE_OLLAMA_EXE in the caller environment.
set "TRILOBITE_RUNTIME_ROOT=%~dp0"
if not defined TRILOBITE_HOME (
  if defined LOCALAPPDATA (
    set "TRILOBITE_HOME=%LOCALAPPDATA%\trilobite"
  ) else (
    set "TRILOBITE_HOME=%USERPROFILE%\.trilobite"
  )
)

set "TRILOBITE_ENGINE_ID=windows-x86_64"
if /I "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "TRILOBITE_ENGINE_ID=windows-arm64"
set "TRILOBITE_ENGINE_ROOT="
if defined TRILOBITE_ENGINE_BUNDLE set "TRILOBITE_ENGINE_ROOT=%TRILOBITE_ENGINE_BUNDLE%"
if not defined TRILOBITE_ENGINE_ROOT if exist "%TRILOBITE_RUNTIME_ROOT%engine\%TRILOBITE_ENGINE_ID%\ENGINE-BUNDLE.json" set "TRILOBITE_ENGINE_ROOT=%TRILOBITE_RUNTIME_ROOT%engine\%TRILOBITE_ENGINE_ID%"
if not defined TRILOBITE_ENGINE_ROOT if exist "%TRILOBITE_RUNTIME_ROOT%engine\ENGINE-BUNDLE.json" set "TRILOBITE_ENGINE_ROOT=%TRILOBITE_RUNTIME_ROOT%engine"
if defined TRILOBITE_ENGINE_ROOT for %%I in ("%TRILOBITE_ENGINE_ROOT%") do if /I "%%~nxI"=="ENGINE-BUNDLE.json" set "TRILOBITE_ENGINE_ROOT=%%~dpI"

set "TRILOBITE_PYTHON="
if defined TRILOBITE_ENGINE_ROOT if exist "%TRILOBITE_ENGINE_ROOT%\runtime\python\python.exe" set "TRILOBITE_PYTHON=%TRILOBITE_ENGINE_ROOT%\runtime\python\python.exe"
if not defined TRILOBITE_PYTHON if exist "%TRILOBITE_RUNTIME_ROOT%venv\Scripts\python.exe" set "TRILOBITE_PYTHON=%TRILOBITE_RUNTIME_ROOT%venv\Scripts\python.exe"
if not defined TRILOBITE_PYTHON for %%P in (python.exe py.exe) do if not defined TRILOBITE_PYTHON (
  where %%P >nul 2>&1
  if not errorlevel 1 set "TRILOBITE_PYTHON=%%P"
)

if defined TRILOBITE_ENGINE_ROOT if exist "%TRILOBITE_ENGINE_ROOT%\runtime\ollama\ollama.exe" (
  set "TRILOBITE_OLLAMA_EXE=%TRILOBITE_ENGINE_ROOT%\runtime\ollama\ollama.exe"
  set "PATH=%TRILOBITE_ENGINE_ROOT%\runtime\ollama;%PATH%"
  if not defined OLLAMA_MODELS set "OLLAMA_MODELS=%TRILOBITE_HOME%\ollama-models"
  set "OLLAMA_NO_CLOUD=1"
)
if not defined TRILOBITE_OLLAMA_EXE set "TRILOBITE_OLLAMA_EXE=ollama"
exit /b 0
