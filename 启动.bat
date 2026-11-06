@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PY=python
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [!] 未找到 python，请先安装 Python 3.10+ 并加入 PATH
  pause
  exit /b 1
)

%PY% -c "import numpy,cv2,pyvirtualcam,sounddevice,av" >nul 2>nul
if errorlevel 1 (
  echo [*] 首次运行，正在安装依赖...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [!] 依赖安装失败，请检查网络后重试
    pause
    exit /b 1
  )
)

%PY% main.py %*
if errorlevel 1 pause
