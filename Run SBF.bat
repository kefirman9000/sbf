@echo off
rem Starts SBF without a black command window (pythonw / pyw have no console).
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw -3 main.py
  exit /b
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw main.py
  exit /b
)
title SBF
echo Python was not found.
echo Get it from https://www.python.org/downloads/ and tick "Add Python to PATH".
echo.
pause
