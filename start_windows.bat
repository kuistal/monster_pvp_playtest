@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)
%PY% -c "import flask" >nul 2>nul
if errorlevel 1 (
  echo Installing Flask for the first launch...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Failed to install Flask. Check Python and internet connection.
    pause
    exit /b 1
  )
)
echo.
echo Monster PvP: http://127.0.0.1:5000
start "" "http://127.0.0.1:5000"
%PY% app.py
if errorlevel 1 pause
endlocal
