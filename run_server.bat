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
    goto find_tokens
)

:: Check the py launcher (works with any installed Python 3)
py -3 --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=py -3
    goto find_tokens
)

echo [ERROR] Python 3 not found on your system!
echo Please install Python from https://www.python.org/downloads/
echo.
pause
exit /b 1

:find_tokens
if not "%FARM_ADMIN_TOKEN%"=="" goto check_vet
echo This server needs an admin token for writes and backups.
set /p FARM_ADMIN_TOKEN=Choose an admin token for this session: 

:check_vet
if not "%FARM_VET_TOKEN%"=="" goto run
echo A separate vet token is required to mark records verified.
set /p FARM_VET_TOKEN=Choose a DIFFERENT vet token for this session: 

:run
if "%FARM_ADMIN_TOKEN%"=="" (
    echo [ERROR] FARM_ADMIN_TOKEN cannot be empty.
    pause
    exit /b 1
)
if "%FARM_VET_TOKEN%"=="" (
    echo [ERROR] FARM_VET_TOKEN cannot be empty.
    pause
    exit /b 1
)
if /I "%FARM_ADMIN_TOKEN%"=="%FARM_VET_TOKEN%" (
    echo [ERROR] Admin and vet tokens must be different.
    pause
    exit /b 1
)

echo Using Python: %PYTHON_CMD%
echo Admin token: set
echo Vet token:   set
echo Enter the admin token in the browser's "Admin passcode" field when saving/backing up.
echo.
%PYTHON_CMD% server.py
pause
