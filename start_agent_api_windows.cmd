@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".agent-api-env\Scripts\python.exe" (
  echo 请先双击 setup_agent_api_windows.cmd 安装环境。
  pause
  exit /b 1
)
".agent-api-env\Scripts\python.exe" -u batch_agent_api_launcher.py
set "api_exit=%errorlevel%"
echo.
echo 批量Agent接口任务已退出，退出码：%api_exit%
pause
exit /b %api_exit%
