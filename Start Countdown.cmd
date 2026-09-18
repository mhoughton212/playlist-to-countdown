@echo off
cd /d "%~dp0"
python -m countdown --restart --reload
if errorlevel 1 (
  echo.
  echo Playlist to Countdown could not start. The error is shown above.
  pause
)
