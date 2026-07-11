@echo off
cd /d %~dp0
where python >nul 2>nul
if errorlevel 1 (
  echo Python が見つかりません。https://www.python.org/downloads/ からインストールしてください。
  echo インストール時に「Add python.exe to PATH」にチェックを入れてください。
  pause
  exit /b 1
)
python -m pip install -r requirements.txt -q
python app.py
pause
