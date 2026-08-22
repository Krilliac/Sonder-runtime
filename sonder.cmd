@echo off
setlocal
set "REPO=%~dp0"
call "%REPO%sonder-runtime.cmd"
if not defined SONDER_HOST set "SONDER_HOST=127.0.0.1"
if not defined SONDER_PORT set "SONDER_PORT=11435"
if not defined SONDER_CONTEXT_SIZE set "SONDER_CONTEXT_SIZE=8192"
if not defined SONDER_NUM_THREAD set "SONDER_NUM_THREAD=%NUMBER_OF_PROCESSORS%"
if not defined SONDER_NUM_GPU set "SONDER_NUM_GPU=999"
if not defined SONDER_NUM_BATCH set "SONDER_NUM_BATCH=512"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"

rem Surface the reasoning a model emits on its own thinking channel. Off by
rem default upstream, and off means Sonder never even asks Ollama for it, so
rem turning this on changes what is requested. Audience is deliberately left
rem at its default (developer) rather than 'all'. Unset it to go back.
if not defined SONDER_EXPOSE_REASONING set "SONDER_EXPOSE_REASONING=1"
if not defined SONDER_PYTHON (
  echo [sonder] ERROR: no bundled or system Python runtime was found.
  endlocal & exit /b 3
)

rem Bootstrap is quiet on success and loud on failure. Both steps below print
rem the same "ollama reachable / alias ready" pair, so the old unconditional
rem echo showed every launch the same eight lines twice, above a status block
rem the REPL banner now covers. Nothing is lost: on failure the captured log is
rem printed in full, which is strictly more than the old output showed, and
rem SONDER_TERMINAL_VERBOSE=1 restores the running commentary.
rem Concurrent terminal launches must not redirect into one shared file: cmd
rem reports a visible "file is being used" warning before the REPL otherwise.
rem Preserve an explicit operator override for diagnostics, but give the normal
rem launcher a per-invocation path.
if not defined SONDER_BOOT_LOG (
  set "SONDER_BOOT_LOG=%TEMP%\sonder-bootstrap-%RANDOM%-%RANDOM%.log"
  set "SONDER_BOOT_LOG_GENERATED=1"
)

if /I not "%SONDER_TERMINAL_BOOTSTRAP%"=="0" (
  if /I "%SONDER_TERMINAL_VERBOSE%"=="1" (
    "%SONDER_PYTHON%" "%REPO%sonder_headless.py" engine
  ) else (
    "%SONDER_PYTHON%" "%REPO%sonder_headless.py" engine >"%SONDER_BOOT_LOG%" 2>&1
  )
  if errorlevel 1 (
    echo [sonder] ERROR: local engine is unavailable or blocked by endpoint policy.
    if exist "%SONDER_BOOT_LOG%" type "%SONDER_BOOT_LOG%"
    endlocal & exit /b 2
  )
)

if /I not "%SONDER_TERMINAL_START_SERVER%"=="0" (
  if /I "%SONDER_TERMINAL_VERBOSE%"=="1" (
    "%SONDER_PYTHON%" "%REPO%sonder_headless.py" start --host "%SONDER_HOST%" --port "%SONDER_PORT%" --context-size "%SONDER_CONTEXT_SIZE%"
  ) else (
    "%SONDER_PYTHON%" "%REPO%sonder_headless.py" start --host "%SONDER_HOST%" --port "%SONDER_PORT%" --context-size "%SONDER_CONTEXT_SIZE%" >"%SONDER_BOOT_LOG%" 2>&1
    if errorlevel 1 (
      findstr /C:"sonder: already listening" "%SONDER_BOOT_LOG%" >nul 2>&1
      if not errorlevel 1 (
        echo [sonder] NOTE: using an existing local API server that this launch does not manage.
      ) else (
        rem A cold server can become managed/listening in the narrow interval
        rem after the headless readiness return. Re-probe now instead of trusting
        rem an older status block in the failed startup log: the child could
        rem also have exited after that earlier observation.
        "%SONDER_PYTHON%" "%REPO%sonder_headless.py" status --host "%SONDER_HOST%" --port "%SONDER_PORT%" | findstr /C:"sonder api: listening on" >nul
        if not errorlevel 1 (
          echo [sonder] NOTE: local API server became ready after the startup probe.
        ) else (
          echo [sonder] WARNING: local API server did not start cleanly.
          if exist "%SONDER_BOOT_LOG%" type "%SONDER_BOOT_LOG%"
        )
      )
    )
  )
)

rem Generated logs have served their only purpose once bootstrap/start output
rem has either been printed or accepted. An explicitly supplied path remains
rem untouched for operator diagnostics.
if defined SONDER_BOOT_LOG_GENERATED if exist "%SONDER_BOOT_LOG%" del /q "%SONDER_BOOT_LOG%" >nul 2>&1

if defined SONDER_SERVER (
  if /I not "%SONDER_TERMINAL_REMOTE%"=="0" (
    "%SONDER_PYTHON%" "%REPO%sonder_client.py" %*
    endlocal
    exit /b %ERRORLEVEL%
  )
)

pushd "%REPO%" >nul
"%SONDER_PYTHON%" -m sonder_runtime repl %*
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul
if not "%EXIT_CODE%"=="0" (
  endlocal & exit /b %EXIT_CODE%
)
endlocal
