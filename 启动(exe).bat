@echo off
chcp 65001 >nul
rem 直接启动打包好的单文件 exe（免 Python 环境）
cd /d "%~dp0dist"
if not exist "虚拟音视频注入器.exe" (
  echo [!] 未找到 dist\虚拟音视频注入器.exe
  echo     请先执行： python -m PyInstaller build.spec --noconfirm --clean
  pause
  exit /b 1
)
start "" "虚拟音视频注入器.exe"
