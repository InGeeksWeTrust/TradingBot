@echo off
REM Paper-trade the sweep + MES strategy for today (no real orders).
cd /d "%~dp0"
".venv\Scripts\python.exe" -m orbbot.bot --config config-sweep.yaml --dry-run --once
pause
