@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
if exist ".batch-env\Scripts\python.exe" goto install
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto use_python
py -3 -m venv .batch-env
if errorlevel 1 goto failed
goto install
:use_python
where python >nul 2>nul
if errorlevel 1 goto missing
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto missing
python -m venv .batch-env
if errorlevel 1 goto failed
:install
".batch-env\Scripts\python.exe" -m pip install -r requirements-batch.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120 --retries 5
if errorlevel 1 goto failed
echo.
echo 安装完成。请双击 start_windows.cmd 开始批量提问。
pause
exit /b 0
:missing
echo 请先安装 Python 3.10 或更高版本，并启用 Python Launcher 或添加 Python 到 PATH。
echo 官方下载地址：https://www.python.org/downloads/windows/
pause
exit /b 1
:failed
echo 安装未完成，请保留本窗口的错误信息。
pause
exit /b 1
