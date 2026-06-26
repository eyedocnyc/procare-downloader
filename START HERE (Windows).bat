@echo off
setlocal
cd /d "%~dp0"
title Procare Downloader

echo ================================================
echo   Procare Photo, Video and Scrapbook Downloader
echo ================================================
echo.

REM --- Find Python ---
set "PYTHON="
where py >nul 2>nul && set "PYTHON=py"
if not defined PYTHON (
  where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
  echo Python is not installed yet ^(you only need to do this once^).
  echo.
  echo   1^) Go to:  https://www.python.org/downloads/
  echo   2^) Run the installer and CHECK the box "Add Python to PATH"
  echo   3^) Then double-click this file again.
  echo.
  pause
  exit /b
)

echo Installing the small helpers this tool needs ^(one-time, ~1 minute^)...
%PYTHON% -m pip install -r requirements.txt --quiet --disable-pip-version-check
echo.

echo What would you like to do?
echo.
echo   [1] Download photos ^& videos AND build the scrapbook   ^(recommended^)
echo   [2] Download photos ^& videos only
echo   [3] Rebuild the scrapbook only ^(no re-downloading^)
echo.
set "choice="
set /p "choice=Type 1, 2, or 3 then press Enter (default is 1): "
if not defined choice set "choice=1"

REM Optional school name for the scrapbook (the class/room is detected for you).
set "SCHOOL_ARG="
if "%choice%"=="2" goto after_school
set "school="
set /p "school=Your school's name for the scrapbook (press Enter to skip): "
if defined school set SCHOOL_ARG=--school "%school%"
:after_school

echo.
echo You'll be asked for your Procare email and password next.
echo (Your password is hidden as you type and is never saved.)
echo.

if "%choice%"=="2" (
  %PYTHON% procare_download.py
) else if "%choice%"=="3" (
  %PYTHON% procare_download.py --scrapbook-only %SCHOOL_ARG%
) else (
  %PYTHON% procare_download.py --scrapbook %SCHOOL_ARG%
)

echo.
echo Finished. You can close this window.
pause
