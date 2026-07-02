@echo off
setlocal
set "REPO=%~dp0"
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
"%REPO%venv\Scripts\python.exe" "%REPO%trilobite_serve.py" %*
endlocal
