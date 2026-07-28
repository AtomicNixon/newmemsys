@echo on
title NewMemSys Heartbeat Daemon
cd /d E:\ClaudeAI\NewMemSys
echo.
echo  NewMemSys Heartbeat Daemon
echo  Close this window to stop. Ctrl+C for clean shutdown.
echo.
:loop
C:\Python312\python.exe scripts\heartbeat_daemon.py
if %ERRORLEVEL% EQU 1 (
    echo.
    echo  Daemon exited with code 1 (likely lock conflict or disabled^).
    echo  Not restarting. Close this window or investigate.
    pause
    goto end
)
echo.
echo  Daemon exited cleanly. Restarting in 30 seconds...
timeout /t 30 /nobreak >nul
goto loop
:end