@echo off
setlocal
set "REPO=%~dp0"
call "%REPO%trilobite-runtime.cmd"
if not defined LOCAL_LLM_NUM_THREAD set "LOCAL_LLM_NUM_THREAD=%NUMBER_OF_PROCESSORS%"
if not defined LOCAL_LLM_NUM_GPU set "LOCAL_LLM_NUM_GPU=999"
if not defined LOCAL_LLM_NUM_BATCH set "LOCAL_LLM_NUM_BATCH=512"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"
if not defined TRILOBITE_PYTHON (
  echo [trilobite] ERROR: no bundled or system Python runtime was found.
  endlocal & exit /b 3
)
"%TRILOBITE_OLLAMA_EXE%" list >nul 2>&1
if errorlevel 1 (
  echo [trilobite] starting Ollama...
  start "" /b "%TRILOBITE_OLLAMA_EXE%" serve
  timeout /t 2 >nul
)
"%TRILOBITE_OLLAMA_EXE%" list 2>nul | findstr /i "trilobite" >nul
if errorlevel 1 (
  echo [trilobite] bootstrapping engine ^(first run^)...
  "%TRILOBITE_PYTHON%" "%REPO%bootstrap_engine.py"
)
"%TRILOBITE_PYTHON%" "%REPO%trilobite_serve.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
