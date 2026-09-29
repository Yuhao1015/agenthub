@echo off
rem Agent Hub desktop launcher (Windows). Double-click to start.
rem Starts the local server once, then opens the dashboard; a second
rem double-click just opens the browser against the running instance.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Need Python 3: https://www.python.org/downloads/
  pause
  exit /b 1
)

set "URL=http://127.0.0.1:8787/dashboard.html"

curl -s -m 2 http://127.0.0.1:8787/health >nul 2>nul
if not errorlevel 1 (
  start "" "%URL%"
  exit /b 0
)

if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "DATA_DIR=%LOCALAPPDATA%\AgentHub"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"

start "AgentHub" /min python serve.py

rem give the server a moment, then open the browser
timeout /t 2 /nobreak >nul
start "" "%URL%"
