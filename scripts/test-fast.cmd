@echo off
rem Fast regression loop: tests selected from the change, under xdist worksteal.
rem
rem   scripts\test-fast.cmd                  change since merge-base with origin/main
rem   scripts\test-fast.cmd --working-tree   uncommitted change only
rem   scripts\test-fast.cmd --all            full suite
rem   scripts\test-fast.cmd -- -k pattern    anything after -- goes to pytest
rem
rem Resolves the interpreter exactly as scripts\run-tests.cmd does (see the
rem quoting note there). The logic lives in scripts\test_fast.py.
setlocal
set "REPO=%~dp0.."
set "PY=%SONDER_PYTHON%"
if not defined PY set "PY=%REPO%\venv\Scripts\python.exe"

if not defined SONDER_PYTHON if not exist "%PY%" (
  echo ERROR: no interpreter at "%PY%"
  echo The venv is missing or incomplete. Recreate it with:
  echo     python -m venv "%REPO%\venv"
  echo     "%PY%" -m pip install -r "%REPO%\requirements-dev.txt"
  exit /b 3
)

"%PY%" "%REPO%\scripts\test_fast.py" %*
exit /b %ERRORLEVEL%
