@echo off
rem Robust pytest entrypoint for agents and CI.
rem
rem Exists because the venv interpreter was invoked three different ways across
rem this project's tooling and one of them failed with:
rem
rem     No Python at '"C:\Users\...\Python312\python.exe'
rem
rem Note the stray double-quote inside the path. That is a QUOTING failure as a
rem command crosses a shell boundary (bash -> cmd -> the venv redirector, which
rem re-execs the base interpreter named in pyvenv.cfg), not a missing file and
rem not a sandbox denial. It was misdiagnosed as both, twice, and cost an agent
rem lane its entire verification step -- it shipped fixes whose tests had never
rem been executed because it believed it could not run them.
rem
rem So: resolve the interpreter from this script's own location, quote it once,
rem and let callers pass plain pytest arguments.
rem
rem   scripts\run-tests.cmd                     all tests
rem   scripts\run-tests.cmd tests\test_foo.py   one file
rem   scripts\run-tests.cmd -q -k pattern       any pytest args
setlocal
set "REPO=%~dp0.."
set "PY=%REPO%\venv\Scripts\python.exe"

if not exist "%PY%" (
  echo ERROR: no interpreter at "%PY%"
  echo The venv is missing or incomplete. Recreate it with:
  echo     python -m venv "%REPO%\venv"
  echo     "%PY%" -m pip install -r "%REPO%\requirements.txt"
  exit /b 3
)

rem Prove the interpreter actually runs before handing it a test suite: a
rem redirector that cannot reach its base Python fails here with a clear
rem message instead of surfacing as a confusing collection error later.
"%PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo ERROR: "%PY%" exists but will not start.
  echo Its base interpreter is named in "%REPO%\venv\pyvenv.cfg"; check that it
  echo is present and readable, then recreate the venv if it is not.
  exit /b 4
)

"%PY%" -m pytest %*
exit /b %ERRORLEVEL%
