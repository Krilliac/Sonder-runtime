@echo off
setlocal
set "REPO=%~dp0"
call "%REPO%sonder-runtime.cmd"

if not defined SONDER_NUM_THREAD set "SONDER_NUM_THREAD=%NUMBER_OF_PROCESSORS%"
if not defined SONDER_NUM_GPU set "SONDER_NUM_GPU=999"
if not defined SONDER_NUM_BATCH set "SONDER_NUM_BATCH=512"
if not defined SONDER_CODE set "SONDER_CODE=qwen2.5-coder:7b"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"

if not defined SONDER_ENDLESS_TOTAL set "SONDER_ENDLESS_TOTAL=30"
if not defined SONDER_ENDLESS_LANGUAGES set "SONDER_ENDLESS_LANGUAGES=python,javascript,powershell,cpp,csharp"
if not defined SONDER_ENDLESS_TIER set "SONDER_ENDLESS_TIER=fast"
if not defined SONDER_ENDLESS_WORKERS set "SONDER_ENDLESS_WORKERS=4"
if not defined SONDER_ENDLESS_TIMEOUT set "SONDER_ENDLESS_TIMEOUT=10"
if not defined SONDER_ENDLESS_REPAIRS set "SONDER_ENDLESS_REPAIRS=2"
if not defined SONDER_ENDLESS_SLEEP set "SONDER_ENDLESS_SLEEP=2"
if not defined SONDER_ENDLESS_STOP_AFTER_NO_PROGRESS set "SONDER_ENDLESS_STOP_AFTER_NO_PROGRESS=1"

if not defined SONDER_PYTHON (
  echo [sonder] ERROR: no bundled or system Python runtime was found.
  exit /b 1
)

if exist "%REPO%venv\Lib\site-packages" (
  set "PYTHONPATH=%REPO%venv\Lib\site-packages;%REPO%venv\Lib\site-packages\win32;%REPO%venv\Lib\site-packages\win32\lib;%REPO%venv\Lib\site-packages\pywin32_system32;%PYTHONPATH%"
)

"%SONDER_OLLAMA_EXE%" --version >nul 2>&1
if errorlevel 1 (
  echo [sonder] Ollama CLI not on PATH; using HTTP connection only.
) else (
  "%SONDER_OLLAMA_EXE%" list >nul 2>&1
  if errorlevel 1 (
    echo [sonder] starting Ollama...
    start "" /b "%SONDER_OLLAMA_EXE%" serve
    timeout /t 2 >nul
  )

  "%SONDER_OLLAMA_EXE%" list 2>nul | findstr /i "sonder" >nul
  if errorlevel 1 (
    echo [sonder] creating model alias ^(first run^)...
    "%SONDER_PYTHON%" "%REPO%bootstrap_engine.py"
  )
)

echo [sonder] endless grounded-practice loop starting.
echo [sonder] Stop with Ctrl+C.
echo [sonder] Per round: %SONDER_ENDLESS_TOTAL% jobs, languages=%SONDER_ENDLESS_LANGUAGES%, tier=%SONDER_ENDLESS_TIER%
"%SONDER_PYTHON%" "%REPO%endless_train.py"
set "RC=%ERRORLEVEL%"

echo [sonder] endless practice exited with code %RC%.
endlocal & exit /b %RC%
