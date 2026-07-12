@echo off
setlocal
set "REPO=%~dp0"
call "%REPO%sonder-runtime.cmd"
if not defined SONDER_HOST set "SONDER_HOST=127.0.0.1"
if not defined SONDER_PORT set "SONDER_PORT=11435"
if not defined SONDER_CONTEXT_SIZE set "SONDER_CONTEXT_SIZE=8192"
if not defined SONDER_NUM_THREAD set "SONDER_NUM_THREAD=%NUMBER_OF_PROCESSORS%"
if not defined SONDER_NUM_GPU set "SONDER_NUM_GPU=999"
if not defined SONDER_NUM_BATCH set "SONDER_NUM_BATCH=512"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"
if not defined SONDER_PYTHON (
  echo [sonder] ERROR: no bundled or system Python runtime was found.
  endlocal & exit /b 3
)

if /I not "%SONDER_TERMINAL_BOOTSTRAP%"=="0" (
  "%SONDER_OLLAMA_EXE%" list >nul 2>&1
  if errorlevel 1 (
    echo [sonder] starting local engine bootstrap...
    "%SONDER_PYTHON%" "%REPO%bootstrap_engine.py"
  ) else (
    "%SONDER_OLLAMA_EXE%" list 2>nul | findstr /i "sonder" >nul
    if errorlevel 1 (
      echo [sonder] bootstrapping engine ^(first run^)...
      "%SONDER_PYTHON%" "%REPO%bootstrap_engine.py"
    )
  )
)

if /I not "%SONDER_TERMINAL_START_SERVER%"=="0" (
  echo [sonder] ensuring local API server is running...
  "%SONDER_PYTHON%" "%REPO%sonder_headless.py" start --host "%SONDER_HOST%" --port "%SONDER_PORT%" --context-size "%SONDER_CONTEXT_SIZE%"
)

if defined SONDER_SERVER (
  if /I not "%SONDER_TERMINAL_REMOTE%"=="0" (
    "%SONDER_PYTHON%" "%REPO%sonder_client.py" %*
    endlocal
    exit /b %ERRORLEVEL%
  )
)

"%SONDER_PYTHON%" "%REPO%sonder_repl.py" %*
endlocal
