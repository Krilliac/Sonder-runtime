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
"%TRILOBITE_PYTHON%" "%REPO%trilobite_headless.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
