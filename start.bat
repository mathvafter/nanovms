@echo off
REM JagaNVR launcher (Windows)
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo python not found on PATH
  exit /b 1
)
python run.py %*
