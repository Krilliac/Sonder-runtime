@echo off
setlocal
set "REPO=%~dp0"
pushd "%REPO%" >nul 2>nul
if errorlevel 1 (
  echo ERROR: could not enter repo folder %REPO%
  exit /b 1
)
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
  echo ERROR: %REPO% is not a Git checkout.
  popd >nul
  exit /b 1
)
set "PRE_HEAD="
for /f "delims=" %%H in ('git rev-parse HEAD') do set "PRE_HEAD=%%H"
if not defined PRE_HEAD (
  echo ERROR: could not resolve the current revision.
  popd >nul
  exit /b 1
)
set "PRE_BRANCH="
for /f "delims=" %%B in ('git symbolic-ref --quiet --short HEAD 2^>nul') do set "PRE_BRANCH=%%B"
set "STAMP=%DATE:/=-%-%TIME::=-%"
set "STAMP=%STAMP: =0%"
git status --porcelain > "%TEMP%\sonder-git-status.txt"
for %%A in ("%TEMP%\sonder-git-status.txt") do set "STATUS_SIZE=%%~zA"
set "STASHED=0"
set "STASH_SHA="
if not "%STATUS_SIZE%"=="0" (
  echo [sonder] saving local edits before update...
  git stash push --include-untracked -m "sonder gui update backup %STAMP%"
  if errorlevel 1 (
    echo ERROR: could not save local edits. Commit or move them, then retry.
    del "%TEMP%\sonder-git-status.txt" >nul 2>nul
    popd >nul
    exit /b 1
  )
  set "STASHED=1"
  for /f "delims=" %%S in ('git rev-parse -q --verify refs/stash') do set "STASH_SHA=%%S"
)
del "%TEMP%\sonder-git-status.txt" >nul 2>nul
rem Address our stash entry by SHA, never the positional stash@{0}: the stash
rem stack is shared by every worktree and concurrent session.
if "%STASHED%"=="1" if not defined STASH_SHA (
  echo ERROR: saved local edits but could not identify the stash entry. Refusing to continue; run: git stash list
  popd >nul
  exit /b 1
)
echo [sonder] fetching latest main...
git fetch origin main
if errorlevel 1 goto fail
echo [sonder] rebasing local checkout...
git rebase origin/main
if errorlevel 1 goto rebase_fail
if "%STASHED%"=="1" (
  echo [sonder] restoring saved local edits...
  git stash apply %STASH_SHA%
  if errorlevel 1 (
    echo WARNING: updated to latest main, but saved local edits need manual conflict resolution.
    echo Your backup stash was kept. Run: git stash list
    popd >nul
    exit /b 2
  )
  call :drop_stash
)
echo [sonder] update complete.
popd >nul
exit /b 0

:rebase_fail
rem A conflicted rebase (for example of local selfmod commits) must not leave
rem the live runtime sources mid-rebase with conflict markers.
echo ERROR: update failed; aborting the rebase and restoring %PRE_HEAD%...
git rebase --abort >nul 2>nul
set "NOW_HEAD="
for /f "delims=" %%H in ('git rev-parse HEAD') do set "NOW_HEAD=%%H"
if not "%NOW_HEAD%"=="%PRE_HEAD%" goto restore_fail
if not defined PRE_BRANCH goto branch_ok
set "NOW_BRANCH="
for /f "delims=" %%B in ('git symbolic-ref --quiet --short HEAD 2^>nul') do set "NOW_BRANCH=%%B"
if not "%NOW_BRANCH%"=="%PRE_BRANCH%" goto restore_fail
:branch_ok
if "%STASHED%"=="1" (
  git stash apply %STASH_SHA%
  if errorlevel 1 (
    echo ERROR: checkout restored to %PRE_HEAD%, but saved local edits could not be re-applied cleanly.
    echo Your backup stash was kept. Run: git stash list
    popd >nul
    exit /b 1
  )
  call :drop_stash
)
echo ERROR: update aborted; checkout restored to %PRE_HEAD%
popd >nul
exit /b 1

:restore_fail
echo ERROR: could not restore the checkout to %PRE_HEAD% after the failed rebase. Run: git status
if "%STASHED%"=="1" echo Your local edits are saved in git stash. Run: git stash list
popd >nul
exit /b 1

:drop_stash
set "STASH_INDEX=0"
set "STASH_DROPPED=0"
for /f "delims=" %%S in ('git stash list --format^=%%H') do call :drop_if_ours %%S
exit /b 0

:drop_if_ours
if "%STASH_DROPPED%"=="1" exit /b 0
if "%~1"=="%STASH_SHA%" (
  git stash drop "stash@{%STASH_INDEX%}" >nul 2>nul
  set "STASH_DROPPED=1"
  exit /b 0
)
set /a STASH_INDEX+=1
exit /b 0

:fail
echo ERROR: update failed.
if "%STASHED%"=="1" echo Your local edits are saved in git stash. Run: git stash list
popd >nul
exit /b 1
