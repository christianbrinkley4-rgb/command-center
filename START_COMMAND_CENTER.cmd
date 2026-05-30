@echo off
cd /d "%~dp0"
echo.
echo  ==========================================================
echo   COMMAND CENTER  -  starting...
echo  ==========================================================
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed or not on PATH. Install Python, then run this again.
  pause
  exit /b 1
)
python -m pip install -q -r requirements.txt
start "" http://localhost:5055
python app.py
pause
