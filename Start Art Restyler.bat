@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up...
  python -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)
".venv\Scripts\python.exe" server.py
goto :eof
:fail
echo Setup failed. Make sure Python 3.10+ is installed.
pause
