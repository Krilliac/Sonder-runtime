@echo off
setlocal
set "REPO=%~dp0"

if not defined LOCAL_LLM_NUM_THREAD set "LOCAL_LLM_NUM_THREAD=%NUMBER_OF_PROCESSORS%"
if not defined LOCAL_LLM_NUM_GPU set "LOCAL_LLM_NUM_GPU=999"
if not defined LOCAL_LLM_NUM_BATCH set "LOCAL_LLM_NUM_BATCH=512"
if not defined LOCAL_LLM_CODE set "LOCAL_LLM_CODE=qwen2.5-coder:7b"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"

if not defined TRILOBITE_ENDLESS_TOTAL set "TRILOBITE_ENDLESS_TOTAL=30"
if not defined TRILOBITE_ENDLESS_LANGUAGES set "TRILOBITE_ENDLESS_LANGUAGES=python,javascript,powershell,cpp,csharp"
if not defined TRILOBITE_ENDLESS_TIER set "TRILOBITE_ENDLESS_TIER=fast"
if not defined TRILOBITE_ENDLESS_WORKERS set "TRILOBITE_ENDLESS_WORKERS=4"
if not defined TRILOBITE_ENDLESS_TIMEOUT set "TRILOBITE_ENDLESS_TIMEOUT=10"
if not defined TRILOBITE_ENDLESS_REPAIRS set "TRILOBITE_ENDLESS_REPAIRS=2"
if not defined TRILOBITE_ENDLESS_SLEEP set "TRILOBITE_ENDLESS_SLEEP=2"
if not defined TRILOBITE_ENDLESS_STOP_AFTER_NO_PROGRESS set "TRILOBITE_ENDLESS_STOP_AFTER_NO_PROGRESS=1"

set "PYEXE=%REPO%venv\Scripts\python.exe"
"%PYEXE%" -c "import sys" >nul 2>&1
if errorlevel 1 (
  set "PYEXE="
  if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
    set "PYEXE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  )
  for %%P in (py.exe python.exe) do (
    if not defined PYEXE (
      where %%P >nul 2>&1
      if not errorlevel 1 set "PYEXE=%%P"
    )
  )
)

if not defined PYEXE (
  echo [trilobite] ERROR: no usable Python found. Recreate venv or install Python.
  exit /b 1
)

if exist "%REPO%venv\Lib\site-packages" (
  set "PYTHONPATH=%REPO%venv\Lib\site-packages;%REPO%venv\Lib\site-packages\win32;%REPO%venv\Lib\site-packages\win32\lib;%REPO%venv\Lib\site-packages\pywin32_system32;%PYTHONPATH%"
)

where ollama >nul 2>&1
if errorlevel 1 (
  echo [trilobite] Ollama CLI not on PATH; using HTTP connection only.
) else (
  ollama list >nul 2>&1
  if errorlevel 1 (
    echo [trilobite] starting Ollama...
    start "" /b ollama serve
    timeout /t 2 >nul
  )

  ollama list 2>nul | findstr /i "trilobite" >nul
  if errorlevel 1 (
    echo [trilobite] creating model alias ^(first run^)...
    "%PYEXE%" "%REPO%setup_alias.py"
  )
)

echo [trilobite] endless training loop starting.
echo [trilobite] Stop with Ctrl+C.
echo [trilobite] Per round: %TRILOBITE_ENDLESS_TOTAL% jobs, languages=%TRILOBITE_ENDLESS_LANGUAGES%, tier=%TRILOBITE_ENDLESS_TIER%
"%PYEXE%" "%REPO%endless_train.py"
set "RC=%ERRORLEVEL%"

echo [trilobite] endless training exited with code %RC%.
endlocal & exit /b %RC%
