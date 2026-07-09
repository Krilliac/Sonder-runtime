@echo off
setlocal
set "REPO=%~dp0"
if not defined TRILOBITE_HOME (
  if defined LOCALAPPDATA (
    set "TRILOBITE_HOME=%LOCALAPPDATA%\trilobite"
  ) else (
    set "TRILOBITE_HOME=%USERPROFILE%\.trilobite"
  )
)
if not defined TRILOBITE_HOST set "TRILOBITE_HOST=127.0.0.1"
if not defined TRILOBITE_PORT set "TRILOBITE_PORT=11435"
if not defined TRILOBITE_CONTEXT_SIZE set "TRILOBITE_CONTEXT_SIZE=8192"
if not defined LOCAL_LLM_NUM_THREAD set "LOCAL_LLM_NUM_THREAD=%NUMBER_OF_PROCESSORS%"
if not defined LOCAL_LLM_NUM_GPU set "LOCAL_LLM_NUM_GPU=999"
if not defined LOCAL_LLM_NUM_BATCH set "LOCAL_LLM_NUM_BATCH=512"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"
set "PYTHON=python"
if exist "%REPO%venv\Scripts\python.exe" (
  "%REPO%venv\Scripts\python.exe" --version >nul 2>&1
  if not errorlevel 1 set "PYTHON=%REPO%venv\Scripts\python.exe"
)

if /I not "%TRILOBITE_TERMINAL_BOOTSTRAP%"=="0" (
  ollama list >nul 2>&1
  if errorlevel 1 (
    echo [trilobite] starting local engine bootstrap...
    "%PYTHON%" "%REPO%bootstrap_engine.py"
  ) else (
    ollama list 2>nul | findstr /i "trilobite" >nul
    if errorlevel 1 (
      echo [trilobite] bootstrapping engine ^(first run^)...
      "%PYTHON%" "%REPO%bootstrap_engine.py"
    )
  )
)

if /I not "%TRILOBITE_TERMINAL_START_SERVER%"=="0" (
  echo [trilobite] ensuring local API server is running...
  "%PYTHON%" "%REPO%trilobite_headless.py" start --host "%TRILOBITE_HOST%" --port "%TRILOBITE_PORT%" --context-size "%TRILOBITE_CONTEXT_SIZE%"
)

if defined TRILOBITE_SERVER (
  if /I not "%TRILOBITE_TERMINAL_REMOTE%"=="0" (
    "%PYTHON%" "%REPO%trilobite_client.py" %*
    endlocal
    exit /b %ERRORLEVEL%
  )
)

"%PYTHON%" "%REPO%trilobite_repl.py" %*
endlocal
