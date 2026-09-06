@echo off
rem Start the central Manager on all network interfaces.
python -m uvicorn manager.main:app --host 0.0.0.0 --port 8000
