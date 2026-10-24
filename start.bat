@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Create the environment first: py -3.12 -m venv .venv
  exit /b 1
)
.venv\Scripts\python.exe main.py
exit /b %errorlevel%
