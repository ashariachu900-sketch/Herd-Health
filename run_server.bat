@echo off
setlocal
title CattleCare Server

echo ========================================================
echo   Starting CattleCare Full-Stack Server...
echo ========================================================
echo.

set PYTHON_CMD=

:: Check if standard python command works
python --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=python
    goto run
)

:: Check if py launcher works
py --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=py
    goto run
)

:: Check AppData Python installation
if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" (
    set "PYTHON_CMD=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
    goto run
)

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto run
)

if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto run
)

echo [ERROR] Python not found on your system!
echo Please install Python from https://www.python.org/downloads/
echo.
pause
exit /b 1

:run
echo Using Python: %PYTHON_CMD%
echo.
"%PYTHON_CMD%" server.py
pause
