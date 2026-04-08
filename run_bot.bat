@echo off
cd /d "%~dp0"
echo Weather Bot — auto-restart loop
echo Logs: data/logs/bot.log
echo Press Ctrl+C to stop
echo.

:loop
echo [%date% %time%] Starting bot...
python -u main.py
echo.
echo [%date% %time%] Bot exited. Restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
