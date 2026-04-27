@echo off
title NewMemSys Heartbeat Daemon
cd /d E:\ClaudeAI\NewMemSys
echo.
echo  NewMemSys Heartbeat Daemon
echo  Auto-restarts on crash. Close this window to stop.
echo.
:loop
C:\Python312\python.exe scripts\heartbeat_daemon.py
echo.
echo  Daemon exited. Restarting in 30 seconds...
timeout /t 30 /nobreak >nul
goto loop
