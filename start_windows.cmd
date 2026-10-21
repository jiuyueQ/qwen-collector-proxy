@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".batch-env\Scripts\python.exe" (
  echo 请先双击 setup_windows.cmd 安装批量脚本环境。
  pause
  exit /b 1
)
".batch-env\Scripts\python.exe" -u batch_windows_launcher.py %*
set "batch_exit=%errorlevel%"
echo.
echo 脚本已退出，退出码：%batch_exit%
pause
exit /b %batch_exit%
