@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if errorlevel 1 (
    python -X utf8 app.py
    if errorlevel 1 pause
    exit /b
)
start "" pythonw.exe -X utf8 "%~dp0app.py"
