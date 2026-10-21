@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
if exist ".agent-api-env\Scripts\python.exe" goto install
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 goto use_python
py -3 -m venv .agent-api-env
if errorlevel 1 goto failed
goto install
:use_python
where python >nul 2>nul
if errorlevel 1 goto missing
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 goto missing
python -m venv .agent-api-env
if errorlevel 1 goto failed
:install
".agent-api-env\Scripts\python.exe" -m pip install -r requirements-agent-api.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120 --retries 5
if errorlevel 1 goto failed
echo 安装完成，请双击 start_agent_api_windows.cmd。
pause
exit /b 0
:missing
echo 请先安装Python 3.10或更高版本，并将Python添加到PATH或启用Python Launcher。
echo https://www.python.org/downloads/windows/
pause
exit /b 1
:failed
echo 安装失败，请保留窗口中的错误信息。
pause
exit /b 1
