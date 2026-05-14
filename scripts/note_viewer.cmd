@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."
start "" "%PROJECT_DIR%\.venv\Scripts\pythonw.exe" "%SCRIPT_DIR%note_viewer.py"
