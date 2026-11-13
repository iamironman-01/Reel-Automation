@echo off
rem One-time local setup for Windows. Double-click or run:  scripts\setup_local.bat
cd /d "%~dp0.."

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo Python 3.11 or newer is required. Install it from python.org and tick "Add to PATH".
  exit /b 1
)

if not exist .venv\Scripts\python.exe python -m venv .venv
if errorlevel 1 exit /b 1

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1
python scripts\local_init.py

echo.
echo Setup done. Next:  scripts\run_local.bat demo
