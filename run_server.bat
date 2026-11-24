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
    goto find_token
)

:: Check the py launcher (works with any installed Python 3)
py -3 --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=py -3
    goto find_token
)

echo [ERROR] Python 3 not found on your system!
echo Please install Python from https://www.python.org/downloads/
echo.
pause
exit /b 1

:find_token
if not "%FARM_ADMIN_TOKEN%"=="" goto run
echo This server needs an admin token for writes and backups.
set /p FARM_ADMIN_TOKEN=Choose an admin token for this session: 

:run
if "%FARM_ADMIN_TOKEN%"=="" (
    echo [ERROR] FARM_ADMIN_TOKEN cannot be empty.
    pause
    exit /b 1
)

echo Using Python: %PYTHON_CMD%
echo.
%PYTHON_CMD% server.py
pause
