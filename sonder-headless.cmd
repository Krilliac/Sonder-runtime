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
"%SONDER_PYTHON%" "%REPO%sonder_headless.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
