@echo off
REM Build BOTH EXEs on Windows — double-click this file once.
REM   LaptopAgentV2.exe  (the agent, console app, from laptop_agent_v2.spec)
REM   LaptopLauncher.exe (the GUI launcher, windowed, from launcher.spec)
REM Both are copied here next to this file, so the EXEs, the config
REM and the scripts all stay together in ONE folder (e.g. C:\agent\).
REM Requires Python 3.10+ (with "Add to PATH") and internet for pip.
REM This build runs on YOUR laptop, so the EXEs are yours — Windows
REM SmartScreen will still warn because they are unsigned. That is
REM normal for a self-built EXE: click "More info" then "Run anyway".

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

echo Building agent EXE...
python -m PyInstaller --clean --noconfirm laptop_agent_v2.spec
if errorlevel 1 (
  echo ERROR: agent build failed. Read the output above.
  pause
  exit /b 1
)
if not exist "dist\LaptopAgentV2.exe" (
  echo ERROR: agent build finished but dist\LaptopAgentV2.exe missing.
  pause
  exit /b 1
)
copy /y "dist\LaptopAgentV2.exe" "LaptopAgentV2.exe" >nul

echo Building launcher EXE...
python -m PyInstaller --clean --noconfirm launcher.spec
if errorlevel 1 (
  echo ERROR: launcher build failed. Read the output above.
  pause
  exit /b 1
)
if not exist "dist\LaptopLauncher.exe" (
  echo ERROR: launcher build finished but dist\LaptopLauncher.exe missing.
  pause
  exit /b 1
)
copy /y "dist\LaptopLauncher.exe" "LaptopLauncher.exe" >nul

echo.
echo DONE. Both EXEs are now in this folder:
echo   LaptopAgentV2.exe  (the agent — you do not need to run it directly)
echo   LaptopLauncher.exe (the GUI — double-click this one)
echo Next: double-click LaptopLauncher.exe, go to Settings,
echo Generate a token, Auto-detect the host, turn remote control
echo ON, Save settings, then Start on the Status tab.
echo.
pause
