@echo off
REM Wrapper for Windows Task Scheduler — runs the Gousto scraper and logs output.
cd /d "%~dp0"
if not exist logs mkdir logs
for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set dt=%%a
set stamp=%dt:~0,8%
.venv\Scripts\python.exe scraper.py >> "logs\scrape_%stamp%.log" 2>&1
exit /b %errorlevel%
