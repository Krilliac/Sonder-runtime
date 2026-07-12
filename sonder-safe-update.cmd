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
set "STAMP=%DATE:/=-%-%TIME::=-%"
set "STAMP=%STAMP: =0%"
git status --porcelain > "%TEMP%\sonder-git-status.txt"
for %%A in ("%TEMP%\sonder-git-status.txt") do set "STATUS_SIZE=%%~zA"
set "STASHED=0"
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
)
del "%TEMP%\sonder-git-status.txt" >nul 2>nul
echo [sonder] fetching latest main...
git fetch origin main
if errorlevel 1 goto fail
echo [sonder] rebasing local checkout...
git rebase origin/main
if errorlevel 1 goto fail
if "%STASHED%"=="1" (
  echo [sonder] restoring saved local edits...
  git stash apply
  if errorlevel 1 (
    echo WARNING: updated to latest main, but saved local edits need manual conflict resolution.
    echo Your backup stash was kept. Run: git stash list
    popd >nul
    exit /b 2
  )
  git stash drop >nul 2>nul
)
echo [sonder] update complete.
popd >nul
exit /b 0

:fail
echo ERROR: update failed. If a rebase is in progress, run: git rebase --abort
if "%STASHED%"=="1" echo Your local edits are saved in git stash. Run: git stash list
popd >nul
exit /b 1
