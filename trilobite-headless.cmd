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
if not defined LOCAL_LLM_NUM_THREAD set "LOCAL_LLM_NUM_THREAD=%NUMBER_OF_PROCESSORS%"
if not defined LOCAL_LLM_NUM_GPU set "LOCAL_LLM_NUM_GPU=999"
if not defined LOCAL_LLM_NUM_BATCH set "LOCAL_LLM_NUM_BATCH=512"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"
if exist "%REPO%venv\Scripts\python.exe" (
  "%REPO%venv\Scripts\python.exe" "%REPO%trilobite_headless.py" %*
) else (
  python "%REPO%trilobite_headless.py" %*
)
endlocal
