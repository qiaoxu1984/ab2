@echo off
rem Start this machine's Agent and connect it to the Manager.
cd /d "%~dp0"
python -c "import websockets" >nul 2>&1
if errorlevel 1 (
    echo Installing Agent dependencies...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install Agent dependencies.
        pause
        exit /b 1
    )
)
set "MANAGER_URL=%AB2_MANAGER_URL%"
if "%MANAGER_URL%"=="" set "MANAGER_URL=ws://172.18.67.71:8000/ws/agent"
python -m agent.service --manager "%MANAGER_URL%" --config data/agent.json --web-port 8020
