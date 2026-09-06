@echo off
rem Start the localhost-only AB2 service control page.
start "AB2 Control" /min python control.py --host 127.0.0.1 --port 8010
timeout /t 1 /nobreak >nul
start "" "http://127.0.0.1:8010/"
