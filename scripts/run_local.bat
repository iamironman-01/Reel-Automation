@echo off
rem Run ReelDrop locally on Windows.
rem   scripts\run_local.bat demo        dry-run demo in an isolated folder (nothing is sent)
rem   scripts\run_local.bat scheduler   the real scheduler, using your .env and config
rem   scripts\run_local.bat dashboard   the web dashboard at http://localhost:8501
rem   scripts\run_local.bat test        run the test suite
rem   scripts\run_local.bat cli ...     queue commands, e.g.  cli add "<reel url>"  /  cli list
cd /d "%~dp0.."

if not exist .venv\Scripts\activate.bat (
  echo Run  scripts\setup_local.bat  first.
  exit /b 1
)
call .venv\Scripts\activate.bat

set CMD=%1
if "%CMD%"=="" goto usage
shift

if /I "%CMD%"=="demo"      python scripts\local_demo.py %1 %2 %3 %4 & goto :eof
if /I "%CMD%"=="scheduler" python main.py & goto :eof
if /I "%CMD%"=="dashboard" python -m streamlit run dashboard.py & goto :eof
if /I "%CMD%"=="test"      python -m pytest -q & goto :eof
if /I "%CMD%"=="cli"       python -m reeldrop %1 %2 %3 %4 %5 %6 & goto :eof

:usage
echo Usage: scripts\run_local.bat demo ^| scheduler ^| dashboard ^| test ^| cli ...
exit /b 1
