@echo off
REM Backtest the last 30 days and write journal\backtest.xlsx
cd /d "%~dp0"
"%USERPROFILE%\.pyvenv\Scripts\python.exe" -m orbbot.backtest --days 30
pause
