@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."
set "PYTHONW=%PROJECT_DIR%\.venv\Scripts\pythonw.exe"
set "VIEWER_SCRIPT=%SCRIPT_DIR%note_viewer.py"

rem --- .venv が存在するか確認 ---
if not exist "%PYTHONW%" (
    echo ERROR: .venv pythonw.exe was not found.
    echo        %PYTHONW%
    echo.
    echo Run scripts\setup.ps1 or create the .venv environment first.
    echo.
    echo [Setup hint] If setup.ps1 fails due to execution policy, run this first:
    echo   PowerShell: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
    echo   (Japanese) PowerShell で上のコマンドを実行してから setup.ps1 を再実行してください。
    pause
    exit /b 1
)

start "" /D "%PROJECT_DIR%" "%PYTHONW%" "%VIEWER_SCRIPT%"
