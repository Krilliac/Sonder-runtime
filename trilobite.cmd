@echo off
setlocal
set "REPO=%~dp0"
if not defined LOCAL_LLM_NUM_THREAD set "LOCAL_LLM_NUM_THREAD=%NUMBER_OF_PROCESSORS%"
if not defined LOCAL_LLM_NUM_GPU set "LOCAL_LLM_NUM_GPU=999"
if not defined LOCAL_LLM_NUM_BATCH set "LOCAL_LLM_NUM_BATCH=512"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"
ollama list >nul 2>&1
if errorlevel 1 (
  echo [trilobite] starting Ollama...
  start "" /b ollama serve
  timeout /t 2 >nul
)
ollama list 2>nul | findstr /i "trilobite" >nul
if errorlevel 1 (
  echo [trilobite] creating model alias ^(first run^)...
  "%REPO%venv\Scripts\python.exe" "%REPO%setup_alias.py"
)
"%REPO%venv\Scripts\python.exe" "%REPO%trilobite_repl.py" %*
endlocal
