@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "PYTHONW=%PROJECT_DIR%.venv\Scripts\pythonw.exe"
set "GUI_SCRIPT=%PROJECT_DIR%scripts\record_gui.py"
set "CHECK_ONLY=0"

if /I "%~1"=="/check" set "CHECK_ONLY=1"

if not exist "%PYTHONW%" (
    echo ERROR: .venv pythonw.exe was not found.
    echo        %PYTHONW%
    echo.
    echo Run scripts\setup.ps1 or create the .venv environment first.
    if "%CHECK_ONLY%"=="1" exit /b 1
    pause
    exit /b 1
)

if not exist "%GUI_SCRIPT%" (
    echo ERROR: record_gui.py was not found.
    echo        %GUI_SCRIPT%
    if "%CHECK_ONLY%"=="1" exit /b 1
    pause
    exit /b 1
)

if "%CHECK_ONLY%"=="1" (
    echo PersonalRAG launcher check OK.
    exit /b 0
)

start "" /D "%PROJECT_DIR%" "%PYTHONW%" "%GUI_SCRIPT%"
exit /b 0
