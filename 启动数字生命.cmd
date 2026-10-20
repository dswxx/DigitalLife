@echo off
rem ---------------------------------------------------------------
rem DigitalLife launcher (double-clickable).
rem
rem KEEP THIS FILE PURE ASCII. cmd.exe reads a .cmd using the OEM
rem code page (936 on a Chinese Windows) while the file itself is
rem UTF-8: the multi-byte characters get mis-decoded and cmd eats
rem the first characters of the next token ("echo" becomes "o").
rem That is why this launcher contains ASCII only, and all Chinese
rem text is printed by Python (run.py). tests/test_docs.py checks it.
rem ---------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
title DigitalLife

rem Python must speak UTF-8 to the console, otherwise its Chinese
rem output turns into mojibake on a cp936 console.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

py -3 run.py --port 8787
if errorlevel 1 (
  echo.
  echo   Startup failed. The reason is above. The usual one is missing deps:
  echo       py -3 -m pip install fastapi uvicorn httpx
  echo.
  pause
)
