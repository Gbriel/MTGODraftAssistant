@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11+ from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer, then run this again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv || goto :fail
  echo Installing...
  ".venv\Scripts\python.exe" -m pip install --quiet -e . || goto :fail
)

".venv\Scripts\python.exe" -m mtgo_draft_assistant %*
exit /b %errorlevel%

:fail
echo Setup failed. See the messages above.
pause
exit /b 1
