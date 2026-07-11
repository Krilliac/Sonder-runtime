@echo off
setlocal
set "REPO=%~dp0"
call "%REPO%trilobite-runtime.cmd"
if not defined TRILOBITE_HOST set "TRILOBITE_HOST=127.0.0.1"
if not defined TRILOBITE_PORT set "TRILOBITE_PORT=11435"
if not defined TRILOBITE_CONTEXT_SIZE set "TRILOBITE_CONTEXT_SIZE=8192"
if not defined LOCAL_LLM_NUM_THREAD set "LOCAL_LLM_NUM_THREAD=%NUMBER_OF_PROCESSORS%"
if not defined LOCAL_LLM_NUM_GPU set "LOCAL_LLM_NUM_GPU=999"
if not defined LOCAL_LLM_NUM_BATCH set "LOCAL_LLM_NUM_BATCH=512"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"
if not defined TRILOBITE_PYTHON (
  echo [trilobite] ERROR: no bundled or system Python runtime was found.
  endlocal & exit /b 3
)

if /I not "%TRILOBITE_TERMINAL_BOOTSTRAP%"=="0" (
  "%TRILOBITE_OLLAMA_EXE%" list >nul 2>&1
  if errorlevel 1 (
    echo [trilobite] starting local engine bootstrap...
    "%TRILOBITE_PYTHON%" "%REPO%bootstrap_engine.py"
  ) else (
    "%TRILOBITE_OLLAMA_EXE%" list 2>nul | findstr /i "trilobite" >nul
    if errorlevel 1 (
      echo [trilobite] bootstrapping engine ^(first run^)...
      "%TRILOBITE_PYTHON%" "%REPO%bootstrap_engine.py"
    )
  )
)

if /I not "%TRILOBITE_TERMINAL_START_SERVER%"=="0" (
  echo [trilobite] ensuring local API server is running...
  "%TRILOBITE_PYTHON%" "%REPO%trilobite_headless.py" start --host "%TRILOBITE_HOST%" --port "%TRILOBITE_PORT%" --context-size "%TRILOBITE_CONTEXT_SIZE%"
)

if defined TRILOBITE_SERVER (
  if /I not "%TRILOBITE_TERMINAL_REMOTE%"=="0" (
    "%TRILOBITE_PYTHON%" "%REPO%trilobite_client.py" %*
    endlocal
    exit /b %ERRORLEVEL%
  )
)

"%TRILOBITE_PYTHON%" "%REPO%trilobite_repl.py" %*
endlocal
