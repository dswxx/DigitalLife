@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   正在启动圆桌...
echo.
py -3 run.py --port 8787
if errorlevel 1 (
  echo.
  echo   启动失败。上面有原因。常见的是没装依赖：
  echo       py -3 -m pip install fastapi uvicorn httpx
  echo.
  pause
)
