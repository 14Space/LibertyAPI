@echo off
chcp 65001 > nul
cd /d "%~dp0"

:loop
.venv\Scripts\python.exe -m bot
echo Bot stopped, restarting in 10 seconds. Close this window to exit.
timeout /t 10 > nul
goto loop
