@echo off
REM Build LaptopAgentV2.exe on Windows — double-click this file once.
REM Requires Python 3.10+ (with "Add to PATH") and internet for pip.
REM Output: LaptopAgentV2.exe, copied here next to this file, so
REM the EXE, agent_config.json, and run_agent.bat all stay together
REM in one folder (e.g. C:\agent\). No more hunting in dist\.
REM This build runs on YOUR laptop, so the EXE is yours — Windows
REM SmartScreen will still warn because it is unsigned. That is normal
REM for a self-built EXE: click "More info" then "Run anyway".

setlocal
cd /d "%~dp0"

echo Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python not found. Install Python 3.10+ from python.org
  echo and tick "Add python.exe to PATH", then run this again.
  pause
  exit /b 1
)

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install pyinstaller websockets
REM Screenshots only — skip if you do not need them:
python -m pip install mss pillow

echo Building EXE (this takes a minute or two)...
python -m PyInstaller --clean --noconfirm laptop_agent_v2.spec
if errorlevel 1 (
  echo ERROR: PyInstaller build failed. Read the output above.
  pause
  exit /b 1
)

if not exist "dist\LaptopAgentV2.exe" (
  echo ERROR: build finished but dist\LaptopAgentV2.exe was not found.
  pause
  exit /b 1
)

copy /y "dist\LaptopAgentV2.exe" "LaptopAgentV2.exe" >nul
if errorlevel 1 (
  echo ERROR: could not copy dist\LaptopAgentV2.exe here.
  pause
  exit /b 1
)

echo.
echo DONE: LaptopAgentV2.exe is now in this folder, next to
echo agent_config.json and run_agent.bat.
echo Next: edit agent_config.json (real token, host = your Tailscale
echo IP, remote_control_enabled = true), then double-click
echo run_agent.bat to run it. Leave that window open.
echo.
pause
