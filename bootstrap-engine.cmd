@echo off
setlocal
set "REPO=%~dp0"
pushd "%REPO%" || exit /b 1
call "%REPO%trilobite-runtime.cmd"
if not defined LOCAL_LLM_NUM_THREAD set "LOCAL_LLM_NUM_THREAD=%NUMBER_OF_PROCESSORS%"
if not defined LOCAL_LLM_NUM_GPU set "LOCAL_LLM_NUM_GPU=999"
if not defined LOCAL_LLM_NUM_BATCH set "LOCAL_LLM_NUM_BATCH=512"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"
if not defined TRILOBITE_PYTHON (
  echo [trilobite] ERROR: no bundled or system Python runtime was found.
  popd
  endlocal & exit /b 3
)
"%TRILOBITE_PYTHON%" "%REPO%bootstrap_engine.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
endlocal & exit /b %EXIT_CODE%
