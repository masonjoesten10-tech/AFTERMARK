@echo off
cd /d %~dp0
set HOST=127.0.0.1
set PORT=8765
python server.py
pause
