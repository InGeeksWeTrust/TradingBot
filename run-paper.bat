@echo off
REM Paper-trade today's session (no real orders). Double-click, or point Task Scheduler at this file.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m orbbot.bot --once --dry-run
pause
