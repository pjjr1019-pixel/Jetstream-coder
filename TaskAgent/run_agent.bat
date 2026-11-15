@echo off
REM Run LaptopAgentV2.exe and keep the window open so you can read
REM any error. Put this file in the SAME folder as LaptopAgentV2.exe
REM and agent_config.json, then double-click THIS file instead of the EXE.
REM If the agent exits with an error, the message stays on screen.
REM Close this window (or Ctrl+C) to stop the agent and revoke access.
cd /d "%~dp0"
if not exist "LaptopAgentV2.exe" (
  echo ERROR: LaptopAgentV2.exe is not in this folder.
  echo Put run_agent.bat next to LaptopAgentV2.exe and agent_config.json,
  echo e.g. both in C:\agent\, then double-click run_agent.bat again.
  pause
  exit /b 1
)
if not exist "agent_config.json" (
  echo WARNING: agent_config.json is NOT in this folder.
  echo The agent will exit with "config not found".
  echo Copy agent_config.example.json to agent_config.json here and edit it:
  echo   token = a long random string, host = your Tailscale IP,
  echo   remote_control_enabled = true
  echo.
)
LaptopAgentV2.exe
echo.
echo Agent stopped with exit code %ERRORLEVEL%.
echo If it closed right away, read the error lines above and send them to Jett.
pause
