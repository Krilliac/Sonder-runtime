@echo off
setlocal
set "REPO=%~dp0"
call "%REPO%sonder-runtime.cmd"
if not defined SONDER_NUM_THREAD set "SONDER_NUM_THREAD=%NUMBER_OF_PROCESSORS%"
if not defined SONDER_NUM_GPU set "SONDER_NUM_GPU=999"
if not defined SONDER_NUM_BATCH set "SONDER_NUM_BATCH=512"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"
if not defined SONDER_PYTHON (
  echo [sonder] ERROR: no bundled or system Python runtime was found.
  endlocal & exit /b 3
)
"%SONDER_OLLAMA_EXE%" list >nul 2>&1
if errorlevel 1 (
  echo [sonder] starting Ollama...
  start "" /b "%SONDER_OLLAMA_EXE%" serve
  timeout /t 2 >nul
)
"%SONDER_OLLAMA_EXE%" list 2>nul | findstr /i "sonder" >nul
if errorlevel 1 (
  echo [sonder] bootstrapping engine ^(first run^)...
  "%SONDER_PYTHON%" "%REPO%bootstrap_engine.py"
)
"%SONDER_PYTHON%" "%REPO%sonder_serve.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
