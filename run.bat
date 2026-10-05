@echo off
rem Called by Windows Task Scheduler every hour. Appends to data\run.log.
cd /d "%~dp0"
".venv\Scripts\sma.exe" run >> "data\run.log" 2>&1
